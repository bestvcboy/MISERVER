import unittest
from copy import deepcopy

import test_equilibrium
from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class GradedCoolingTests(unittest.TestCase):
    def step(self, controller, now, actual=10, readback=None, **values):
        return controller.decide(fleet(now, **values), now, minimum_level=0,
                                 feedback=feedback(now, actual) if readback is None else readback)

    def start_deficit(self):
        controller, now = test_equilibrium.EquilibriumTests().reach(10)
        now += 3
        decision = self.step(controller, now, cpu=60.2)
        self.assertEqual(decision['level'], 10)
        return controller, now

    def test_safe_llm_startup_uses_normal_band(self):
        controller, now = test_equilibrium.EquilibriumTests().reach(9, cpu=42.8)
        actual = 9
        for age in (3, 6, 9):
            decision = controller.decide(fleet(now + age, cpu=42.8 + .4 * age,
                                                load=40, power=70),
                                         now + age, minimum_level=0,
                                         feedback=feedback(now + age, actual))
            self.assertLessEqual(decision['level'], 10, decision['reasons'])
            self.assertFalse(decision['learning_eligible'])
            actual = decision['level']

    def test_cold_power_feedforward_does_not_mean_emergency(self):
        controller, now = test_equilibrium.EquilibriumTests().reach(9, cpu=42.8)
        decision = controller.decide(fleet(now + 3, cpu=43, power=104), now + 3,
                                     minimum_level=0, feedback=feedback(now + 3, 9))
        self.assertEqual(decision['level'], 10)
        self.assertFalse(decision['learning_eligible'])

    def test_absolute_limit_still_overrides_normal_band(self):
        controller, now = test_equilibrium.EquilibriumTests().reach(9, cpu=42.8)
        decision = controller.decide(fleet(now + 3, cpu=73, load=40, power=104), now + 3,
                                     minimum_level=0, feedback=feedback(now + 3, 9))
        self.assertEqual(decision['level'], 16)
        self.assertEqual(decision['scenario'], 'protect')

    def test_all_six_steps_need_thirty_seconds_after_matching_confirmation(self):
        controller, confirmed = self.start_deficit()
        for actual in range(10, 16):
            for age in range(3, 31, 3):
                decision = self.step(controller, confirmed + age, actual, cpu=60.2)
                self.assertEqual(decision['level'], actual + (age == 30))
                self.assertFalse(decision['learning_eligible'])
            requested = confirmed + 30
            for age in range(3, 16, 3):
                decision = self.step(controller, requested + age, actual, cpu=60.2,
                                     readback=feedback(requested + age, actual, confirmed=False, execution='pending'))
                self.assertEqual(decision['level'], actual + 1)
            confirmed = requested + 18
            decision = self.step(controller, confirmed, actual + 1, cpu=60.2,
                                 readback=feedback(requested + 16.5, actual + 1))
            self.assertEqual(decision['level'], actual + 1)
            self.assertEqual(controller.upward['confirmed_at'], confirmed)
        for age in range(3, 61, 3):
            self.assertEqual(self.step(controller, confirmed + age, 16, cpu=60.2)['level'], 16)

    def test_late_deficit_onset_cannot_borrow_prior_confirmed_gear_time(self):
        controller, start = test_equilibrium.EquilibriumTests().reach(10)
        for age in range(3, 94, 3):
            self.assertEqual(self.step(controller, start + age, cpu=59)['level'], 10)
        onset = start + 96
        self.step(controller, onset, cpu=60.2)
        for age in range(3, 31, 3):
            self.assertEqual(self.step(controller, onset + age, cpu=60.2)['level'], 11 if age == 30 else 10)

    def test_duplicate_interior_feedback_breaks_interval_without_filling_it_later(self):
        controller, start = self.start_deficit()
        for age in range(3, 37, 3):
            readback = feedback(start + age - (3 if age == 15 else 0), 10)
            decision = self.step(controller, start + age, cpu=60.2, readback=readback)
            self.assertEqual(decision['level'], 11 if age == 36 else 10)

    def test_pending_deadline_survives_mode_and_context_changes(self):
        controller, start = self.start_deficit()
        for age in range(3, 31, 3):
            self.step(controller, start + age, cpu=60.2)
        requested = start + 30
        for age in range(3, 61, 3):
            nodes = fleet(requested + age, cpu=59, power=70 if age > 15 else 30)
            nodes[0]['power_source'] = 'changed' if age > 30 else 'spbm'
            decision = controller.decide(nodes, requested + age, minimum_level=1 if age > 45 else 0,
                                         feedback=feedback(requested + age, 10, confirmed=False, execution='pending'))
            self.assertEqual(decision['level'], 16 if age == 60 else 11)
            self.assertFalse(decision['learning_eligible'])
            if age < 60:
                self.assertEqual(controller.upward['requested_at'], requested)
        self.assertEqual(decision['scenario'], 'protect')
        self.assertIsNone(controller.upward)

    def test_sensor_switch_and_cleared_deficit_restart_dwell(self):
        for event in ('switch', 'clear'):
            with self.subTest(event=event):
                controller, start = self.start_deficit()
                for age in (3, 6, 9, 12):
                    self.step(controller, start + age, cpu=60.2)
                onset = start + 15
                values = dict(cpu=42, gpu=60.2) if event == 'switch' else dict(cpu=42)
                decision = self.step(controller, onset, **values)
                self.assertEqual(decision['level'], 10)
                if event == 'clear':
                    self.assertIsNone(controller.upward)
                    onset += 3
                    self.step(controller, onset, cpu=60.2)
                    values = dict(cpu=60.2)
                for age in range(3, 31, 3):
                    self.assertEqual(self.step(controller, onset + age, **values)['level'], 11 if age == 30 else 10)

    def test_identity_source_workload_mode_and_actual_gear_restart_dwell(self):
        base, start = self.start_deficit()
        for age in (3, 6, 9, 12):
            self.step(base, start + age, cpu=60.2)
        for event in ('source', 'identity', 'hostname', 'load', 'power', 'mode', 'actual'):
            with self.subTest(event=event):
                controller = deepcopy(base)
                end = 75 if event == 'identity' else 45
                for age in range(15, end + 1, 3):
                    now = start + age
                    nodes = fleet(now, cpu=60.2, load=20 if event == 'load' else 0,
                                  power=50 if event == 'power' else 30)
                    if event == 'source':
                        nodes[0]['power_source'] = 'changed'
                    if event == 'identity':
                        nodes[0]['sensors'].append(dict(id='extra', group='other', temp=40, calibrated=True))
                    if event == 'hostname':
                        nodes[0]['hostname'] = 'changed'
                    decision = controller.decide(nodes, now, minimum_level=1 if event == 'mode' else 0,
                                                 feedback=feedback(now, 11 if event == 'actual' and age == 15 else 10))
                    self.assertEqual(decision['level'], 11 if age == end and event != 'actual' or event == 'actual' and age == 15 else 10)
                    self.assertFalse(decision['learning_eligible'])
                if event == 'actual':
                    self.assertEqual(self.step(controller, start + 48, cpu=60.2)['level'], 11)

    def test_ordinary_power_and_load_allow_safe_startup_recovery_to_ten(self):
        for values in (dict(power=104), dict(load=40), dict(power=104, load=40)):
            controller, actual = Controller(HOSTS), 16
            for now in range(0, 901, 3):
                decision = self.step(controller, now, actual, **values)
                actual = decision['level']
                self.assertGreaterEqual(actual, 10)
                self.assertFalse(decision['learning_eligible'])
            self.assertEqual(actual, 10)

    def test_plateau_below_target_and_isolated_raw_spike_do_not_request_sixteen(self):
        controller, start = test_equilibrium.EquilibriumTests().reach(10, cpu=42.8)
        for age in range(3, 184, 3):
            decision = self.step(controller, start + age, cpu=44 if age == 3 else 46.4,
                                 load=40, power=104)
            self.assertEqual(decision['level'], 10)
        controller, start = test_equilibrium.EquilibriumTests().reach(10)
        self.assertEqual(self.step(controller, start + 3, cpu=59.8)['level'], 10)

    def test_emergency_actual_limits_apply_even_when_flat_or_falling(self):
        for field, temp in (('cpu', 73), ('gpu', 73), ('board', 73)):
            for delta in (0, -.2):
                controller, start = test_equilibrium.EquilibriumTests().reach(10)
                for age, value in ((3, temp - delta), (6, temp)):
                    decision = self.step(controller, start + age, **{field: value})
                    self.assertEqual(decision['level'], 16)
                    self.assertEqual(decision['scenario'], 'protect')

    def test_coherent_projection_to_hard_limit_overrides_dwell(self):
        for field, origin, rate in (('cpu', 67.5, .2), ('gpu', 67.5, .2), ('board', 62, .4),
                                    ('cpu', 60, .5)):
            controller, start = test_equilibrium.EquilibriumTests().reach(10)
            for age in (3, 6, 9):
                decision = self.step(controller, start + age, **{field: origin + rate * age})
            self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['scenario'], 'protect')

    def test_faults_preempt_pending_or_dwell_and_clear_upward_evidence(self):
        base, start = self.start_deficit()
        for event in ('rpm', 'pending_fault', 'missing', 'stale', 'source', 'throttle', 'storage'):
            with self.subTest(event=event):
                controller, now = deepcopy(base), start + 3
                nodes, readback = fleet(now, cpu=60.2), feedback(now, 10)
                if event == 'rpm':
                    readback['rpm'] = 0
                elif event == 'pending_fault':
                    readback.update(execution='pending', confirmed=False, fault=True)
                elif event == 'missing':
                    nodes.pop()
                elif event == 'stale':
                    nodes[0]['requested_mono'] = now - 7
                elif event == 'source':
                    nodes[0]['source_ts'] -= 3
                elif event == 'throttle':
                    nodes[0]['thermal_throttle'] = True
                decision = controller.protect('storage') if event == 'storage' else controller.decide(
                    nodes, now, minimum_level=0, feedback=readback)
                self.assertEqual(decision['level'], 16)
                self.assertFalse(decision['learning_eligible'])
                self.assertIsNone(controller.upward)

    def test_finished_episode_cannot_lend_time_to_downward_admission(self):
        controller, start = self.start_deficit()
        for age in range(3, 31, 3):
            self.step(controller, start + age, cpu=60.2)
        self.step(controller, start + 33, 11, cpu=60.2)
        cleared = start + 36
        self.assertEqual(self.step(controller, cleared, 11)['level'], 11)
        self.assertIsNone(controller.upward)
        for age in range(3, 31, 3):
            self.assertEqual(self.step(controller, cleared + age, 11)['level'], 11)
        self.assertEqual(self.step(controller, cleared + 33, 11)['level'], 10)


if __name__ == '__main__':
    unittest.main()
