import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class PredictiveGuardTests(unittest.TestCase):
    def baseline(self):
        controller = Controller(HOSTS)
        for now in range(0, 60, 3):
            controller.decide(fleet(now, cpu=47.8), now, minimum_level=0,
                              feedback=feedback(now, 16))
        return controller

    def step(self, controller, now, cpu, actual=16):
        nodes = fleet(now, cpu=47.8)
        nodes[1] = fleet(now, cpu=cpu)[1]
        return controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, actual))

    def test_cold_impulse_holds_without_failure_then_release_resumes(self):
        controller = self.baseline()
        decision = self.step(controller, 60, 53.2)
        self.assertNotEqual(decision['scenario'], 'protect')
        self.assertEqual(decision['level'], 16)
        self.assertFalse(decision['learning_eligible'])
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
        for now in range(63, 124, 3):
            decision = self.step(controller, now, 48)
            if decision['level'] == 15:
                break
        self.assertEqual(decision['level'], 15, decision['scenario_detail'])

    def test_small_cold_impulse_cannot_authorize_descent_on_that_frame(self):
        controller = self.baseline()
        decision = self.step(controller, 60, 50.5)
        self.assertEqual(decision['level'], 16)
        self.assertFalse(decision['learning_eligible'])

    def test_pending_descent_holds_actual_level_without_renewing_deadline(self):
        controller = self.baseline()
        requested = self.step(controller, 60, 42.8)
        self.assertEqual(requested['level'], 15)
        deadline = requested['trial']['deadline']
        uncertain = self.step(controller, 63, 53.2)
        self.assertEqual(uncertain['level'], 15)
        self.assertEqual(uncertain['scenario'], 'trial_pending')
        self.assertFalse(uncertain['learning_eligible'])
        self.assertEqual(uncertain['trial']['deadline'], deadline)
        self.assertIsNone(uncertain['trial']['observation_elapsed'])
        resumed = self.step(controller, 66, 48)
        self.assertEqual(resumed['level'], 15)
        self.assertEqual(resumed['trial']['deadline'], deadline)

    def test_hard_near_limit_and_startup_predictions_remain_protected(self):
        for temperature in (73, 75):
            with self.subTest(temperature=temperature):
                decision = self.step(self.baseline(), 60, temperature)
                self.assertEqual(decision['scenario'], 'protect')
                self.assertEqual(decision['level'], 16)
        controller = Controller(HOSTS)
        self.step(controller, 0, 42.8)
        decision = self.step(controller, 3, 45)
        self.assertEqual(decision['level'], 16)
        self.assertFalse(decision['learning_eligible'])

    def test_retained_fast_event_alone_does_not_authorize_emergency(self):
        controller = self.baseline()
        self.step(controller, 60, 49)
        decision = self.step(controller, 63, 50.2)
        self.assertNotEqual(decision['scenario'], 'protect')
        for now in range(66, 94, 3):
            decision = self.step(controller, now, 50.2)
            self.assertNotEqual(decision['scenario'], 'protect', now)
        self.assertNotEqual(self.step(controller, 96, 50.2)['scenario'], 'protect')

    def test_sensor_qualification_does_not_depend_on_fleet_iteration_order(self):
        for reverse in (False, True):
            controller = self.baseline()
            nodes = fleet(60, cpu=47.8)
            nodes[0] = fleet(60, cpu=73)[0]
            nodes[1] = fleet(60, cpu=53.2)[1]
            decision = controller.decide(nodes[::-1] if reverse else nodes, 60,
                                         minimum_level=0, feedback=feedback(60, 16))
            self.assertEqual(decision['scenario'], 'protect')
            self.assertTrue(any('[uncertain]' in reason and HOSTS[1] in reason
                                for reason in decision['reasons']))

    def test_two_board_steps_far_from_thermal_envelope_are_not_an_emergency(self):
        controller = Controller(HOSTS)
        for now in range(0, 64, 3):
            temperature = 41 if now == 60 else 42 if now == 63 else 40
            decision = controller.decide(fleet(now, board=temperature), now,
                                         minimum_level=0, feedback=feedback(now, 16))
        self.assertNotEqual(decision['scenario'], 'protect')
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')


if __name__ == '__main__':
    unittest.main()
