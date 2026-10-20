import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class Enhanced80AdmissionTests(unittest.TestCase):
    def test_exact_measured_gate(self):
        for temperature, expected in ((75, 10), (79.999, 10), (80, 16), (80.001, 16)):
            with self.subTest(temperature=temperature):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                decision = controller.decide(fleet(100, cpu=temperature, gpu=45, board=50),
                    100, minimum_level=0, feedback=feedback(100, 10))
                self.assertEqual(decision['high_wind_limit'], expected)
                if temperature < 80:
                    self.assertLessEqual(decision['demand'], 10)
                    self.assertLessEqual(decision['level'], 10)

    def test_hot_permission_is_revoked_without_low_temperature_hysteresis(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        controller.decide(fleet(100, cpu=82), 100,
            minimum_level=0, feedback=feedback(100, 10))
        decision = controller.decide(fleet(103, cpu=79.999), 103,
            minimum_level=0, feedback=feedback(103, 10))
        self.assertEqual(decision['high_wind_limit'], 10)
        self.assertLessEqual(decision['level'], 10)

    def test_manual_high_selection_cannot_supply_temperature_permission(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        decision = controller.decide(fleet(100, cpu=79, gpu=52, board=64, load=90, power=130),
            100, minimum_level=16, feedback=feedback(100, 10), manual_level=16)
        self.assertEqual(decision['high_wind_limit'], 10)
        self.assertLessEqual(decision['level'], 10)

    def test_existing_high_endpoint_has_a_policy_exit_without_old_observation(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        commands = []
        for now in range(0, 40, 3):
            decision = controller.decide(fleet(now, cpu=65, gpu=52, board=64,
                load=60 if now % 2 else 85, power=100 if now % 2 else 125), now,
                minimum_level=0, feedback=feedback(now, 16, confirmed=now == 0,
                    execution='matched' if now == 0 else 'pending', rpm=2096))
            self.assertEqual(decision['high_wind_limit'], 10)
            self.assertLessEqual(decision['demand'], 10)
            operation = decision.get('execution_trial') or {}
            if operation.get('status') == 'pending' and operation.get('from') == 16:
                commands.append(operation.get('to'))
                break
        self.assertEqual(commands, [15])


if __name__ == '__main__':
    unittest.main()
