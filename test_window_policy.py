import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class WindowPolicyTests(unittest.TestCase):
    def pending(self, minimum=9):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1200, 3):
            decision = controller.decide(fleet(now), now, minimum_level=minimum,
                                         feedback=feedback(now, actual))
            if decision['level'] == 9:
                return controller, now
            actual = decision['level']
        self.fail('no adjacent probe request')

    def test_bounded_rebound_that_reaches_plateau_is_not_failed_cooling(self):
        controller, requested = self.pending()
        confirmed = requested + 3
        succeeded = False
        for age in range(0, 300, 3):
            now = confirmed + age
            decision = controller.decide(fleet(now, cpu=51 + min(age * .025, 1.5)), now,
                                         minimum_level=9, feedback=feedback(now, 9))
            self.assertEqual(decision['level'], 9, decision['scenario_detail'])
            self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
            succeeded |= decision['trial']['outcome'] == 'success'
        self.assertTrue(succeeded, 'bounded rebound never settled before deadline')

    def test_genuine_continuing_heat_cannot_escape_deadline_or_thermal_envelope(self):
        for rate in (.025, .05, .1):
            with self.subTest(rate=rate):
                controller, requested = self.pending()
                confirmed = requested + 3
                for age in range(0, 303, 3):
                    now = confirmed + age
                    decision = controller.decide(fleet(now, cpu=51 + age * rate), now,
                                                 minimum_level=9, feedback=feedback(now, 9))
                    self.assertNotEqual(decision['trial']['outcome'], 'success')
                    if decision['level'] >= 10:
                        break
                self.assertGreaterEqual(decision['level'], 10)
                self.assertLessEqual(age, 300)
                self.assertLess(51 + age * rate, 60)
                self.assertIn(decision['trial']['outcome'], ('failed', 'inconclusive_timeout'))
                self.assertEqual(decision['trial']['outcome'], 'failed')

    def test_successful_hold_loses_and_recovers_settlement_before_next_descent(self):
        controller, requested = self.pending(minimum=0)
        for age in range(3, 124, 3):
            now = requested + age
            decision = controller.decide(fleet(now), now, minimum_level=0,
                                         feedback=feedback(now, 9))
        self.assertEqual(decision['trial']['outcome'], 'success')
        anchor = controller.search_anchor.copy()
        for age in range(3, 214, 3):
            now = requested + 123 + age
            decision = controller.decide(fleet(now, cpu=51 + min(age * .025, 1.5)), now,
                                         minimum_level=0, feedback=feedback(now, 9))
            self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
            self.assertEqual(controller.search_anchor, anchor)
            if age == 60:
                self.assertEqual(decision['level'], 9)
                self.assertEqual(decision['scenario'], 'rebound')
            if decision['level'] == 8:
                break
            self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['level'], 8)
        self.assertGreater(age, 60)
        self.assertLessEqual(age, 210)
        self.assertEqual(decision['trial']['status'], 'pending')

    def test_covered_nonsettlement_is_inconclusive_then_requires_current_qualification(self):
        controller, requested = self.pending(minimum=0)
        for age in range(3, 304, 3):
            now = requested + age
            decision = controller.decide(fleet(now, cpu=51 + .019 * age), now,
                                         minimum_level=0, feedback=feedback(now, 9))
            if age < 303:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertEqual(decision['level'], 10)
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
        self.assertFalse(decision['retry_blocked'])
        self.assertIn('cause=nonsettled', ' '.join(decision['reasons']))
        for stamp in range(now + 3, now + 903, 3):
            decision = controller.decide(fleet(stamp), stamp, minimum_level=0,
                                         feedback=feedback(stamp, 10))
            self.assertFalse(decision['retry_blocked'])
            self.assertFalse(decision['learning_eligible'])
            if decision['level'] == 9:
                break
            self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['trial']['status'], 'pending')
        self.assertGreaterEqual(stamp - now, 150)

    def test_slow_heating_across_success_freezes_new_descent_without_moving_identities(self):
        controller, requested = self.pending(minimum=0)
        anchor = controller.search_anchor.copy()
        actual, successes = 9, set()
        for age in range(0, 205, 3):
            now = requested + 3 + age
            decision = controller.decide(fleet(now, cpu=51 + age * .01), now,
                                         minimum_level=0, feedback=feedback(now, actual))
            if decision['trial']['outcome'] == 'success':
                successes.add(decision['trial']['to'])
            if decision['level'] >= 10:
                break
            self.assertEqual(controller.search_anchor, anchor)
            actual = decision['level']
        self.assertIn(9, successes)
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['scenario'], 'warming_hold')
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')


if __name__ == '__main__':
    unittest.main()
