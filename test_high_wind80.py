import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class CriticalPolicyGateTests(unittest.TestCase):
    def decide(self, controller, now, temp):
        return controller.decide(fleet(now, cpu=temp, gpu=45, board=50), now,
            minimum_level=0, feedback=feedback(now, 10))

    def test_sub_critical_rising_temperature_never_authorizes(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now, temp in ((0, 68), (3, 69), (6, 70), (9, 74.999)):
            decision = self.decide(controller, now, temp)
            self.assertEqual(decision['high_wind_limit'], 10)
            self.assertLessEqual(decision['level'], 10)

    def test_current_critical_boundary_needs_no_rising_history(self):
        for temp, expected in ((74.999, 10), (75, 10), (75.001, 10), (79.999, 10), (80, 16)):
            with self.subTest(temp=temp):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                self.assertEqual(self.decide(controller, 100, temp)['high_wind_limit'], expected)

    def test_hot_plateau_and_cooling_remain_authorized(self):
        for temperatures in ((80, 80, 80, 80), (83, 82, 81, 80)):
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            limits = [self.decide(controller, now, temp)['high_wind_limit']
                for now, temp in zip(range(0, 12, 3), temperatures)]
            self.assertEqual(limits, [16, 16, 16, 16])

    def test_cooling_below_critical_revokes_old_permission(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.decide(controller, 100, 82)
        self.assertEqual(self.decide(controller, 103, 74.9)['high_wind_limit'], 10)

    def test_exact_six_second_expiry(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.decide(controller, 100, 80)
        self.assertEqual(controller.high_wind.observed_mono, 99.5)
        self.assertEqual(controller.effective_limit(105.499), 16)
        self.assertEqual(controller.effective_limit(105.5), 10)
        self.assertEqual(controller.effective_limit(105.999), 10)
        self.assertEqual(controller.effective_limit(106), 10)
        self.assertEqual(controller.effective_limit(106.001), 10)


if __name__ == '__main__':
    unittest.main()
