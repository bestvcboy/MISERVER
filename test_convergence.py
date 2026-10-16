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
        self.assertEqual(hot['execution_trial']['to'], 16)
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

    def test_expired_unresolved_execution_does_not_receive_new_deadlines(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        first = self.step(controller, 100, cpu=80)
        deadline = first['execution_trial']['original_deadline']
        for now in (160, 163, 166, 220):
            decision = self.step(controller, now, cpu=80)
            self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
            self.assertNotEqual(decision['execution_trial']['status'], 'pending')

    def test_ceiling_correction_from_actual_sixteen_keeps_remaining_budget(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        first = self.step(controller, 100, cpu=80)
        decision = self.step(controller, 103, actual=16)
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['execution_trial']['to'], 10)
        self.assertEqual(decision['execution_trial']['status'], 'pending')
        self.assertEqual(decision['execution_trial']['original_deadline'],
                         first['execution_trial']['original_deadline'])
        self.assertFalse(controller.feedback_matches_request(10, 103, 103))


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
