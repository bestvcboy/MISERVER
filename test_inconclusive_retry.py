import unittest
from copy import deepcopy
import test_core

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback


class InconclusiveRetryTests(unittest.TestCase):
    def step(self, controller, now, actual, readings=(('wifi', 50),), nodes=None, **options):
        readback = options.pop('feedback', feedback(now, actual))
        return controller.decide(test_core.ControlTests().sensor_fleet(now, readings) if nodes is None else nodes,
                                 now, minimum_level=0, feedback=readback, **options)

    def pending(self, target=9, **values):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 2400, 3):
            decision = self.step(controller, now, actual, nodes=test_core.ControlTests().sensor_fleet(now, **values))
            actual = decision['level']
            if actual == target:
                self.assertEqual(decision['trial']['status'], 'pending')
                return controller, now
        self.fail('no adjacent probe request')

    def timeout(self, controller=None, requested=None, target=9):
        if controller is None:
            controller, requested = self.pending(target)
        for age in range(3, 304, 3):
            now = requested + age
            decision = self.step(controller, now, target,
                                 readings=(('wifi', 50),) if age == 3 or age % 15 == 0 else ())
            if age < 303:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertEqual(decision['level'], max(10, target + 1))
        self.assertFalse(decision['retry_blocked'])
        self.assertFalse(decision['learning_eligible'])
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
        return controller, now

    def retry(self, controller, rolled_back):
        for age in range(3, 157, 3):
            now = rolled_back + age
            decision = self.step(controller, now, 10)
            self.assertFalse(decision['learning_eligible'])
            self.assertEqual(decision['level'], 10 if age < 156 else 9)
        self.assertEqual(decision['trial']['status'], 'pending')
        self.assertFalse(decision['retry_blocked'])
        return now

    def assert_requalifies(self, controller, now, actual=10, **values):
        for age in range(3, 903, 3):
            stamp = now + age
            nodes = test_core.ControlTests().sensor_fleet(stamp, **values)
            decision = self.step(controller, stamp, actual, nodes=nodes)
            actual = decision['level']
            self.assertFalse(decision['retry_blocked'])
            self.assertFalse(decision['learning_eligible'])
            if actual == 9:
                self.assertGreaterEqual(age, 150)
                self.assertEqual((decision['trial']['from'], decision['trial']['to']), (10, 9))
                self.assertEqual(decision['trial']['status'], 'pending')
                return stamp, decision
            self.assertGreaterEqual(actual, 10)
        self.fail('safe current source evidence did not admit a new adjacent trial')

    def test_complete_cold_telemetry_requalifies_inconclusive_probe(self):
        self.recovered_probe(0)

    def test_settled_rebound_before_sampling_timeout_can_requalify(self):
        self.recovered_probe(2)

    def recovered_probe(self, rise):
        controller, actual = Controller(HOSTS), 16
        fixture = test_core.ControlTests()
        for now in range(0, 1000, 3):
            decision = controller.decide(fixture.sensor_fleet(now), now, minimum_level=0,
                                         feedback=feedback(now, actual))
            actual = decision['level']
            if actual == 9:
                break
        self.assertEqual(actual, 9)
        requested = now
        warming_seen = False
        for age in range(3, 304, 3):
            now = requested + age
            readings = (('wifi', 50),) if age == 3 or age % 15 == 0 else ()
            cpu = 51 + rise * min(age / 120, 1)
            decision = controller.decide(fixture.sensor_fleet(now, readings, cpu=cpu), now,
                                         minimum_level=0, feedback=feedback(now, 9))
            warming_seen |= age >= 123 and decision['scenario'] == 'warming_hold'
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertEqual(decision['level'], 10)
        if rise:
            self.assertTrue(warming_seen, 'Fixture must exercise resolved mature rebound')
        actual = 10
        for stamp in range(now + 3, now + 1803, 3):
            decision = controller.decide(fixture.sensor_fleet(stamp, cpu=51 + rise), stamp, minimum_level=0,
                                         feedback=feedback(stamp, actual))
            actual = decision['level']
            if actual < 10:
                break
        self.assertEqual(actual, 9, 'Recovered complete cold telemetry never retried the inconclusive edge')
        self.assertEqual(decision['trial']['status'], 'pending')
        self.assertFalse(decision['learning_eligible'])

    def test_retry_requires_fresh_physical_source_window_then_can_succeed_and_descend(self):
        controller, now = self.timeout()
        for age in range(3, 61, 3):
            decision = self.step(controller, now + age, 9)
            self.assertEqual(decision['level'], 10 if age < 60 else 16)
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertEqual(decision['execution_trial']['outcome'], 'execution_timeout')
        self.assertEqual(decision['execution_trial']['original_deadline'], now + 60)
        requested, decision = self.assert_requalifies(controller, now + 60, actual=16)
        for age in range(3, 124, 3):
            decision = self.step(controller, requested + age, 9)
            self.assertEqual(decision['level'], 9)
            self.assertFalse(decision['learning_eligible'])
            self.assertEqual(decision['trial']['outcome'], 'success' if age == 123 else None)
        for age in range(126, 184, 3):
            decision = self.step(controller, requested + age, 9)
        self.assertEqual(decision['level'], 8)
        self.assertEqual(decision['trial']['status'], 'pending')
        self.assertFalse(decision['learning_eligible'])

    def test_sparse_telemetry_never_requalifies_and_recovery_needs_full_window(self):
        controller, now = self.timeout()
        for age in range(3, 904, 3):
            decision = self.step(controller, now + age, 10,
                                 readings=(('wifi', 50),) if age == 3 or age % 15 == 0 else ())
            self.assertEqual(decision['level'], 10)
            self.assertFalse(decision['retry_blocked'])
        for age in range(906, 1051, 3):
            decision = self.step(controller, now + age, 10)
            self.assertEqual(decision['level'], 10 if age < 1050 else 9)
            self.assertFalse(decision['learning_eligible'])
        self.assertEqual(decision['trial']['status'], 'pending')

    def test_grace_and_repeated_feedback_retain_samples_but_cannot_authorize(self):
        base, rolled_back = self.timeout()
        for event in ('wifi', 'feedback'):
            with self.subTest(event=event):
                controller = deepcopy(base)
                for age in range(3, 157, 3):
                    now = rolled_back + age
                    options = {}
                    if age == 153:
                        if event == 'wifi':
                            options['readings'] = ()
                        else:
                            options['feedback'] = feedback(now - 3, 10)
                    decision = self.step(controller, now, 10, **options)
                    self.assertEqual(decision['level'], 10)
                    self.assertFalse(decision['learning_eligible'])
                decision = self.step(controller, now + 3, 10)
                self.assertEqual(decision['level'], 10)
                decision = self.step(controller, now + 6, 10)
                self.assertEqual(decision['level'], 9)
                self.assertEqual(decision['trial']['status'], 'pending')

    def test_source_wifi_gap_and_density_limits_are_unchanged(self):
        base, rolled_back = self.timeout()
        for interval in (9, 12):
            with self.subTest(interval=interval):
                controller = deepcopy(base)
                for age in range(3, 184, 3):
                    present = (age - 3) % interval == 0
                    decision = self.step(controller, rolled_back + age, 10,
                                         readings=(('wifi', 50),) if present else ())
                    self.assertFalse(decision['learning_eligible'])
                    if decision['level'] == 9:
                        self.assertEqual(interval, 9)
                        self.assertEqual(age, 156)
                        self.assertTrue(present)
                        break
                    self.assertEqual(decision['level'], 10)
                self.assertEqual(decision['level'], 10)
                resume = rolled_back + 183
                for age in range(3, 154, 3):
                    decision = self.step(controller, resume + age, 10)
                    if decision['level'] == 9:
                        self.assertGreaterEqual(age, 60)
                        break
                self.assertEqual(decision['level'], 9)

    def test_each_interruption_preserves_or_rebuilds_current_source_window(self):
        base, rolled_back = self.timeout()
        for event in ('source', 'identity', 'manual', 'minimum', 'actual', 'pending', 'stale',
                      'clock', 'timestamp', 'power_unknown', 'power_high', 'load', 'thermal', 'protect'):
            with self.subTest(event=event):
                controller, actual, options = deepcopy(base), 10, {}
                for age in range(3, 64, 3):
                    self.assertEqual(self.step(controller, rolled_back + age, actual)['level'], 10)
                now = rolled_back + 66
                nodes = test_core.ControlTests().sensor_fleet(now)
                readback = feedback(now, 10)
                if event == 'source':
                    nodes[0]['power_source'] = 'changed'
                elif event == 'identity':
                    nodes[0]['sensors'].append(dict(nodes[0]['sensors'][0], id='new:cpu'))
                elif event == 'manual':
                    options['manual_level'] = 0
                elif event == 'minimum':
                    options['minimum_level'] = 1
                elif event == 'actual':
                    readback = feedback(now, 11)
                elif event == 'pending':
                    readback = feedback(now, 10, execution='pending', confirmed=False)
                elif event == 'stale':
                    readback = feedback(now - 10, 10)
                elif event == 'clock':
                    now += 6
                    nodes = test_core.ControlTests().sensor_fleet(now)
                    readback = feedback(now, 10)
                elif event == 'timestamp':
                    nodes[0]['source_ts'] -= 3
                elif event == 'power_unknown':
                    nodes[0]['power_w'] = None
                elif event == 'power_high':
                    nodes[0]['power_w'] = 80
                elif event == 'load':
                    nodes[0]['cpu_load'] = 35
                elif event == 'thermal':
                    nodes = test_core.ControlTests().sensor_fleet(now, cpu=50.5)
                else:
                    controller.protect('external interruption')
                options.setdefault('minimum_level', 0)
                decision = controller.decide(nodes, now, feedback=readback, **options)
                self.assertGreaterEqual(decision['level'], 10)
                actual = decision['level']
                fresh_source = None
                for age in range(3, 1503, 3):
                    stamp = now + age
                    nodes = test_core.ControlTests().sensor_fleet(stamp)
                    if event == 'identity':
                        nodes[0]['sensors'].append(dict(nodes[0]['sensors'][0], id='new:cpu'))
                    source_window = controller.gear_window
                    decision = self.step(controller, stamp, actual, nodes=nodes)
                    if actual == 10 and fresh_source is None:
                        fresh_source = stamp
                    if decision['level'] == 9:
                        self.assertIsNotNone(fresh_source)
                        self.assertIsNotNone(source_window)
                        self.assertGreaterEqual(stamp - source_window['since'], 150)
                        self.assertEqual(decision['trial']['status'], 'pending')
                        self.assertFalse(decision['learning_eligible'])
                        break
                    actual = decision['level']
                self.assertEqual(decision['level'], 9)

    def test_each_timeout_invalidation_and_failure_requires_current_requalification(self):
        base, rolled_back = self.timeout()
        requested = self.retry(base, rolled_back)
        for outcome in ('timeout', 'pending', 'protect', 'mode', 'source', 'thermal'):
            with self.subTest(outcome=outcome):
                controller = deepcopy(base)
                if outcome == 'timeout':
                    controller, now = self.timeout(controller, requested)
                    actual = 10
                elif outcome == 'pending':
                    for age in range(3, 61, 3):
                        now = requested + age
                        decision = self.step(controller, now, 10)
                    self.assertEqual(decision['trial']['outcome'], 'execution_timeout')
                    actual = 10
                else:
                    now = requested + 3
                    self.step(controller, now, 9)
                    now += 3
                    nodes = test_core.ControlTests().sensor_fleet(now, cpu=73 if outcome == 'thermal' else 51)
                    options = {}
                    if outcome == 'protect':
                        controller.protect('external interruption')
                    elif outcome == 'mode':
                        options['manual_level'] = 0
                    elif outcome == 'source':
                        nodes[0]['power_source'] = 'changed'
                    decision = self.step(controller, now, 9, nodes=nodes, **options)
                    self.assertEqual(decision['trial']['outcome'], 'failed' if outcome == 'thermal' else 'invalidated')
                    actual = decision['level']
                now, decision = self.assert_requalifies(controller, now, actual)
                controller.protect('later fault requires another normal recovery')
                self.assert_requalifies(controller, now, 16, load=0, power=20)

    def test_success_and_later_timeout_still_require_fresh_current_evidence(self):
        controller, rolled_back = self.timeout()
        requested = self.retry(controller, rolled_back)
        for age in range(3, 124, 3):
            decision = self.step(controller, requested + age, 9)
        self.assertEqual(decision['trial']['outcome'], 'success')
        self.assertFalse(decision['retry_blocked'])
        now = requested + 123
        controller.protect('rebuild normal authority after success')
        actual = 16
        for age in range(3, 1203, 3):
            stamp = now + age
            decision = self.step(controller, stamp, actual)
            actual = decision['level']
            if actual == 9:
                break
        self.assertEqual(actual, 9)
        controller, now = self.timeout(controller, stamp)
        self.assert_requalifies(controller, now)

    def test_lower_edge_needs_successful_adjacent_authority_and_own_fresh_window(self):
        controller, now = self.timeout(target=8)
        actual, source_since, succeeded = 10, None, False
        for age in range(3, 1803, 3):
            stamp = now + age
            decision = self.step(controller, stamp, actual)
            if decision['trial'] and decision['trial']['to'] == 9:
                if source_since is None:
                    source_since = stamp
                succeeded |= decision['trial']['outcome'] == 'success'
            if decision['level'] == 8:
                self.assertTrue(succeeded)
                self.assertGreaterEqual(stamp - source_since, 180)
                self.assertFalse(decision['learning_eligible'])
                break
            actual = decision['level']
        self.assertEqual(decision['level'], 8)

    def test_adjacent_edges_require_separate_confirmation_and_success(self):
        controller, now = self.timeout(target=8)
        for age in range(3, 183, 3):
            stamp = now + age
            decision = self.step(controller, stamp, 10)
            if decision['level'] == 9:
                break
        self.assertEqual(decision['level'], 9)
        controller, now = self.timeout(controller, stamp)
        for age in range(3, 157, 3):
            decision = self.step(controller, now + age, 10)
            self.assertEqual(decision['level'], 10 if age < 156 else 9)
            self.assertFalse(decision['learning_eligible'])
        self.assertFalse(decision['retry_blocked'])
        requested = now + 156
        for age in range(3, 217, 3):
            decision = self.step(controller, requested + age, 9)
            if decision['level'] == 8:
                break
        self.assertEqual(decision['level'], 8)
        self.assertEqual(decision['trial']['status'], 'pending')
        self.assertFalse(decision['retry_blocked'])
        self.assertEqual((decision['trial']['from'], decision['trial']['to']), (9, 8))
        self.assertIsNone(decision['trial']['outcome'])
        self.assertGreaterEqual(age, 183)

    def test_true_thermal_failure_requires_full_safe_recovery_before_requalification(self):
        controller, requested = self.pending()
        self.step(controller, requested + 3, 9)
        now = requested + 6
        nodes = test_core.ControlTests().sensor_fleet(now, cpu=73)
        decision = self.step(controller, now, 9, nodes=nodes)
        self.assertEqual(decision['trial']['outcome'], 'failed')
        self.assertEqual(decision['trial']['outcome'], 'failed')
        self.assert_requalifies(controller, now, decision['level'])

    def test_unresolved_sensor_heat_cannot_be_cleared_by_missing_or_other_settled_sensors(self):
        for shape in ('cpu_continuing', 'wifi_obstacle_then_sparse', 'projected_margin'):
            with self.subTest(shape=shape):
                controller, requested = self.pending(cpu=57 if shape == 'projected_margin' else 51)
                for age in range(3, 304, 3):
                    now = requested + age
                    present = age == 3 or age % 15 == 0
                    wifi = 50
                    cpu = 51
                    if shape == 'cpu_continuing':
                        cpu = 51 + .016 * age
                    elif shape == 'wifi_obstacle_then_sparse':
                        present |= age <= 150
                        wifi = 50 + min(age / 120, 1) * 3
                    else:
                        cpu = 57.1
                    nodes = test_core.ControlTests().sensor_fleet(now, (('wifi', wifi),) if present else (), cpu=cpu)
                    decision = self.step(controller, now, 9, nodes=nodes)
                    if age < 303:
                        self.assertIsNone(decision['trial']['outcome'])
                self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
                self.assertEqual(decision['level'], 10)
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
                self.assertFalse(decision['retry_blocked'])
                self.assertIn('cause=nonsettled', ' '.join(decision['reasons']))
                self.assert_requalifies(controller, now)

    def test_target_risk_on_deadline_precedes_missing_sampling_timeout(self):
        controller, requested = self.pending()
        for age in range(3, 304, 3):
            now = requested + age
            nodes = test_core.ControlTests().sensor_fleet(
                now, (('wifi', 50),) if age == 3 or age % 15 == 0 else (),
                cpu=51 + .06 * max(0, age - 215))
            decision = self.step(controller, now, 9, nodes=nodes)
            if age < 303:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'failed')
        self.assertEqual(decision['trial']['outcome'], 'failed')
        self.assertIn('[target]', ' '.join(decision['reasons']))
        self.assertNotIn('[hard]', ' '.join(decision['reasons']))
        self.assertFalse(decision['learning_eligible'])
        self.assert_requalifies(controller, now, decision['level'])

    def test_partial_coverage_rebound_does_not_invent_a_measured_settlement_obstacle(self):
        controller, requested = self.pending()
        for age in range(3, 304, 3):
            now = requested + age
            present = age == 3 or age % 15 == 0
            wifi = 50 + min(age / 120, 1) * 3
            decision = self.step(controller, now, 9, readings=(('wifi', wifi),) if present else ())
            if age < 303:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertIn('cause=missing_samples', ' '.join(decision['reasons']))
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
        self.retry(controller, now)

    def test_repeated_deadline_feedback_is_an_independent_inconclusive_blocker(self):
        controller, requested = self.pending()
        for age in range(3, 304, 3):
            now = requested + age
            decision = self.step(controller, now, 9,
                                 readings=(('wifi', 50),) if age == 3 or age % 15 == 0 else (),
                                 feedback=feedback(now - (3 if age == 303 else 0), 9))
            if age < 303:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertEqual(decision['level'], 10)
        self.assertIn('cause=nonsettled', ' '.join(decision['reasons']))
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
        self.assert_requalifies(controller, now)

    def test_current_uncertainty_or_isolated_load_precludes_sampling_only_deadline(self):
        for event in ('uncertainty', 'load'):
            with self.subTest(event=event):
                controller, requested = self.pending()
                for age in range(3, 304, 3):
                    now = requested + age
                    nodes = test_core.ControlTests().sensor_fleet(
                        now, (('wifi', 50),) if age == 3 or age % 15 == 0 else (),
                        cpu=55.9 if age == 303 and event == 'uncertainty' else 51,
                        load=35 if age == 303 and event == 'load' else 0)
                    decision = self.step(controller, now, 9, nodes=nodes)
                    if age < 303:
                        self.assertIsNone(decision['trial']['outcome'])
                self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
                self.assertEqual(decision['level'], 10)
                self.assertIn('cause=nonsettled', ' '.join(decision['reasons']))
                if event == 'uncertainty':
                    self.assertIn('[uncertain]', ' '.join(decision['reasons']))
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
                self.assert_requalifies(controller, now)


if __name__ == '__main__':
    unittest.main()
