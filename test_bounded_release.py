import unittest
from copy import deepcopy
from unittest.mock import patch

from core import Controller, NoAscent
from test_core import HOSTS
from test_scenarios import feedback, fleet


WAVE = (0, .1, .4, .8, 1, .8, .6, .5, .4, .3, .2, .1, 0, 0)


def sample(now):
    nodes = fleet(now, cpu=43, gpu=40, board=50, power=45, load=5)
    for index, node in enumerate(nodes):
        node['cpu_load'] = 1 + ((now // 3 + index) % 6) * 2
        node['gpu_load'] = 0
        node['power_w'] = 44 + ((now // 3 + index) % 4) * 4
        for source_index, sensor in enumerate(node['sensors']):
            base = 43 if sensor['group'] == 'acpi' else 40 if sensor['group'] == 'gpu' else 50
            sensor['temp'] = base + WAVE[(now // 3 + index * 3 + source_index) % len(WAVE)]
        sync_temperatures(node)
    return nodes


def sync_temperatures(node):
    for group, name in (('acpi', 'cpu_temp'), ('gpu', 'gpu_temp'), ('mlx', 'board_temp')):
        node[name] = max(s['temp'] for s in node['sensors'] if s['group'] == group)


class BoundedReleaseTests(unittest.TestCase):
    def controller(self):
        return Controller(HOSTS, rules_only=True, high_wind_gate=True)

    def step(self, controller, now, actual=10, nodes=None, **options):
        return controller.decide(sample(now) if nodes is None else nodes, now,
            minimum_level=options.pop('minimum_level', 0),
            feedback=feedback(now, actual, **options.pop('readback', {})), **options)

    def first_release(self, controller, make_nodes=sample, start=0, stop=363):
        for now in range(start, stop, 3):
            decision = self.step(controller, now, nodes=make_nodes(now))
            if decision['level'] < 10:
                self.assertEqual(decision['level'], 9)
                self.assertGreaterEqual(now - start, 180)
                return now, decision
        self.fail(f"no adjacent 10-to-9 request in {(stop - start) // 3} fresh frames; "
            f"demand={decision['demand']} request={decision['level']} actual=10")

    def test_bounded_wave_and_idle_power_jitter_eventually_release_one_step(self):
        self.first_release(self.controller())

    def test_no_ascent_service_demotion_preserves_physical_endpoint_observation(self):
        controller = self.controller()
        demotions = 0
        for now in range(0, 9, 3):
            readback = feedback(now, 16,
                observed_mono=now - 3.1 if now >= 30 and now % 30 == 0 else now)
            if readback['level'] != min(controller.effective_limit(now), controller.level):
                readback.update(confirmed=False, execution='pending')
                demotions += 1
            decision = controller.decide(self.flat(now, 40), now,
                minimum_level=0, feedback=readback)
            self.assertIsInstance(controller.high_wind.proof, NoAscent)
            self.assertEqual(controller.operating.confirmed_level, 16)
            if decision['level'] < 16:
                self.assertEqual(decision['level'], 15)
                self.assertEqual(decision['execution_trial']['status'], 'pending')
                self.assertEqual(decision['execution_trial']['original_deadline'], 60)
                if now == 6:
                    self.assertGreater(demotions, 1)
                    return
        self.fail(f"fresh known 16/RPM endpoint never requested 15; "
            f"demotions={demotions}, credit={decision['operating']['observe_credit']}")

    def test_120_phase_shifted_sources_release_with_or_without_context_rebuilds(self):
        for jitter in (False, True):
            with self.subTest(jitter=jitter):
                def many(now):
                    nodes = sample(now)
                    for index, node in enumerate(nodes):
                        if not jitter:
                            node.update(cpu_load=5, power_w=45)
                        node['sensors'].extend(dict(id=f'other:wave:{number}', group='other',
                            label=str(number), calibrated=True,
                            temp=42 + WAVE[(now // 3 + index * 30 + number) % len(WAVE)])
                            for number in range(27))
                    self.assertEqual(sum(len(n['sensors']) for n in nodes), 120)
                    return nodes
                self.first_release(self.controller(), many)

    def test_120_phase_shifted_two_degree_pulses_eventually_request_one_step(self):
        for jitter in (False, True):
            with self.subTest(jitter=jitter):
                def pulses(now):
                    nodes = self.flat(now, 30)
                    for index, node in enumerate(nodes):
                        if jitter:
                            node.update(cpu_load=1 + (now // 3 + index) % 6 * 2,
                                power_w=44 + (now // 3 + index) % 4 * 4)
                        node['sensors'].extend(dict(id=f'other:pulse:{number}', group='other',
                            label=str(number), calibrated=True,
                            temp=30 + (2 if (now + (index * 30 + number) * 3) % 60 < 15 else 0))
                            for number in range(27))
                    return nodes
                now, decision = self.first_release(self.controller(), pulses, stop=603)
                self.assertGreaterEqual(now, 210)
                self.assertEqual(decision['execution_trial']['status'], 'pending')

    def test_no_ascent_does_not_excuse_invalid_physical_or_source_feedback(self):
        for case in ('stale', 'zero_rpm', 'unknown', 'fault', 'wrong_endpoint',
                     'identity', 'interrupted', 'readback_failure', 'gap'):
            with self.subTest(case=case):
                controller = self.controller()
                for now in range(0, 181, 3):
                    self.step(controller, now, actual=16, nodes=self.flat(now, 40))
                now = 189 if case == 'gap' else 183
                nodes, readback, actual = self.flat(now, 40), dict(confirmed=False, execution='pending'), 16
                if case == 'stale':
                    readback['observed_mono'] = now - 9.001
                elif case == 'zero_rpm':
                    readback['rpm'] = 0
                elif case == 'unknown':
                    readback['level'] = None
                elif case == 'fault':
                    readback['fault'] = True
                elif case == 'wrong_endpoint':
                    actual = 15
                elif case == 'identity':
                    nodes[0]['hostname'] = 'replacement'
                elif case == 'interrupted':
                    readback['protection_interrupted'] = True
                elif case == 'readback_failure':
                    readback['readback_failure'] = 'initial_transport'
                decision = self.step(controller, now, actual=actual, nodes=nodes, readback=readback)
                self.assertEqual(decision['operating']['observe_credit'], 0)
                self.assertFalse(controller._pending() and controller._policy['operation']['purpose'] == 'release')

    def test_proven_returning_steps_execute_when_full_observation_completes(self):
        controller = self.controller()
        requested = None
        for now in range(0, 363, 3):
            nodes = self.flat(now, 44 if 30 <= now % 60 < 45 else 42)
            decision = self.step(controller, now, nodes=nodes)
            if now == 207:
                self.assertEqual(decision['level'], 10)
                self.assertEqual(decision['operating']['observe_credit'], 177)
            if decision['level'] < 10:
                requested = now
                self.assertEqual(decision['level'], 9)
                break
        self.assertEqual(requested, 210)

    def flat(self, now, cpu=45.5):
        return fleet(now, cpu=cpu, gpu=40, board=50, power=45, load=5)

    def prime(self, controller, stop=207):
        for now in range(0, stop + 1, 3):
            decision = self.step(controller, now, nodes=self.flat(now))
            self.assertEqual(decision['level'], 10)
        return decision

    def test_own_uncapped_eight_degree_boundary(self):
        for endpoint, expected in ((47, 9), (47.001, 10)):
            with self.subTest(endpoint=endpoint):
                controller = self.controller()
                self.prime(controller, 204)
                self.step(controller, 207, nodes=self.flat(207, 46.5))
                decision = self.step(controller, 210, nodes=self.flat(210, endpoint))
                self.assertEqual(decision['demand'], 0)
                self.assertEqual(decision['level'], expected)

    def test_capped_forecast_cannot_supply_guard_margin(self):
        controller = self.controller()
        for now in range(0, 205, 3):
            self.step(controller, now, nodes=self.flat(now, 42))
        self.step(controller, 207, nodes=self.flat(207, 43))
        decision = self.step(controller, 210, nodes=self.flat(210, 46.5))
        self.assertEqual(decision['demand'], 0)
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_large_cold_steps_refuse_until_full_poststep_window(self):
        for jump in (2, 4.5, 8.7):
            with self.subTest(jump=jump):
                controller = self.controller()
                for now in range(0, 210, 3):
                    self.step(controller, now, nodes=self.flat(now, 30))
                for now in range(210, 240, 3):
                    decision = self.step(controller, now, nodes=self.flat(now, 30 + jump))
                    self.assertEqual(decision['level'], 10)
                    self.assertLessEqual(decision['operating']['observe_credit'], 180)
                    if now == 210:
                        self.assertEqual(decision['operating']['observe_credit'], 0 if jump == 8.7 else 180)
                released = None
                for now in range(240, 633, 3):
                    decision = self.step(controller, now, nodes=self.flat(now, 30 + jump))
                    if decision['level'] < 10:
                        self.assertEqual(decision['level'], 9)
                        released = now
                        break
                self.assertIsNotNone(released)
                self.assertEqual(released, 240 if jump == 2 else 420)

    def test_two_sample_positive_step_is_also_retained(self):
        controller = self.controller()
        self.prime(controller, 204)
        self.step(controller, 207, nodes=self.flat(207, 46.5))
        decision = self.step(controller, 210, nodes=self.flat(210, 47.5))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_raw_rapid_and_current_critical_protection(self):
        controller = self.controller()
        self.prime(controller, 90)
        for now, cpu in ((93, 50), (96, 53), (99, 56)):
            decision = self.step(controller, now, 8, nodes=self.flat(now, cpu))
        self.assertGreater(decision['level'], 8)
        self.assertTrue(any(r.startswith('[hard]') for r in decision['reasons']))
        decision = self.step(controller, 102, 8, nodes=self.flat(102, 75))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(controller.motion.actual_c, 75)
        self.assertEqual(decision['normal_ceiling'], 10)

    def test_repeated_fresh_feedback_does_not_earn_time(self):
        controller = self.controller()
        before = self.prime(controller, 180)['operating']['observe_credit']
        for now in (183, 186, 189):
            decision = self.step(controller, now, nodes=self.flat(now),
                readback=dict(observed_mono=180))
            self.assertEqual(decision['level'], 10)
            self.assertEqual(decision['operating']['observe_credit'], before)
        decision = self.step(controller, 192, nodes=self.flat(192))
        self.assertEqual(decision['operating']['observe_credit'], before + 3)

    def test_invalid_sources_and_feedback_revoke_credit(self):
        for case in ('unknown', 'hostname', 'sensor', 'group', 'power_source', 'stale_source',
                     'duplicate_source', 'wifi_gap', 'zero_rpm', 'stale_feedback',
                     'mismatch', 'interrupted', 'readback_failure', 'gap', 'power', 'load', 'minimum'):
            with self.subTest(case=case):
                controller = self.controller()
                self.prime(controller, 180)
                now = 189 if case == 'gap' else 183
                nodes, options = self.flat(now), {}
                if case == 'unknown':
                    nodes[0].update(hostname=None, hostname_verified=False)
                elif case == 'hostname':
                    nodes[0]['hostname'] = 'replacement'
                elif case in ('sensor', 'group'):
                    nodes[0]['sensors'][0]['id' if case == 'sensor' else 'group'] = 'replacement'
                elif case == 'power_source':
                    nodes[0]['power_source'] = 'replacement'
                elif case in ('stale_source', 'duplicate_source'):
                    nodes[0]['source_ts'] = now - (9 if case == 'stale_source' else 3)
                elif case == 'wifi_gap':
                    nodes[0]['sensors'].append(dict(id='wifi:new', group='wifi',
                        label='wifi', temp=40, calibrated=True))
                elif case == 'zero_rpm':
                    options['readback'] = dict(rpm=0)
                elif case == 'stale_feedback':
                    options['readback'] = dict(observed_mono=now - 10)
                elif case == 'mismatch':
                    options['readback'] = dict(confirmed=False, execution='pending')
                elif case == 'interrupted':
                    options['readback'] = dict(protection_interrupted=True)
                elif case == 'readback_failure':
                    options['readback'] = dict(readback_failure='initial_transport')
                elif case == 'power':
                    nodes[0]['power_w'] = 80
                elif case == 'load':
                    nodes[0]['cpu_load'] = 30
                elif case == 'minimum':
                    options['minimum_level'] = 1
                decision = self.step(controller, now, nodes=nodes, **options)
                self.assertGreaterEqual(decision['level'], 10)
                self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_pending_safe_old_endpoint_keeps_deadline_then_unsafe_guard_cancels(self):
        controller = self.controller()
        self.prime(controller, 204)
        self.step(controller, 207, nodes=self.flat(207, 46.5))
        decision = self.step(controller, 210, nodes=self.flat(210, 47))
        self.assertEqual(decision['level'], 9)
        deadline = decision['execution_trial']['original_deadline']
        decision = self.step(controller, 213, nodes=self.flat(213, 47),
            readback=dict(confirmed=False, execution='pending'))
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['execution_trial']['status'], 'pending')
        self.assertEqual(decision['operating']['observe_credit'], 0)
        decision = self.step(controller, 216, nodes=self.flat(216, 48.6),
            readback=dict(confirmed=False, execution='pending'))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['execution_trial']['status'], 'invalidated')
        self.assertEqual(decision['execution_trial']['original_deadline'], deadline)

    def test_pending_stale_source_cancels_and_timeout_is_not_renewed(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                controller = self.controller()
                now, decision = self.first_release(controller)
                deadline = decision['execution_trial']['original_deadline']
                while now < deadline:
                    now += 3
                    nodes = sample(now)
                    if stale:
                        nodes[0]['source_ts'] -= 9
                    decision = self.step(controller, now, nodes=nodes,
                        readback=dict(confirmed=False, execution='pending'))
                    self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
                    self.assertEqual(decision['operating']['observe_credit'], 0)
                    if stale:
                        self.assertGreaterEqual(decision['level'], 10)
                        self.assertEqual(decision['trial']['outcome'], 'invalidated')
                        self.assertEqual(decision['execution_trial']['purpose'], 'protect')
                        self.assertEqual(decision['high_wind']['exception'], 'SourceFaultProtection')
                        break
                    if now < deadline:
                        self.assertEqual(decision['level'], 9)
                if not stale:
                    self.assertEqual(decision['execution_trial']['status'], 'execution_timeout')

    def test_bounded_step_cancels_pending_but_does_not_falsify_arrival(self):
        for arrived in (False, True):
            with self.subTest(arrived=arrived):
                controller = self.controller()
                for now in range(0, 211, 3):
                    decision = self.step(controller, now, nodes=self.flat(now, 30))
                self.assertEqual(decision['level'], 9)
                deadline = decision['execution_trial']['original_deadline']
                decision = self.step(controller, 213, actual=9 if arrived else 10,
                    nodes=self.flat(213, 32), readback={} if arrived else
                    dict(confirmed=False, execution='pending'))
                self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
                self.assertEqual(decision['operating']['observe_credit'], 0)
                if arrived:
                    self.assertEqual(decision['level'], 9)
                    self.assertEqual(decision['execution_trial']['status'], 'confirmed')
                    self.assertIsNotNone(controller._policy['last_downward'])
                else:
                    self.assertEqual(decision['level'], 10)
                    self.assertEqual(decision['execution_trial']['status'], 'invalidated')

    def test_overlapping_steps_renew_execution_gate_without_credit_bank(self):
        controller = self.controller()
        for now in range(0, 211, 3):
            self.step(controller, now, nodes=self.flat(now, 30 if now < 210 else 32))
        for now in range(213, 261, 3):
            nodes = self.flat(now, 32)
            if now >= 225:
                for sensor in nodes[1]['sensors']:
                    if sensor['group'] == 'gpu':
                        sensor['temp'] = 42
                sync_temperatures(nodes[1])
            decision = self.step(controller, now, nodes=nodes)
            if now < 255:
                self.assertEqual(decision['level'], 10)
            else:
                self.assertEqual(decision['level'], 9)
                break

    def test_pending_physical_endpoint_requires_current_ownership_and_admission(self):
        for case in ('identity', 'power_source', 'minimum', 'mode', 'zero_rpm', 'interrupted'):
            with self.subTest(case=case):
                controller = self.controller()
                now, decision = self.first_release(controller)
                deadline = decision['execution_trial']['original_deadline']
                nodes = sample(now + 3)
                readback = dict(confirmed=False, execution='pending')
                options = {}
                if case == 'identity':
                    nodes[0]['hostname'] = 'replacement'
                elif case == 'power_source':
                    nodes[0]['power_source'] = 'replacement'
                elif case == 'minimum':
                    options['minimum_level'] = 1
                elif case == 'mode':
                    options['manual_level'] = 10
                elif case == 'zero_rpm':
                    readback['rpm'] = 0
                else:
                    readback['protection_interrupted'] = True
                decision = self.step(controller, now + 3, nodes=nodes, readback=readback, **options)
                self.assertGreaterEqual(decision['level'], 10)
                self.assertEqual(decision['execution_trial']['status'], 'invalidated')
                self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
                self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_destination_confirmation_requires_fresh_full_observation(self):
        controller = self.controller()
        now, _ = self.first_release(controller)
        decision = self.step(controller, now + 3, actual=9)
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['scenario'], 'recovery_released')
        self.assertEqual(decision['operating']['observe_credit'], 0)
        for stamp in range(now + 6, now + 183, 3):
            self.assertEqual(self.step(controller, stamp, actual=9)['level'], 9)

    def test_storage_pause_during_pending_consumes_all_credit(self):
        controller = self.controller()
        now, decision = self.first_release(controller)
        deadline = decision['execution_trial']['original_deadline']
        decision = controller.pause_release('storage interruption', now + 1)
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
        for stamp in range(now + 3, now + 183, 3):
            self.assertEqual(self.step(controller, stamp)['level'], 10)

    def test_release_view_preserves_raw_and_ordinary_snapshots(self):
        controller = self.controller()
        classify = controller._fixed_release
        bounded = []

        def inspect(frame, sources, **options):
            before = deepcopy((frame['evidence'], frame['uncertain'], sources))
            view = classify(frame, sources, **options)
            self.assertEqual((frame['evidence'], frame['uncertain'], sources), before)
            for key, source in sources.items():
                released, raw = view['evidence'][key], frame['evidence'][key]
                if source['uncertain'] and not released['uncertain']:
                    bounded.append(key)
                    self.assertTrue(raw['covered'])
                    self.assertFalse(raw['persistent_heat'] or raw['rapid_rise'])
                    self.assertGreaterEqual(raw['target'] - raw['guard_projected'], 8)
                    self.assertLessEqual(released['release_margin'], source['release_margin'])
            return view

        with patch.object(controller, '_fixed_release', side_effect=inspect):
            self.first_release(controller)
        self.assertTrue(bounded)

    def test_guard_captures_short_fallback_full_window_and_negative_rates(self):
        for case, expected in (('short', 51), ('negative', 44.5), ('long', 58)):
            with self.subTest(case=case):
                controller, captured = self.controller(), []
                original = controller._fixed_ordinary

                def inspect(frame, *args):
                    captured.append(deepcopy(frame['evidence']))
                    return original(frame, *args)

                with patch.object(controller, '_fixed_ordinary', side_effect=inspect):
                    if case == 'long':
                        for now in range(0, 31, 3):
                            self.step(controller, now, nodes=self.flat(now,
                                30 if now == 0 else 44 if now == 30 else 45))
                    else:
                        self.step(controller, 0, nodes=self.flat(0))
                        self.step(controller, 3, nodes=self.flat(3, 46 if case == 'short' else 44.5))
                raw = next(s for key, s in captured[-1].items() if key[0] == HOSTS[0] and key[2] == 'acpi')
                self.assertAlmostEqual(raw['guard_projected'], expected)

    def test_raw_persistent_heating_cannot_gain_bounded_exemption(self):
        controller = self.controller()
        for now in range(0, 363, 3):
            nodes = sample(now)
            for node in nodes:
                for sensor in node['sensors']:
                    if sensor['group'] == 'acpi':
                        sensor['temp'] = 30 + now * .03
                sync_temperatures(node)
            decision = self.step(controller, now, nodes=nodes)
            self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['demand'], 0)
        self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_noisy_persistent_ramp_and_monotonic_staircase_refuse_release(self):
        for shape in ('noisy_ramp', 'staircase'):
            with self.subTest(shape=shape):
                controller = self.controller()
                for now in range(0, 603, 3):
                    cpu = (30 + now * .03 + (.08 if now // 3 % 2 else -.08)
                        if shape == 'noisy_ramp' else 30 + now // 30)
                    decision = self.step(controller, now, nodes=self.flat(now, cpu))
                    self.assertGreaterEqual(decision['level'], 10)
                self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_genuine_rise_or_expanding_pulses_after_returns_prevent_next_descent(self):
        for shape in ('ramp', 'expanding'):
            with self.subTest(shape=shape):
                controller = self.controller()
                def returning(now):
                    return self.flat(now, 32 if 30 <= now % 60 < 45 else 30)
                now, _ = self.first_release(controller, returning)
                confirmed = now + 3
                self.step(controller, confirmed, actual=9, nodes=returning(confirmed))
                for stamp in range(confirmed + 3, confirmed + 363, 3):
                    age = stamp - confirmed
                    cpu = (returning(confirmed)[0]['cpu_temp'] + age * .03 if shape == 'ramp'
                        else 30 + (2 + age // 60 * .2 if 30 <= stamp % 60 < 45 else 0))
                    decision = self.step(controller, stamp, actual=9, nodes=self.flat(stamp, cpu))
                    self.assertGreaterEqual(decision['level'], 9)
                self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_high_endpoint_pending_deadline_is_exact_and_never_renewed(self):
        for arrival in (False, True):
            with self.subTest(arrival=arrival):
                controller = self.controller()
                for now in range(0, 363, 3):
                    decision = self.step(controller, now, actual=16, nodes=self.flat(now, 40))
                    if decision['level'] < 16:
                        break
                self.assertEqual(decision['level'], 15)
                requested, deadline = now, decision['execution_trial']['original_deadline']
                self.assertEqual(deadline, requested + 60)
                for stamp in range(requested + 3, int(deadline), 3):
                    decision = self.step(controller, stamp, actual=16, nodes=self.flat(stamp, 40),
                        readback=dict(confirmed=False, execution='pending'))
                    self.assertEqual(decision['level'], 15)
                    self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
                    self.assertEqual(decision['operating']['observe_credit'], 0)
                decision = self.step(controller, deadline, actual=15 if arrival else 16,
                    nodes=self.flat(deadline, 40), readback={} if arrival else dict(confirmed=False, execution='pending'))
                self.assertEqual(decision['execution_trial']['status'], 'execution_timeout')
                self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
                self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_mode_switch_restarts_observation_without_new_epoch(self):
        controller = self.controller()
        self.prime(controller, 180)
        decision = self.step(controller, 183, nodes=self.flat(183), manual_level=10)
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['operating']['observe_credit'], 0)
        for now in range(186, 366, 3):
            self.assertEqual(self.step(controller, now, nodes=self.flat(now))['level'], 10)
        self.assertEqual(self.step(controller, 366, nodes=self.flat(366))['level'], 9)

    def test_unsolicited_actual_change_restarts_observation(self):
        controller = self.controller()
        self.prime(controller, 180)
        decision = self.step(controller, 183, actual=9, nodes=self.flat(183))
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['operating']['observe_credit'], 0)
        for now in range(186, 363, 3):
            self.assertEqual(self.step(controller, now, actual=9, nodes=self.flat(now))['level'], 9)

    def test_failed_floor_survives_bounded_noise_without_rehabilitation(self):
        controller = self.controller()
        now, _ = self.first_release(controller)
        self.step(controller, now + 3, actual=9)
        decision = self.step(controller, now + 6, actual=9, nodes=self.flat(now + 6, 58))
        self.assertTrue(decision['retry_blocked'])
        self.assertEqual(decision['level'], 10)
        failure = deepcopy(decision['operating']['failed_floor'])
        for stamp in range(now + 9, now + 609, 3):
            decision = self.step(controller, stamp)
            self.assertEqual(decision['level'], 10)
            self.assertEqual(decision['operating']['failed_floor'], failure)

    def test_unsafe_destination_confirmation_preempts_release(self):
        controller = self.controller()
        now, _ = self.first_release(controller)
        decision = self.step(controller, now + 3, actual=9, nodes=self.flat(now + 3, 75))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['demand'], 10)
        self.assertTrue(decision['retry_blocked'])
        self.assertTrue(controller.export_thermal_memory()['failures'])
        self.assertEqual(controller.motion.actual_c, 75)

    def test_wifi_disappearance_freezes_release(self):
        controller = self.controller()
        for now in range(0, 183, 3):
            nodes = self.flat(now)
            nodes[0]['sensors'].append(dict(id='wifi:existing', group='wifi',
                label='wifi', temp=40, calibrated=True))
            self.step(controller, now, nodes=nodes)
        decision = self.step(controller, 183, nodes=self.flat(183))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['operating']['observe_credit'], 0)


if __name__ == '__main__':
    unittest.main()
