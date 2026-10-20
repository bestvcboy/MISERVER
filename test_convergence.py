import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class ExecutionConvergenceTests(unittest.TestCase):
    def step(self, controller, now, actual=10, cpu=45):
        return controller.decide(fleet(now, cpu=cpu, gpu=45, board=50), now,
            minimum_level=0, feedback=feedback(now, actual))

    def test_revoked_high_wind_ends_old_pending_execution(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        hot = self.step(controller, 100, cpu=80)
        self.assertEqual(hot['execution_trial']['to'], 11)
        original_deadline = hot['execution_trial']['original_deadline']
        cool = self.step(controller, 103)
        self.assertEqual(cool['level'], 10)
        self.assertNotEqual(cool['execution_trial']['status'], 'pending')
        self.assertEqual(cool['execution_trial']['original_deadline'], original_deadline)
        self.assertTrue(controller.feedback_matches_request(10, 104, 104))

    def test_protection_retires_incompatible_pending_operation(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(controller, 100, cpu=80)
        controller.high_wind = type(controller.high_wind)()
        decision = controller.protect('synthetic storage interruption', 103)
        self.assertEqual(decision['level'], 10)
        self.assertNotEqual(decision['execution_trial']['status'], 'pending')
        self.assertTrue(controller.feedback_matches_request(10, 104, 104))

    def test_compatible_pending_execution_keeps_original_deadline(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        first = self.step(controller, 100, cpu=80)
        later = self.step(controller, 103, cpu=80)
        self.assertEqual(later['execution_trial']['status'], 'pending')
        self.assertEqual(later['execution_trial']['original_deadline'],
                         first['execution_trial']['original_deadline'])
        self.assertFalse(controller.feedback_matches_request(10, 104, 104))

    def test_completed_ceiling_exit_accepts_only_post_request_readback(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        requested = self.step(controller, 100, actual=16)
        self.assertEqual(requested['level'], 15)
        completed = self.step(controller, 103, actual=15)
        trial = completed['execution_trial']
        self.assertEqual((trial['status'], trial['outcome']), ('confirmed', 'confirmed'))
        self.assertFalse(controller.feedback_matches_request(15, 100, 104))
        self.assertTrue(controller.feedback_matches_request(15, 104, 104))
        self.assertEqual(completed['execution_trial']['original_deadline'],
                         requested['execution_trial']['original_deadline'])

    def test_confirmation_outcome_cannot_qualify_pending_or_retired_intent(self):
        for status, pending in (('pending', True), ('confirmed', True), ('failed', False)):
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            self.step(controller, 100, actual=16)
            operation = controller._policy['operation']
            operation.update(status=status, outcome='confirmed', confirmed_at=103)
            if not pending:
                operation['episode'] = None
            with self.subTest(status=status, pending=pending):
                self.assertFalse(controller.feedback_matches_request(15, 104, 104))

    def test_expired_unresolved_execution_does_not_receive_new_deadlines(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        first = self.step(controller, 100, cpu=80)
        deadline = first['execution_trial']['original_deadline']
        for now in (160, 163, 166, 220):
            decision = self.step(controller, now, cpu=80)
            self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
            self.assertNotEqual(decision['execution_trial']['status'], 'pending')

    def test_revocation_from_actual_sixteen_holds_without_new_execution(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        first = self.step(controller, 100, cpu=80)
        decision = self.step(controller, 103, actual=16)
        self.assertEqual(decision['level'], 16)
        self.assertEqual(decision['execution_trial']['to'], 11)
        self.assertEqual(decision['execution_trial']['status'], 'invalidated')
        self.assertEqual(decision['execution_trial']['original_deadline'],
                         first['execution_trial']['original_deadline'])
        decision = self.step(controller, 106, actual=16)
        self.assertEqual(decision['level'], 15)
        self.assertEqual(decision['execution_trial']['purpose'], 'ceiling_exit')
        self.assertEqual(decision['execution_trial']['original_deadline'], 166)


class DescentConvergenceTests(unittest.TestCase):
    def step(self, controller, now, actual, *, cpu=49, load=5, power=60):
        return controller.decide(fleet(now, cpu=cpu, gpu=47, board=54, load=load, power=power),
            now, minimum_level=0, feedback=feedback(now, actual))

    def test_each_safe_descent_is_one_level_with_full_observation(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual, edges = 10, []
        for now in range(0, 600, 3):
            decision = self.step(controller, now, actual)
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertTrue(edges)
        self.assertGreaterEqual(edges[0][0], 180)
        self.assertTrue(all(before - after == 1 for _, before, after in edges), edges)
        self.assertTrue(all(second[0] - first[0] >= 180 for first, second in zip(edges, edges[1:])), edges)

    def test_prediction_below_actual_restore_threshold_does_not_fail_high_descent(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 46, 3):
            controller.decide(fleet(now, cpu=49, gpu=47, board=54, load=7.6, power=63.47), now,
                minimum_level=0, manual_level=10, feedback=feedback(now, 10))
        actual = 16
        for now in (48, 51, 54):
            decision = self.step(controller, now, actual, load=7.6, power=63.47)
            actual = decision['level']
        self.assertEqual((decision['scenario'], actual), ('ceiling_exit_motor_interval', 15))
        for now, cpu in ((57, 46.698), (60, 47.899), (63, 49.1)):
            decision = self.step(controller, now, actual, cpu=cpu, load=7.6, power=63.47)
            actual = decision['level']
        self.assertTrue(any('T=49.1' in reason and 'accepted_projection=61.110' in reason
                            for reason in decision['reasons']),
                        decision['reasons'])
        self.assertIsNone(decision['operating']['failed_floor'])
        self.assertFalse(decision['retry_blocked'])
        self.assertEqual(decision['level'], 15)
        self.assertIsNone(controller.motion.restore_level)
        self.assertIn(decision['control_status']['release_basis'], ('ceiling80', 'ordinary'))
        for now in range(66, 157, 3):
            cpu = (46.698, 47.899, 49.1)[((now - 66) // 3) % 3]
            decision = self.step(controller, now, actual, cpu=cpu, load=7.6, power=63.47)
            self.assertIn(decision['level'], (actual, actual - 1))
            self.assertFalse(decision['retry_blocked'])
            actual = decision['level']
            self.assertNotEqual(decision['trial']['outcome'], 'failed')
        last_pulse = now
        for now in range(last_pulse + 3, last_pulse + 180, 3):
            decision = self.step(controller, now, actual, load=7.6, power=63.47)
            self.assertIn(decision['level'], (actual, actual - 1))
            actual = decision['level']
            if actual == 10:
                break
        self.assertEqual(actual, 10)

    def test_forecast_pause_keeps_exact_high_restoration_and_critical_priority(self):
        for cpu, expected in ((57.999, 15), (58, 15), (75, 15), (80, 16)):
            with self.subTest(cpu=cpu):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                actual = 16
                for now in (0, 3):
                    decision = self.step(controller, now, actual)
                    actual = decision['level']
                for now, temperature in ((6, 46.698), (9, 47.899), (12, 49.1)):
                    decision = self.step(controller, now, actual, cpu=temperature)
                    actual = decision['level']
                self.assertEqual(actual, 15)
                decision = self.step(controller, 15, actual, cpu=cpu)
                self.assertEqual(decision['level'], expected)
                if cpu >= 80:
                    self.assertEqual(controller.motion.restore_level, 16)
                    self.assertEqual(controller.motion.restore_from, 15)
                    self.assertEqual(decision['operating']['failed_floor']['witness']['actual_c'], cpu)
                    self.assertEqual(decision['high_wind']['exception'], 'FailedDescentRestoration')
                    self.assertTrue(controller.export_thermal_memory()['failures'])
                else:
                    self.assertFalse(decision['retry_blocked'])
                    self.assertIsNone(controller.motion.restore_level)
                    self.assertFalse(controller.export_thermal_memory()['failures'])

    def measured_failure(self, load=7.6, power=63.47):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        for now in range(0, 220, 3):
            decision = self.step(controller, now, actual, cpu=58 if now == 216 else 49,
                                 load=load, power=power)
            actual = decision['level']
        self.assertTrue(decision['retry_blocked'])
        self.assertEqual(actual, 10)
        return controller

    def test_persistent_actual_rebound_requires_both_strict_boundaries(self):
        for baseline, boundary in ((49, 55), (54, 56)):
            with self.subTest(baseline=baseline, boundary=boundary):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                actual = 10
                for now in range(0, 214, 3):
                    decision = self.step(controller, now, actual, cpu=baseline)
                    actual = decision['level']
                self.assertEqual(actual, 9)
                for now in range(216, 394, 3):
                    cpu = baseline + (boundary - baseline) * (now - 213) / 180
                    decision = self.step(controller, now, 9, cpu=cpu)
                    self.assertFalse(decision['retry_blocked'])
                    self.assertEqual(decision['level'], 9)
                decision = self.step(controller, 396, 9, cpu=boundary + .001)
                self.assertTrue(decision['retry_blocked'])
                self.assertEqual(decision['level'], 10)

    def test_low_power_retry_requires_complete_continuous_safe_endpoint_evidence(self):
        for interruption in ('missing_power', 'duplicate', 'gap', 'warming', 'wrong_endpoint'):
            with self.subTest(interruption=interruption):
                controller = self.measured_failure()
                for now in range(222, 375, 3):
                    self.step(controller, now, 10, load=7.6, power=63.47)
                for now in range(375, 408, 3):
                    self.step(controller, now, 10, load=7.6, power=50.776)
                self.assertEqual(controller.operating.failure_qualification, 'lower_power')
                self.assertEqual(controller.operating.failure_credit, 30)
                if interruption == 'duplicate':
                    controller.decide(fleet(408, cpu=49, gpu=47, board=54, load=7.6, power=50.776),
                        408, minimum_level=0, feedback=feedback(405, 10))
                elif interruption == 'gap':
                    self.step(controller, 414, 10, load=7.6, power=50.776)
                else:
                    self.step(controller, 408, 9 if interruption == 'wrong_endpoint' else 10,
                        cpu=50 if interruption == 'warming' else 49, load=7.6,
                        power=None if interruption == 'missing_power' else 50.776)
                    if interruption == 'warming':
                        self.step(controller, 411, 10, cpu=51, load=7.6, power=50.776)
                self.assertIsNotNone(controller.operating.failed_floor)
                self.assertEqual(controller.operating.failure_credit, 0)

    def test_low_power_retry_zero_load_boundary_and_zero_baseline(self):
        for load, failure_power, clears in ((0, 63.47, True), (15, 63.47, False), (7.6, 0, False)):
            with self.subTest(load=load, failure_power=failure_power):
                controller = self.measured_failure(load, failure_power)
                for now in range(222, 375, 3):
                    self.step(controller, now, 10, load=load, power=63.47)
                for now in range(375, 438, 3):
                    decision = self.step(controller, now, 10, load=load, power=failure_power * .8)
                self.assertEqual(not decision['retry_blocked'], clears)

    def test_low_load_failure_requalifies_on_lower_power_then_fresh_observation(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual, confirmation, failed_at = 10, None, None
        for now in range(0, 600, 3):
            decision = self.step(controller, now, actual,
                cpu=58 if confirmation is not None and now == confirmation + 3 else 49,
                load=7.6, power=63.47)
            if decision['scenario'] == 'recovery_released' and confirmation is None:
                confirmation = now
            actual = decision['level']
            if decision['retry_blocked']:
                failed_at = now
                break
        self.assertIsNotNone(failed_at)
        failure = dict(decision['operating']['failed_floor'])
        self.assertEqual(failure['witness']['actual_c'], 58)
        self.assertEqual((failure['safe_level'], failure['failed_level']), (10, 9))
        for now in range(failed_at + 3, failed_at + 903, 3):
            decision = self.step(controller, now, actual, load=7.6, power=63.47)
            actual = decision['level']
            self.assertEqual(decision['operating']['failed_floor'], failure)
            self.assertEqual(actual, 10)
        for load, power in ((7.6, 50.777), (7.7, 50.776)):
            start = now + 3
            for now in range(start, start + 93, 3):
                decision = self.step(controller, now, actual, load=load, power=power)
                actual = decision['level']
                self.assertTrue(decision['retry_blocked'])
        improved_at = now + 3
        for now in range(improved_at, improved_at + 61, 3):
            decision = self.step(controller, now, actual, load=7.6, power=50.776)
            actual = decision['level']
            if now < improved_at + 60:
                self.assertTrue(decision['retry_blocked'])
            self.assertEqual(actual, 10)
        self.assertFalse(decision['retry_blocked'],
                         'Low-load failure cannot require an impossible negative load for retry')
        self.assertEqual(decision['operating']['observe_credit'], 0)
        for now in range(improved_at + 63, improved_at + 243, 3):
            decision = self.step(controller, now, actual, load=7.6, power=50.776)
            self.assertEqual(decision['level'], 10)
        decision = self.step(controller, improved_at + 243, actual, load=7.6, power=50.776)
        self.assertEqual(decision['level'], 9)
        self.step(controller, improved_at + 246, 9, load=7.6, power=50.776)
        decision = self.step(controller, improved_at + 249, 9, cpu=58, load=7.6, power=50.776)
        self.assertTrue(decision['retry_blocked'])
        self.assertEqual(decision['operating']['failed_floor']['power'], 50.776)
        for now in range(improved_at + 252, improved_at + 852, 3):
            decision = self.step(controller, now, 10, load=7.6, power=50.776)
            self.assertTrue(decision['retry_blocked'])
            self.assertEqual(decision['level'], 10)

    def test_safe_gear_cooling_alone_cannot_erase_failed_lower_gear(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual, confirmed_descent_at, failed = 10, None, None
        for now in range(0, 1500, 3):
            pulse = confirmed_descent_at is not None and now == confirmed_descent_at + 3
            decision = self.step(controller, now, actual, cpu=58 if pulse else 49)
            if decision['scenario'] == 'recovery_released' and confirmed_descent_at is None:
                confirmed_descent_at = now
            if decision['retry_blocked']:
                failed = decision['operating']['failed_floor']['failed_level']
            if failed is not None:
                self.assertTrue(decision['retry_blocked'], (now, decision['operating']))
                self.assertGreater(decision['level'], failed)
            actual = decision['level']
        self.assertIsNotNone(failed)

    def test_measured_improvement_reopens_floor_but_not_prebanked_descent(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual, confirmation, failed_at = 10, None, None
        for now in range(0, 600, 3):
            decision = self.step(controller, now, actual,
                cpu=58 if confirmation is not None and now == confirmation + 3 else 49,
                load=25, power=70)
            if decision['scenario'] == 'recovery_released' and confirmation is None:
                confirmation = now
            actual = decision['level']
            if decision['retry_blocked']:
                failed_at = now
                break
        self.assertIsNotNone(failed_at)
        for now in range(failed_at + 3, failed_at + 153, 3):
            decision = self.step(controller, now, actual, load=10, power=70)
            actual = decision['level']
            self.assertTrue(decision['retry_blocked'], 'Lower load alone is not measured load-and-power improvement')
        cleared_at = None
        for now in range(failed_at + 153, failed_at + 303, 3):
            decision = self.step(controller, now, actual, load=10, power=56)
            actual = decision['level']
            if not decision['retry_blocked']:
                cleared_at = now
                break
        self.assertIsNotNone(cleared_at)
        self.assertGreaterEqual(cleared_at - (failed_at + 153), 60)
        safe_level = actual
        for now in range(cleared_at + 3, cleared_at + 180, 3):
            decision = self.step(controller, now, actual, load=10, power=56)
            self.assertEqual(decision['level'], safe_level)
            actual = decision['level']


if __name__ == '__main__':
    unittest.main()
