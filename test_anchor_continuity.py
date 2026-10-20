import unittest
from copy import deepcopy

from core import Controller, _FixedEpoch, _ReleaseRisk
from test_core import HOSTS
from test_scenarios import feedback, fleet
from thermal_policy import ThermalMemory


WIFI = (HOSTS[2], 'wifi:continuity', 'wifi')
WIFI_IDENTITY = (HOSTS[2], 'DGX', 'estimate:gpu', WIFI[1], WIFI[2])


def samples(now, wifi=None):
    nodes = fleet(now, cpu=38, gpu=38, board=46, load=5, power=45)
    nodes[2]['sensors'].append(dict(id=WIFI[1], group='wifi', label='wifi',
        calibrated=True, temp=(42 if now % 60 < 15 else 40) if wifi is None else wifi))
    return nodes


def controller_with_debt():
    memory = ThermalMemory()
    memory.add_anchor(WIFI_IDENTITY, 37, 42)
    controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
    controller.restore_thermal_memory(memory.export(), 0)
    return controller


def step(controller, now, *, nodes=None, actual=9, readback=None):
    return controller.decide(samples(now) if nodes is None else nodes, now,
        minimum_level=0, feedback=feedback(now, actual) if readback is None else readback)


class AnchorContinuityTests(unittest.TestCase):
    def prime(self, controller, stop=207):
        for now in range(0, stop + 1, 3):
            decision = step(controller, now)
            self.assertEqual(decision['level'], 9)
        return decision

    def test_repeated_wifi_steps_recover_exhausted_anchor_and_request_adjacent(self):
        controller = controller_with_debt()
        mature = False
        for now in range(0, 604, 3):
            decision = step(controller, now)
            mature |= decision['operating']['observe_credit'] == 180
            if decision['level'] < 9:
                self.assertEqual(decision['level'], 8)
                self.assertEqual(decision['execution_trial']['status'], 'pending')
                self.assertGreaterEqual(now, 210)
                anchor = next(a for a in controller.export_thermal_memory()['anchors']
                    if tuple(a['source']) == WIFI_IDENTITY)
                self.assertEqual(anchor['anchor_c'], 40)
                self.assertLessEqual(anchor['peak_c'] - anchor['anchor_c'], 2)
                return
        self.assertTrue(mature, 'fixture must earn credit through Controller.decide')
        self.fail(f"safe repeated Wi-Fi returns never requested adjacent descent; "
            f"actual=9 request={decision['level']} credit={decision['operating']['observe_credit']} "
            f"anchor={controller.export_thermal_memory()['anchors']}")

    def test_budget_hold_does_not_claim_an_unobserved_recent_step(self):
        controller = controller_with_debt()
        for now in range(0, 58, 3):
            decision = step(controller, now, nodes=samples(now, 40))
        messages = [r for r in decision['reasons'] if r.startswith('[release-wait]')]
        self.assertTrue(messages)
        self.assertEqual(decision['level'], 9)
        self.assertNotIn('近期跳温', ' '.join(messages))

    def test_new_unreturned_peak_at_rebase_tick_holds_without_borrowing_permission(self):
        for jump in (43, 44):
            with self.subTest(jump=jump):
                controller = controller_with_debt()
                self.prime(controller)
                decision = step(controller, 210, nodes=samples(210, jump))
                self.assertEqual(decision['level'], 9)
                self.assertFalse(controller._pending())
                if jump == 43:
                    self.assertEqual(decision['operating']['observe_credit'], 0)
                    self.assertEqual(controller._thermal_memory.anchors[WIFI_IDENTITY]['anchor_c'], 37)
                for now in range(213, 240, 3):
                    decision = step(controller, now, nodes=samples(now, jump))
                    self.assertEqual(decision['level'], 9)
                    self.assertFalse(controller._pending())

    def test_other_source_new_step_still_blocks_when_wifi_recovers(self):
        controller = controller_with_debt()
        self.prime(controller)
        for now in range(210, 240, 3):
            nodes = samples(now)
            nodes[0]['cpu_temp'] = nodes[0]['sensors'][0]['temp'] = 40
            decision = step(controller, now, nodes=nodes)
            self.assertEqual(decision['level'], 9)
            self.assertFalse(controller._pending())
        self.assertEqual(controller._thermal_memory.anchors[WIFI_IDENTITY]['anchor_c'], 40)
        self.assertEqual(decision['operating']['observe_credit'], 180)

    def test_new_peak_cancels_recovered_pending_without_extending_deadline(self):
        controller = controller_with_debt()
        self.prime(controller)
        decision = step(controller, 210)
        self.assertEqual(decision['level'], 8)
        deadline = decision['execution_trial']['original_deadline']
        decision = step(controller, 213, nodes=samples(213, 43),
            readback=feedback(213, 9, confirmed=False, execution='pending'))
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['execution_trial']['status'], 'invalidated')
        self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
        self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_discontinuities_cannot_borrow_preceding_observation(self):
        for case in ('gap', 'hostname', 'power_source', 'sensor', 'gear', 'storage',
                     'missing', 'repeated', 'load'):
            with self.subTest(case=case):
                controller = controller_with_debt()
                self.prime(controller)
                now = 219 if case == 'gap' else 210
                nodes, actual = samples(now), 9
                if case == 'hostname':
                    nodes[2]['hostname'] = 'replacement'
                elif case == 'power_source':
                    nodes[2]['power_source'] = 'replacement'
                elif case == 'sensor':
                    nodes[2]['sensors'][-1]['id'] = 'wifi:replacement'
                elif case == 'gear':
                    actual = 8
                elif case == 'storage':
                    controller.pause_release('storage interruption', 208)
                elif case == 'missing':
                    nodes[2]['sensors'].pop()
                elif case == 'repeated':
                    nodes[2]['source_ts'] = 207
                elif case == 'load':
                    nodes[2].update(cpu_load=40, gpu_load=40, power_w=100)
                decision = step(controller, now, nodes=nodes, actual=actual)
                self.assertGreaterEqual(decision['level'], actual)
                self.assertFalse(controller._pending() and
                    controller._policy['operation']['purpose'] == 'release')
                self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_restart_restores_debt_and_failures_but_no_live_credit(self):
        controller = controller_with_debt()
        self.prime(controller)
        memory = controller.export_thermal_memory()
        memory['failures'].append(dict(source=list(WIFI_IDENTITY), safe_level=9,
            failed_level=8, baseline=[5, 5, 45], automatic=True,
            failure_class='thermal_failure'))
        restarted = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        restarted.restore_thermal_memory(memory, 1000)
        self.assertEqual(restarted.export_thermal_memory(), memory)
        self.assertEqual(restarted.operating.observe_credit, 0)
        self.assertEqual(restarted._plateau_since, {})
        for now in range(1000, 1180, 3):
            decision = step(restarted, now)
            self.assertGreaterEqual(decision['level'], 9)
        self.assertEqual(restarted.export_thermal_memory()['failures'], memory['failures'])

    def test_genuine_warming_and_rapid_rise_do_not_release(self):
        for rapid in (False, True):
            with self.subTest(rapid=rapid):
                controller = controller_with_debt()
                for now in range(0, 301, 3):
                    wifi = 40 + now * .02 if not rapid else 40 + max(0, now - 90)
                    decision = step(controller, now, nodes=samples(now, min(76, wifi)))
                    self.assertGreaterEqual(decision['level'], 9)
                    if rapid and wifi >= 76:
                        self.assertGreater(decision['level'], 9)
                        self.assertTrue(any(r.startswith('[hard]') for r in decision['reasons']))
                        break
                self.assertEqual(controller._thermal_memory.anchors[WIFI_IDENTITY]['anchor_c'], 37)

    def local_anchor(self, points, *, guard=None, rapid=False, danger=False,
                     pending=False, epoch_since=0, incoming='observe_only'):
        controller = controller_with_debt()
        controller.history[WIFI[0]] = {WIFI[1]: []}
        controller._fixed_epochs[WIFI[0]] = _FixedEpoch((), {}, epoch_since, 0, epoch_since)
        for now, temp in points:
            controller.history[WIFI[0]][WIFI[1]].append((now, temp))
            raw = dict(temp=temp, target=65, guard_projected=temp if guard is None else guard,
                covered=True, rapid_rise=rapid, step_recent=now > 0 and now % 60 == 0)
            risk = _ReleaseRisk(incoming, 10, 'fixture_current_permission')
            result = controller._anchor_risk(WIFI, raw, risk,
                dict(now=now, nodes=samples(now, temp), thermal_risk=danger), pending=pending)
        return controller._thermal_memory.anchors[WIFI_IDENTITY], result

    def test_retained_interval_rebases_without_promoting_incoming_risk(self):
        points = [(t, 42 if t <= 90 or t == 180 else 40) for t in range(0, 181, 3)]
        for kind in ('observe_only', 'refused'):
            with self.subTest(kind=kind):
                anchor, result = self.local_anchor(points, incoming=kind)
                self.assertEqual(anchor, dict(anchor_c=40, peak_c=42))
                self.assertEqual(result, _ReleaseRisk(kind, 10, 'fixture_current_permission'))

    def test_raw_eight_degree_boundary_and_complete_window_remain_required(self):
        points = [(t, 40) for t in range(0, 181, 3)]
        anchor, _ = self.local_anchor(points, guard=57)
        self.assertEqual(anchor['anchor_c'], 40)
        cases = [dict(guard=57.000001), dict(rapid=True), dict(danger=True),
                 dict(pending=True), dict(epoch_since=.001)]
        for options in cases:
            with self.subTest(options=options):
                anchor, _ = self.local_anchor(points, **options)
                self.assertEqual(anchor['anchor_c'], 37)
        for invalid in (points[:-1], [p for p in points if not 81 <= p[0] <= 93],
                        [(t, 40 + t * .02) for t in range(0, 181, 3)]):
            with self.subTest(end=invalid[-1]):
                anchor, _ = self.local_anchor(invalid)
                self.assertEqual(anchor['anchor_c'], 37)

    def test_recent_step_interval_requires_full_sixty_seconds_and_bounded_range(self):
        for amplitude, expected_anchor in ((2, 40), (2.000001, 37), (4.5, 37), (8.7, 37)):
            with self.subTest(amplitude=amplitude):
                points = [(t, 40 + amplitude if t == 180 else 40) for t in range(0, 181, 3)]
                anchor, result = self.local_anchor(points)
                self.assertEqual(anchor['anchor_c'], expected_anchor)
                self.assertNotEqual(result.kind, 'ready')
        points = [(t, 40) for t in range(30, 211, 3)]
        anchor, _ = self.local_anchor(points)
        self.assertEqual(anchor['anchor_c'], 37)


