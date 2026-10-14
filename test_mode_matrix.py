import unittest
import test_equilibrium

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class ModeMatrixTests(unittest.TestCase):
    def test_cold_confirmed_nine_requests_adjacent_eight_within_forty_five_seconds(self):
        controller, start = test_equilibrium.EquilibriumTests().reach(9, cpu=42.8)
        decisions = []
        for age in range(3, 46, 3):
            decision = controller.decide(fleet(start + age, cpu=42.8), start + age,
                                         minimum_level=0, feedback=feedback(start + age, 9))
            decisions.append(decision)
            if decision['level'] == 8:
                self.assertFalse(decision['learning_eligible'])
                self.assertEqual(decision['trial']['from'], 9)
                self.assertEqual(decision['trial']['to'], 8)
                break
        self.assertEqual(decisions[-1]['level'], 8, decisions[-1]['scenario_detail'])

    def test_isolated_sixty_one_reading_does_not_request_full_speed(self):
        controller, start = test_equilibrium.EquilibriumTests().reach(10, cpu=42.8)
        decision = controller.decide(fleet(start + 3, cpu=61), start + 3,
                                     minimum_level=0, feedback=feedback(start + 3, 10))
        self.assertEqual(decision['level'], 10, decision['reasons'])
        self.assertFalse(decision['learning_eligible'])


if __name__ == '__main__':
    unittest.main()
