import unittest
from copy import deepcopy
import test_core

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet, heat_to_target_risk


class LowerAirflowTests(unittest.TestCase):
    def test_completed_release_cannot_regain_attribution_after_interruption(self):
        from test_scenarios import ScenarioTests
        for event in ('source', 'manual', 'actual', 'protect', 'feedback', 'pending', 'power'):
            with self.subTest(event=event):
                fixture = ScenarioTests()
                controller = Controller(HOSTS)
                fixture.baseline(controller)
                fixture.step(controller, 63, actual=15)
                nodes, options, actual = fleet(66), {}, 15
                if event == 'source':
                    nodes[-1]['power_source'] = 'changed'
                elif event == 'manual':
                    options['manual_level'] = 15
                elif event == 'actual':
                    actual = 16
                elif event == 'protect':
                    controller.protect('external storage failure')
                elif event == 'feedback':
                    options['readback'] = feedback(66, 15, fault=True)
                elif event == 'pending':
                    options['readback'] = feedback(66, 15, execution='pending', confirmed=False)
                else:
                    nodes[-1]['power_w'] = None
                fixture.step(controller, 66, actual=actual, nodes=nodes, **options)
                options.pop('readback', None)
                decision = fixture.step(controller, 69, actual=15, nodes=fleet(69, cpu=73), **options)
                self.assertEqual(decision['level'], 16)
                self.assertEqual(decision['trial']['outcome'], 'invalidated', event)

    def test_failed_probe_history_cannot_authorize_learning_above_current_demand(self):
        from learning import Learning
        from test_learning import frame
        from test_scenarios import ScenarioTests
        fixture = ScenarioTests()
        controller, requested = fixture.probe()
        now, decision = heat_to_target_risk(controller, requested)
        self.assertGreaterEqual(decision['level'], 10)
        self.assertEqual(decision['trial']['outcome'], 'failed')
        learner = Learning(HOSTS)
        actual = decision['level']
        for stamp in range(now + 3, now + 603, 3):
            nodes = fleet(stamp)
            decision = fixture.step(controller, stamp, actual=actual, nodes=nodes)
            self.assertFalse(decision['learning_eligible'])
            row = frame(stamp, gear=actual)
            row.update(nodes=nodes, decision=decision)
            learner.observe(row)
            actual = decision['level']
        self.assertLess(actual, 10)
        self.assertEqual(sum(bucket['n'] for bucket in learner.buckets.values()), 0)

    def step(self, controller, now, actual=16, nodes=None, readback=None, **options):
        return controller.decide(fleet(now) if nodes is None else nodes, now,
                                 minimum_level=0, feedback=feedback(now, actual) if readback is None else readback,
                                 **options)

    def pending_probe(self, **values):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1000, 3):
            decision = self.step(controller, now, actual, fleet(now, **values))
            if decision['level'] == 9:
                self.assertEqual(actual, 10)
                self.assertEqual(decision['trial']['kind'], 'probe')
                return controller, now
            actual = decision['level']
        self.fail('strict idle did not admit the first probe')

    def test_release_uses_thirty_safe_seconds_and_post_request_readback(self):
        controller = Controller(HOSTS)
        for now in range(0, 61, 3):
            decision = self.step(controller, now, nodes=fleet(now, load=18))
            if decision['level'] == 15:
                break
        self.assertEqual(decision['level'], 15)
        self.assertLessEqual(now, 60)
        requested = now
        self.assertEqual(decision['trial']['kind'], 'release')
        decision = self.step(controller, now + 3, readback=feedback(now, 15))
        self.assertIsNone(decision['trial']['outcome'])
        decision = self.step(controller, now + 6, 15)
        self.assertEqual(decision['trial']['outcome'], 'recovery_released')
        self.assertFalse(decision['learning_eligible'])
        for age in range(9, 36, 3):
            self.assertEqual(self.step(controller, requested + age, 15)['level'], 15)
        self.assertEqual(self.step(controller, requested + 36, 15)['level'], 14)

    def test_both_pending_deadlines_do_not_lock_thermal_edges(self):
        for kind in ('release', 'probe'):
            controller, requested = self.pending_probe() if kind == 'probe' else (Controller(HOSTS), None)
            if kind == 'release':
                for now in range(0, 64, 3):
                    decision = self.step(controller, now)
                    if decision['level'] == 15:
                        requested = now
                        break
                self.assertIsNotNone(requested)
            source = 10 if kind == 'probe' else 16
            for now in range(requested + 3, requested + 61, 3):
                decision = self.step(controller, now, source)
                if now < requested + 60:
                    self.assertEqual(decision['level'], source - 1)
            self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['trial']['outcome'], 'execution_timeout')
            self.assertFalse(decision['retry_blocked'])

    def test_isolated_load_does_not_cancel_release_but_sustained_load_preempts_probe(self):
        controller = Controller(HOSTS)
        for now in range(0, 64, 3):
            decision = self.step(controller, now)
            if decision['level'] == 15:
                break
        decision = self.step(controller, now + 3, nodes=fleet(now + 3, load=70))
        self.assertEqual(decision['level'], 15)
        self.assertIsNone(decision['trial']['outcome'])
        for component in ('cpu_load', 'gpu_load'):
            controller, requested = self.pending_probe()
            self.step(controller, requested + 3, 9)
            for age in (6, 9, 12):
                nodes = fleet(requested + age)
                nodes[-1][component] = 70
                decision = self.step(controller, requested + age, 9, nodes)
            self.assertEqual(decision['level'], 10)
            self.assertEqual(decision['trial']['outcome'], 'invalidated')
            self.assertFalse(decision['retry_blocked'])

    def test_cold_derivative_qualification_holds_impulses_and_allows_bounded_wifi_grace(self):
        for kind in ('isolated', 'sustained', 'cold_raw_projection', 'wifi'):
            controller = Controller(HOSTS)
            for now in range(0, 61, 3):
                high = 55 if kind == 'cold_raw_projection' else 48
                temp = high if now in (0, 30, 60) else 45
                if kind == 'sustained':
                    temp = 48 if now in (0, 30, 60) else 46.5 if now == 57 else 45
                nodes = (test_core.ControlTests().sensor_fleet(now, (('wifi', temp),)) if kind == 'wifi'
                         else fleet(now, cpu=temp))
                decision = self.step(controller, now, nodes=nodes)
            if kind in ('isolated', 'wifi', 'cold_raw_projection'):
                self.assertNotEqual(decision['scenario'], 'protect')
                self.assertIn('qualified', ' '.join(decision['reasons']))
            else:
                self.assertEqual(decision['level'], 16)
                self.assertEqual(decision['scenario'], 'protect')
                self.assertIn('earned=27/30s', decision['scenario_detail'])
                self.assertFalse(any('[hard]' in reason for reason in decision['reasons']))
            if kind == 'wifi':
                decision = self.step(controller, 63, nodes=test_core.ControlTests().sensor_fleet(63, ()))
                self.assertEqual(decision['level'], 15)
                self.assertIn('Wi-Fi', ' '.join(decision['reasons']))
                self.assertNotIn('传感器消失', ' '.join(decision['reasons']))
                self.assertIsNone(decision['trial']['outcome'])

    def test_tiny_rise_then_single_jump_is_not_two_fast_intervals(self):
        controller = Controller(HOSTS)
        for now in range(0, 61, 3):
            cpu = 48.03 if now == 60 else 45.03 if now == 57 else 45
            decision = self.step(controller, now, nodes=fleet(now, cpu=cpu))
        self.assertEqual(decision['level'], 16)
        self.assertIn('qualified derivative-only', ' '.join(decision['reasons']))
        self.assertIn('soft=0.1010', ' '.join(decision['reasons']))

    def test_cold_qualification_preserves_long_rate_and_soft_margin(self):
        for shape in ('jump', 'margin'):
            controller = Controller(HOSTS)
            for now in range(0, 61, 3):
                cpu = (40 + .1 * now + (3 if now == 60 else 0) if shape == 'jump'
                       else 40.54 + .18 * now + (1 if now == 60 else 0))
                decision = self.step(controller, now, nodes=fleet(now, cpu=cpu))
            self.assertNotEqual(decision['scenario'], 'protect')
            self.assertIn('qualified derivative-only', ' '.join(decision['reasons']))
            if shape == 'jump':
                self.assertEqual(decision['level'], 16)
                self.assertIn('soft=0.2000', ' '.join(decision['reasons']))
            else:
                self.assertEqual(decision['level'], 16)
                self.assertIsNone(decision['trial'])
                self.assertIn('soft_projection=58.740<=58C', ' '.join(decision['reasons']))

    def test_quantized_probe_settles_after_actual_confirmation(self):
        controller, requested = self.pending_probe()
        for age in range(3, 124, 3):
            cpu = 51 + (age // 3) % 2
            decision = self.step(controller, requested + age, 9, fleet(requested + age, cpu=cpu))
            self.assertEqual(decision['level'], 9)
            self.assertFalse(decision['learning_eligible'])
            if age < 123:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'success')

    def test_separate_settled_rebounds_can_cross_old_relative_anchor(self):
        controller, requested = self.pending_probe()
        for age in range(3, 124, 3):
            decision = self.step(controller, requested + age, 9, fleet(requested + age, cpu=52.5))
        self.assertEqual(decision['trial']['outcome'], 'success')
        for age in range(126, 184, 3):
            decision = self.step(controller, requested + age, 9, fleet(requested + age, cpu=52.5))
        self.assertEqual(decision['level'], 8)
        self.step(controller, requested + 186, 8, fleet(requested + 186, cpu=52.5))
        decision = self.step(controller, requested + 189, 8, fleet(requested + 189, cpu=53.1))
        self.assertEqual(decision['level'], 8)
        self.assertIsNone(decision['trial']['outcome'])
        for age in range(192, 310, 3):
            decision = self.step(controller, requested + age, 8, fleet(requested + age, cpu=53.1))
            self.assertEqual(decision['level'], 8)
        self.assertEqual(decision['trial']['outcome'], 'success')
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_unsettled_probe_has_absolute_observation_deadline(self):
        fixture = test_core.ControlTests()
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1000, 3):
            decision = self.step(controller, now, actual, fixture.sensor_fleet(now))
            actual = decision['level']
            if actual == 9:
                requested = now
                break
        for age in range(3, 304, 3):
            beyond_coverage_wifi_readings = (('wifi', 50),) if age == 3 or age % 15 == 0 else ()
            stamp = requested + age - (3 if age == 303 else 0)
            decision = self.step(controller, requested + age, 9,
                                 fixture.sensor_fleet(requested + age, beyond_coverage_wifi_readings), feedback(stamp, 9))
            if age < 303:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertEqual(decision['level'], 10)
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
        self.assertFalse(decision['retry_blocked'])
        self.assertFalse(decision['learning_eligible'])

    def test_target_risk_in_pending_and_confirmed_probe_keeps_attribution(self):
        for confirmed in (False, True):
            controller, requested = self.pending_probe(cpu=55)
            for age in range(3, 40, 3):
                now = requested + age
                decision = self.step(controller, now, 9 if confirmed else 10,
                                     fleet(now, cpu=55 + (age - 3) * .06))
                if decision['level'] >= 10 and decision['scenario'] != 'thermal_uncertain':
                    break
            self.assertEqual(decision['level'], 10)
            self.assertEqual(decision['trial']['outcome'], 'failed' if confirmed else 'invalidated')
            self.assertEqual(decision['trial']['outcome'], 'failed' if confirmed else 'invalidated')

    def test_release_freezes_missing_power_and_restarts_source_or_mode_change(self):
        for change in ('power', 'source', 'mode'):
            controller = Controller(HOSTS)
            for now in range(0, 49, 3):
                self.step(controller, now)
            for now in range(51, 61, 3):
                nodes = fleet(now)
                options = {}
                if change == 'power':
                    nodes[-1]['power_w'] = None
                elif change == 'source':
                    nodes[-1]['power_source'] = 'changed'
                else:
                    options['manual_level'] = 0
                self.assertEqual(self.step(controller, now, nodes=nodes, **options)['level'], 16)
            for now in range(63, 94, 3):
                nodes = fleet(now)
                if change == 'source':
                    nodes[-1]['power_source'] = 'changed'
                decision = self.step(controller, now, nodes=nodes, **({'manual_level': 0} if change == 'mode' else {}))
                if decision['level'] == 15:
                    break
            self.assertEqual(now, 93 if change == 'power' else 84 if change == 'mode' else 81)
            self.assertEqual(decision['level'], 15)

    def test_isolated_spike_pauses_new_release_without_resetting_dwell(self):
        controller = Controller(HOSTS)
        for now in range(0, 63, 3):
            nodes = fleet(now, load=70 if now == 60 else 0)
            decision = self.step(controller, now, nodes=nodes)
            self.assertEqual(decision['level'], 16)
        self.assertEqual(self.step(controller, 63)['level'], 16)
        decision = self.step(controller, 66)
        self.assertEqual(decision['level'], 15)

    def test_independent_device_timeline_confirms_one_step_only(self):
        controller = Controller(HOSTS)
        requests, outcomes = [], []
        prior = 16
        for now in range(0, 174, 3):
            stamp = now - now % 6
            actual = 16 if stamp < 84 else 15 if stamp < 132 else 14
            decision = self.step(controller, now, actual, readback=feedback(stamp, actual))
            if decision['level'] != prior:
                requests.append((now, decision['level']))
                self.assertEqual(decision['level'], actual - 1)
                prior = decision['level']
            if decision['scenario'] == 'recovery_released':
                outcomes.append((now, actual))
        self.assertEqual(requests, [])
        self.assertEqual(outcomes, [])
        for now in range(174, 235, 3):
            decision = self.step(controller, now, 16)
        self.assertEqual(decision['level'], 15)

    def test_confirmed_probe_gear_change_on_duplicate_invalidates_evidence(self):
        controller, requested = self.pending_probe()
        self.step(controller, requested + 3, 9)
        decision = self.step(controller, requested + 6, readback=feedback(requested + 3, 10))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertFalse(decision['retry_blocked'])

    def test_release_allows_safe_warming_but_never_grants_learning(self):
        controller = Controller(HOSTS)
        actual = 16
        for now in range(0, 211, 3):
            decision = self.step(controller, now, actual, fleet(now, cpu=40 + .03 * now))
            actual = decision['level']
            self.assertFalse(decision['learning_eligible'])
        self.assertLessEqual(actual, 14)
        self.assertTrue(any('warming' in reason for reason in decision['reasons']))

    def test_unexpected_actual_increase_restarts_release_gear_dwell(self):
        controller = Controller(HOSTS)
        for now in range(0, 61, 3):
            self.step(controller, now)
        for now in range(63, 93, 3):
            self.step(controller, now, 15)
        decision = self.step(controller, 93, 16)
        self.assertEqual(decision['level'], 16)
        for now in range(96, 123, 3):
            self.assertEqual(self.step(controller, now)['level'], 16)
        self.assertEqual(self.step(controller, 123)['level'], 15)

    def test_safe_wifi_grace_retains_release_dwell_but_cannot_finish_it(self):
        controller = Controller(HOSTS)
        fixture = test_core.ControlTests()
        for now in range(0, 61, 3):
            readings = () if now == 60 else (('wifi', 50),)
            decision = self.step(controller, now, nodes=fixture.sensor_fleet(now, readings))
            self.assertEqual(decision['level'], 16)
        self.assertEqual(self.step(controller, 63, nodes=fixture.sensor_fleet(63))['level'], 16)
        decision = self.step(controller, 66, nodes=fixture.sensor_fleet(66))
        self.assertEqual(decision['level'], 15)
        decision = self.step(controller, 69, 15, fixture.sensor_fleet(69, ()))
        self.assertIsNone(decision['trial']['outcome'])
        decision = self.step(controller, 72, 15, fixture.sensor_fleet(72))
        self.assertEqual(decision['trial']['outcome'], 'recovery_released')

    def test_probe_wifi_coverage_cannot_be_invented_by_elapsed_time(self):
        controller, actual = Controller(HOSTS), 16
        fixture = test_core.ControlTests()
        for now in range(0, 1000, 3):
            decision = self.step(controller, now, actual, fixture.sensor_fleet(now))
            actual = decision['level']
            if actual == 9:
                break
        requested = now
        for age in range(3, 124, 3):
            now = requested + age
            readings = () if age % 15 else (('wifi', 50),)
            decision = self.step(controller, now, 9, fixture.sensor_fleet(now, readings))
            self.assertIsNone(decision['trial']['outcome'])
        self.assertFalse(decision['learning_eligible'])

    def test_cold_noisy_idle_can_leave_full_speed_and_reach_routine_band(self):
        controller = Controller(HOSTS)
        actual = 16
        for now in range(0, 1801, 3):
            nodes = fleet(now, cpu=42, gpu=39, board=47, power=55)
            for index, node in enumerate(nodes):
                node['cpu_load'] = (3, 8, 18, 5, 7, 32, 4)[(now // 3 + index) % 7]
                node['gpu_load'] = 0
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, actual))
            actual = decision['level']
        self.assertLessEqual(actual, 10, 'cold background load must not indefinitely latch startup level16')


if __name__ == '__main__':
    unittest.main()
