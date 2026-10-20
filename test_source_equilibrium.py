import unittest

from core import Controller, _FixedEpoch
from thermal_policy import robust_slope
from test_core import HOSTS
from test_scenarios import feedback, fleet


def oscillation(stamp):
    phase = (stamp + 39) % 90
    return 42.4 if stamp % 90 == 9 else 40 + .03 * min(phase, 90 - phase)


class SourceEquilibriumTests(unittest.TestCase):
    def test_small_decelerating_step_observes_but_does_not_execute(self):
        values = [40.8]*6 + [40.9,41.4,41.6,41.4,41.5,41.6] + [41.7]*9
        points = [(3*i, value) for i, value in enumerate(values)]
        self.assertGreater(robust_slope(points), .01)
        risk = self.risk(points, slope=.0067, rise=0, guard_projected=41.974)
        self.assertEqual(risk.kind, 'refused')
        self.assertEqual(self.risk(points, slope=.03, rise=.03).kind, 'refused')
        self.assertEqual(self.risk(points, persistent_heat=True).kind, 'refused')
        self.assertEqual(self.risk(points, guard_projected=52.001).kind, 'refused')
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 121, 3):
            temp = 40.8 if now < 60 else values[(now-60)//3]
            decision = controller.decide(fleet(now,cpu=temp,gpu=38,board=46,load=5,power=45),
                now,minimum_level=0,feedback=feedback(now,10))
        self.assertGreater(decision['operating']['observe_credit'], 60)
        self.assertEqual(decision['level'], 10)
        self.assertFalse(controller._pending())

    def test_idle_jitter_does_not_erase_temperature_equilibrium(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 601, 3):
            nodes = fleet(now, cpu=38 if now < 90 else 43, gpu=38, board=46,
                power=45, load=2 if now % 30 < 15 else 10)
            controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 10))
        anchors = controller.export_thermal_memory()['anchors']
        self.assertTrue(all(a['anchor_c'] == 43 for a in anchors if a['source'][-1]=='acpi'))

    def risk(self, points, target=60, **changes):
        raw = dict(temp=points[-1][1], target=target, guard_projected=points[-1][1],
            rise=0, slope=0, covered=True, rapid_rise=False, persistent_heat=False,
            uncertain=True, step_recent=False)
        raw.update(changes)
        source = dict(uncertain=True, release_margin=target - raw['temp'])
        return Controller._fixed_release_risk(raw, source, points, points[-1][0], False)

    def test_stationary_positive_phase_requires_complete_proof(self):
        points = [(t, oscillation(t)) for t in range(0, 181, 3)]
        self.assertGreater(robust_slope(points[-21:]), .01)
        self.assertLessEqual(robust_slope(points), 0)
        risk = self.risk(points)
        self.assertEqual((risk.kind, risk.reason), ('ready', 'stationary_envelope'))
        self.assertEqual(self.risk(points[1:]).kind, 'refused')

    def test_stationary_envelope_raw_peak_and_rate_share_eight_degree_margin(self):
        points = [(t, oscillation(t)) for t in range(0, 181, 3)]
        peak = max(v for _, v in points)
        forecast = peak + 180 * max(0, round(robust_slope(points[-21:]), 12))
        self.assertEqual(self.risk(points, forecast + 8).kind, 'ready')
        self.assertEqual(self.risk(points, forecast + 8).margin_c, 8)
        self.assertEqual(self.risk(points, forecast + 7.999999).kind, 'refused')
        self.assertEqual(self.risk(points, guard_projected=52.000001).kind, 'refused')

    def test_stationary_new_peak_gap_and_positive_baseline_invalidate_proof(self):
        points = [(t, oscillation(t)) for t in range(0, 181, 3)]
        cases = [points[:-1] + [(180, 42.400001)],
                 [p for p in points if p[0] not in (90, 93)],
                 [(t, v + .01 * t) for t, v in points]]
        for case in cases:
            with self.subTest(case=case[-1]):
                self.assertEqual(self.risk(case).kind, 'refused')

    def local_window(self, values, *, since=0, pending=False):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        key = (HOSTS[0], 'acpi:0:acpitz', 'acpi')
        now = values[-1][0]
        nodes = fleet(now, cpu=values[-1][1], gpu=38, board=46, load=5, power=45)
        identity = controller._source_memory_key(nodes[0], key)
        controller._thermal_memory.add_anchor(identity, 38, 43)
        controller.history[key[0]] = {key[1]: []}
        controller._fixed_epochs[key[0]] = _FixedEpoch((), {}, since, now, since)
        frame = dict(now=now, nodes=nodes, thermal_risk=False)
        raw = dict(temp=values[-1][1], target=60, guard_projected=values[-1][1],
            rise=0, slope=0, covered=True, rapid_rise=False, persistent_heat=False,
            uncertain=False, step_recent=False)
        source = dict(uncertain=False, release_margin=60 - raw['temp'])
        for stamp, value in values:
            controller.history[key[0]][key[1]].append((stamp, value))
            frame['now'] = stamp
            raw['temp'] = raw['guard_projected'] = value
            risk = Controller._fixed_release_risk(raw, source,
                [p for p in values if p[0] <= stamp], stamp, False, pending=pending)
            controller._anchor_risk(key, raw, risk, frame, pending=pending)
        return controller, controller._thermal_memory.anchors[identity]

    def test_source_local_warmer_floor_refills_without_global_readiness(self):
        controller, anchor = self.local_window([(t, 43) for t in range(0, 181, 3)])
        self.assertEqual(anchor, dict(anchor_c=43, peak_c=43))
        saved = controller.export_thermal_memory()
        restarted = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        restarted.restore_thermal_memory(saved, 1000)
        self.assertEqual(restarted.export_thermal_memory()['anchors'], saved['anchors'])
        self.assertEqual(restarted._fixed_epochs, {})

    def test_local_refill_preserves_completed_block_debt(self):
        values = [(t, 43.4 if t <= 90 else 43) for t in range(0, 181, 3)]
        _, anchor = self.local_window(values)
        self.assertEqual(anchor, dict(anchor_c=43, peak_c=43.4))

    def test_full_local_proof_rebases_without_serial_instant_ready_wait(self):
        values = [(t, oscillation(t) + 3) for t in range(0, 181, 3)]
        self.assertEqual(self.risk(values[:31]).kind, 'refused')
        self.assertEqual(self.risk(values).reason, 'stationary_envelope')
        _, anchor = self.local_window(values)
        self.assertGreater(anchor['anchor_c'], 43)
        self.assertLessEqual(anchor['peak_c'] - anchor['anchor_c'], 2)

    def test_local_refill_requires_180_nonpositive_comparable_seconds_and_no_pending(self):
        for values, options in (
                ([(t, 43 + t / 120) for t in range(0, 181, 3)], {}),
                ([(t, 43) for t in range(0, 181, 3)], dict(pending=True)),
                ([(t, 43) for t in range(0, 181, 3)], dict(since=.001)),
                ([(t, 43) for t in range(0, 178, 3)] + [(179.999, 43)], {}),
                ([(t, 43) for t in range(0, 181, 3) if t not in (90, 93)], {})):
            with self.subTest(options=options, end=values[-1]):
                _, anchor = self.local_window(values, **options)
                self.assertEqual(anchor['anchor_c'], 38)
                self.assertGreater(anchor['peak_c'] - anchor['anchor_c'], 2)

    def test_source_a_rebases_while_b_noise_still_blocks_fleet_descent(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 541, 3):
            nodes = fleet(now, cpu=38 if now < 90 else 43,
                gpu=38, board=46, load=5, power=45)
            noisy = 50 + .03 * (now % 120)
            nodes[1]['cpu_temp'] = noisy
            nodes[1]['sensors'][0]['temp'] = noisy
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 10))
            self.assertGreaterEqual(decision['level'], 10)
        anchor = next(a for a in controller.export_thermal_memory()['anchors']
            if a['source'][0] == HOSTS[0] and a['source'][-1] == 'acpi')
        self.assertEqual(anchor['anchor_c'], 43)
        self.assertEqual(anchor['peak_c'], 43)
        self.assertFalse(controller._pending())


if __name__ == '__main__':
    unittest.main()