class ReleaseDiagnosticTests(unittest.TestCase):
    def risk(self, *, persistent=False, guard=40, step_recent=False):
        raw = dict(temp=40, target=60, guard_projected=guard, covered=True,
            rapid_rise=False, persistent_heat=persistent, uncertain=True,
            step_recent=step_recent, rise=0, slope=0)
        source = dict(uncertain=True, release_margin=20)
        points = [(t, 40) for t in range(0, 31, 3)]
        return Controller._fixed_release_risk(raw, source, points, 30, False)

    def test_refusal_reason_matches_existing_margin_or_heat_branch(self):
        for persistent, guard, reason in ((False, 52.001, 'raw_projection_margin'),
                (True, 40, 'persistent_heat'), (True, 52.001, 'raw_projection_margin')):
            with self.subTest(persistent=persistent, guard=guard):
                risk = self.risk(persistent=persistent, guard=guard)
                self.assertEqual((risk.kind, risk.margin_c), ('refused', min(20, 60 - guard)))
                self.assertEqual(risk.reason, reason)
        self.assertEqual(self.risk(guard=52).kind, 'ready')

    def release(self, kinds):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        nodes, evidence = samples(30, 40), {}
        for index, kind in enumerate(kinds):
            key = (HOSTS[index], 'acpi:0:acpitz', 'acpi')
            controller.history[key[0]] = {key[1]: [(t, 40) for t in range(0, 31, 3)]}
            evidence[key] = dict(temp=40, target=60, guard_projected=53 if kind == 'refused' else 40,
                covered=True, rapid_rise=False, persistent_heat=False, uncertain=True,
                step_recent=kind == 'observe_only', rise=0, slope=0, release_margin=20)
        before = deepcopy(evidence)
        release = controller._fixed_release(dict(now=30, nodes=nodes,
            thermal_risk=False, evidence=evidence), evidence)
        self.assertEqual(evidence, before)
        return release, list(evidence)

    def test_reported_blocker_prefers_refused_and_preserves_tie_order(self):
        for kinds, index in ((('observe_only', 'refused', 'refused'), 1),
                (('refused', 'refused', 'observe_only'), 0),
                (('ready', 'observe_only', 'observe_only'), 1),
                (('ready', 'ready', 'ready'), None)):
            with self.subTest(kinds=kinds):
                release, keys = self.release(kinds)
                self.assertEqual(release['blocking_source'], keys[index] if index is not None else None)
                self.assertEqual(release['can_execute'], all(kind == 'ready' for kind in kinds))
                self.assertEqual(release['observation_eligible'], 'refused' not in kinds)
                self.assertEqual(release['margin'], 7 if 'refused' in kinds else 20)


if __name__ == '__main__':
    unittest.main()
