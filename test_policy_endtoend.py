import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class MinimumSufficientGearTests(unittest.TestCase):
    def test_idle_and_stable_load_can_reach_zero_without_a_hidden_gear_floor(self):
        for load, power in ((5, 45), (60, 100)):
            with self.subTest(load=load, power=power):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                actual, edges = 10, []
                for now in range(0, 2401, 3):
                    decision = controller.decide(fleet(now, cpu=40, gpu=40, board=46,
                        load=load, power=power), now, minimum_level=0, feedback=feedback(now, actual))
                    if decision["level"] != actual:
                        edges.append((now, actual, decision["level"]))
                    actual = decision["level"]
                self.assertEqual(actual, 0, edges)
                self.assertEqual(len(edges), 10, edges)
                self.assertTrue(all(before - after == 1 for _, before, after in edges), edges)
                self.assertTrue(all(b[0] - a[0] >= 180 for a, b in zip(edges, edges[1:])), edges)
                decision = controller.decide(fleet(2403, cpu=75, gpu=40, board=46,
                    load=load, power=power), 2403, minimum_level=0, feedback=feedback(2403, actual))
                self.assertEqual(decision["level"], 10)
                self.assertFalse(controller.high_wind.valid_at(2403))
                self.assertEqual(controller.motion.actual_c, 75)
                decision = controller.decide(fleet(2415, cpu=80, gpu=40, board=46,
                    load=load, power=power), 2415, minimum_level=0, feedback=feedback(2415, 10))
                self.assertEqual(decision['level'], 11)
                self.assertTrue(controller.high_wind.valid_at(2415))


if __name__ == "__main__":
    unittest.main()
