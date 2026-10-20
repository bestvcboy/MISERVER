import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class CriticalConfirmationTests(unittest.TestCase):
    def test_current_critical_temperature_also_wins_in_ascent_confirmation_frame(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        first = controller.decide(fleet(100, cpu=45, gpu=47, board=54, load=35, power=60),
            100, minimum_level=0, feedback=feedback(100, 4))
        self.assertEqual(first['level'], 6)
        decision = controller.decide(fleet(103, cpu=80, gpu=47, board=54, load=35, power=60),
            103, minimum_level=0, feedback=feedback(103, 6))
        self.assertEqual(controller.operating.confirmed_level, 6)
        self.assertEqual((decision['level'], decision['demand']), (6, 10))
        self.assertIsNone(controller.motion.source)
        self.assertEqual(decision['normal_ceiling'], 16)

    def test_current_critical_temperature_wins_in_descent_confirmation_frame(self):
        for temperature, expected in ((75, 10), (79.9, 10), (80, 10)):
            with self.subTest(temperature=temperature):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                for now in range(0, 211, 3):
                    decision = controller.decide(fleet(now, cpu=45, gpu=47, board=54, load=5, power=60),
                        now, minimum_level=0, feedback=feedback(now, 10))
                self.assertEqual(decision['level'], 9)
                decision = controller.decide(fleet(213, cpu=temperature, gpu=47, board=54, load=5, power=60),
                    213, minimum_level=0, feedback=feedback(213, 9))
                self.assertEqual(controller.operating.confirmed_level, 9)
                self.assertEqual(decision['level'], expected)
                self.assertEqual(decision['demand'], expected)
                self.assertTrue(controller.export_thermal_memory()['failures'])
                if temperature < 80:
                    self.assertIsNotNone(controller.motion.source)
                    self.assertEqual(controller.motion.actual_c, temperature)


class DescentOwnershipTests(unittest.TestCase):
    def test_manual_descent_keeps_its_origin_when_failure_arrives_in_automatic_mode(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        for now in range(0, 67, 3):
            decision = controller.decide(fleet(now, cpu=58 if now == 66 else 49,
                gpu=47, board=54, load=5, power=60), now, minimum_level=0,
                manual_level=None if now == 66 else 0, feedback=feedback(now, actual))
            actual = decision['level']
        self.assertTrue(decision['retry_blocked'])
        self.assertFalse(decision['operating']['failed_floor']['automatic'])

    def test_mode_switch_before_failure_cannot_erase_automatic_descent_memory(self):
        for switch_at in ('confirmation', 'failure'):
            with self.subTest(switch_at=switch_at):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                actual = 10
                for now in range(0, 1000, 3):
                    mode = 10 if now == (213 if switch_at == 'confirmation' else 216) else None
                    decision = controller.decide(fleet(now, cpu=58 if now == 216 else 49,
                        gpu=47, board=54, load=25, power=70), now, minimum_level=0,
                        manual_level=mode, feedback=feedback(now, actual))
                    actual = decision['level']
                    if now >= 216:
                        self.assertTrue(decision['retry_blocked'], (now, decision['operating']))
                        self.assertTrue(decision['operating']['failed_floor']['automatic'])
                        self.assertGreaterEqual(actual, 10)


if __name__ == '__main__':
    unittest.main()
