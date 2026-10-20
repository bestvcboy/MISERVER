import unittest
from copy import deepcopy

from core import Controller, FailedDescentRestoration
from test_core import HOSTS
from test_scenarios import feedback, fleet
from thermal_policy import ThermalMemory, source_key


class EnhancedExitR3Tests(unittest.TestCase):
    def restored(self, now=0):
        nodes = fleet(now, cpu=62, gpu=50, board=62, load=5, power=45)
        key = source_key(nodes[1], (HOSTS[1], nodes[1]['sensors'][0]['id'], 'acpi'))
        memory = ThermalMemory()
        memory.add_failure(key, dict(safe_level=16, failed_level=15, baseline=[5, 5, 45],
            automatic=True, failure_class='thermal_failure'))
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        controller.restore_thermal_memory(memory.export(), now)
        return controller

    def step(self, controller, now, actual=16, cpu=62, board=62, **changes):
        return controller.decide(fleet(now, cpu=cpu, gpu=50, board=board, load=5, power=45),
            now, minimum_level=0, feedback=feedback(now, actual, **changes))

    def test_legacy_same_load_pending_endpoint_exits_six_adjacent_edges_and_keeps_rows(self):
        for temperature in (62, 77):
            with self.subTest(temperature=temperature):
                controller = self.restored()
                before = deepcopy(controller.export_thermal_memory()['failures'])
                actual, pending, edges = 16, None, []
                for now in range(0, 904, 3):
                    if pending is not None:
                        actual, pending = pending, None
                    decision = self.step(controller, now, actual, cpu=temperature,
                        confirmed=False, execution='pending')
                    self.assertEqual(controller.export_thermal_memory()['failures'], before)
                    self.assertEqual(controller.operating.observe_credit, 0)
                    operation = decision.get('execution_trial') or {}
                    if operation.get('status') == 'pending' and operation.get('to') == actual - 1:
                        if not edges or edges[-1] != (actual, actual - 1):
                            edges.append((actual, actual - 1))
                            pending = actual - 1
                    if controller.operating.confirmed_level == 10:
                        break
                self.assertEqual(edges, [(16, 15), (15, 14), (14, 13), (13, 12), (12, 11), (11, 10)])
                self.assertEqual(controller.operating.confirmed_level, 10)
                self.assertEqual(now, 378)
                for stamp in range(now + 3, now + 100, 3):
                    self.step(controller, stamp, 10, cpu=temperature)
                    self.assertEqual(controller.export_thermal_memory()['failures'], before)

    def test_requalification_credit_is_visible_and_pending_time_is_frozen(self):
        controller = self.restored()
        for now in range(0, 61, 3):
            decision = self.step(controller, now, confirmed=False, execution='pending')
            if now < 60:
                status = decision['control_status']
                self.assertEqual(status['primary_reason'], 'ceiling_exit_requalifying')
                self.assertEqual((status['observe_seconds'], status['required_seconds']), (now, 60))
                self.assertEqual(status['risk_evidence_expires_mono'], now + 5.5)
                self.assertEqual(decision['operating']['failure_credit'], now)
        self.assertEqual(decision['next_command'], 15)
        for now in range(63, 118, 3):
            decision = self.step(controller, now, confirmed=False, execution='pending')
            self.assertEqual(decision['operating']['failure_credit'], 60)
            self.assertEqual(decision['execution_trial']['original_deadline'], 120)
        decision = self.step(controller, 120, 15)
        self.assertEqual(decision['execution_trial']['status'], 'execution_timeout')

    def test_legacy_idle_variation_qualifies_without_erasing_failure(self):
        controller = self.restored()
        before = deepcopy(controller.export_thermal_memory()['failures'])
        for now in range(0, 61, 3):
            load, power = (5, 45) if now % 6 == 0 else (12, 58)
            decision = controller.decide(fleet(now, cpu=50, gpu=45, board=50,
                load=load, power=power), now, minimum_level=0,
                feedback=feedback(now, 16, confirmed=False, execution='pending'))
            self.assertEqual(controller.export_thermal_memory()['failures'], before)
            if now < 60:
                self.assertEqual(decision['next_command'], 16)
        self.assertEqual(decision['next_command'], 15)
        self.assertEqual(decision['execution_trial']['original_deadline'], 120)
        self.assertEqual(decision['operating']['failure_credit'], 60)

    def test_idle_qualification_does_not_carry_credit_across_regime_excursion(self):
        for load, power in ((15.001, 58), (12, 80)):
            with self.subTest(load=load, power=power):
                controller = self.restored()
                for now in range(0, 58, 3):
                    varying = now % 6 != 0
                    controller.decide(fleet(now, cpu=50, gpu=45, board=50,
                        load=12 if varying else 5, power=58 if varying else 45),
                        now, minimum_level=0, feedback=feedback(now, 16))
                decision = controller.decide(fleet(60, cpu=50, gpu=45, board=50,
                    load=load, power=power), 60, minimum_level=0, feedback=feedback(60, 16))
                self.assertEqual(decision['next_command'], 16)
                self.assertEqual(decision['operating']['failure_credit'], 0)
                for now in range(63, 123, 3):
                    varying = now % 6 != 0
                    decision = controller.decide(fleet(now, cpu=50, gpu=45, board=50,
                        load=15 if varying else 5, power=79.9 if varying else 45),
                        now, minimum_level=0, feedback=feedback(now, 16))
                    self.assertEqual(decision['next_command'], 16)
                decision = controller.decide(fleet(123, cpu=50, gpu=45, board=50,
                    load=15, power=79.9), 123, minimum_level=0, feedback=feedback(123, 16))
                self.assertEqual(decision['next_command'], 15)

    def test_restart_and_storage_pause_discard_credit_without_deleting_failure(self):
        for interruption in ('restart', 'storage'):
            with self.subTest(interruption=interruption):
                controller = self.restored()
                for now in range(0, 58, 3):
                    self.step(controller, now)
                before = controller.export_thermal_memory()
                if interruption == 'restart':
                    controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                    controller.restore_thermal_memory(before, 60)
                else:
                    controller.pause_release('storage', 58)
                for now in range(60, 120, 3):
                    decision = self.step(controller, now)
                    self.assertEqual(decision['next_command'], 16)
                    self.assertEqual(controller.export_thermal_memory()['failures'], before['failures'])
                self.assertEqual(self.step(controller, 120)['next_command'], 15)

    def test_untrusted_feedback_resets_qualification(self):
        for change in (dict(execution='unknown'), dict(rpm=0), dict(mode='auto'),
                dict(observed_mono=51), dict(motor_attempts=2), dict(protection_interrupted=True)):
            with self.subTest(change=change):
                controller = self.restored()
                for now in range(0, 58, 3):
                    self.step(controller, now)
                self.step(controller, 60, **change)
                for now in range(63, 123, 3):
                    self.assertEqual(self.step(controller, now)['next_command'], 16)
                self.assertEqual(self.step(controller, 123)['next_command'], 15)

    def test_eleven_to_ten_is_not_reclassified_at_ordinary_board_target(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(controller, 0, 11, cpu=40, board=62.9)
        for now in range(3, 61, 3):
            decision = self.step(controller, now, 10, cpu=40, board=65.3)
            self.assertLessEqual(decision['next_command'], 10)
            self.assertNotIsInstance(controller.high_wind.proof, FailedDescentRestoration)
            self.assertEqual(controller.export_thermal_memory()['failures'], [])

    def test_board_working_target_crossing_does_not_prove_enhanced_exit_failure(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        first = controller.decide(fleet(0, cpu=40, gpu=38, board=62.9, load=5, power=45),
            0, minimum_level=0, feedback=feedback(0, 16))
        self.assertEqual(first['next_command'], 15)
        decision = controller.decide(fleet(3, cpu=40, gpu=38, board=65.3, load=5, power=45),
            3, minimum_level=0, feedback=feedback(3, 15))
        self.assertEqual(decision['next_command'], 15)
        self.assertNotIsInstance(controller.high_wind.proof, FailedDescentRestoration)
        self.assertEqual(controller.export_thermal_memory()['failures'], [])

    def test_high_failure_is_not_a_universal_ordinary_floor_after_restart_at_ten(self):
        controller = self.restored()
        before = controller.export_thermal_memory()['failures']
        for now in range(0, 604, 3):
            decision = self.step(controller, now, 10, cpu=40, board=46)
            if decision['next_command'] == 9:
                break
        self.assertEqual(decision['next_command'], 9)
        self.assertGreaterEqual(now, 180)
        self.assertEqual(controller.export_thermal_memory()['failures'], before)

    def test_same_process_danger_cannot_retry_only_because_it_cooled(self):
        for manual in (None, 16):
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            for now, actual, cpu in ((0, 16, 40), (3, 15, 80), (6, 16, 40)):
                controller.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=5, power=45),
                    now, minimum_level=0, manual_level=manual, feedback=feedback(now, actual))
            before = controller.export_thermal_memory()['failures']
            for now in range(9, 904, 3):
                decision = controller.decide(fleet(now, cpu=40, gpu=38, board=46, load=5, power=45),
                    now, minimum_level=0, manual_level=manual, feedback=feedback(now, 16))
                self.assertEqual(decision['next_command'], 16)
                self.assertEqual(decision['operating']['failure_credit'], 0)
                self.assertEqual(controller.export_thermal_memory()['failures'], before)

    def test_known_failure_lower_load_recovery_accepts_bounded_temperature_cycle(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now, actual, cpu in ((0, 16, 40), (3, 15, 80), (6, 16, 40)):
            controller.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=60, power=100),
                now, minimum_level=0, feedback=feedback(now, actual))
        before = deepcopy(controller.export_thermal_memory()['failures'])
        self.assertTrue(before)
        for now in range(9, 244, 3):
            temperature = 50 + (0, .2, 0, -.2)[now // 3 % 4]
            decision = controller.decide(fleet(now, cpu=temperature, gpu=45, board=50,
                load=5, power=45), now, minimum_level=0,
                feedback=feedback(now, 16, confirmed=False, execution='pending'))
            self.assertEqual(controller.export_thermal_memory()['failures'], before)
            self.assertEqual(controller.operating.observe_credit, 0)
            if decision['next_command'] == 15:
                break
        self.assertEqual(decision['next_command'], 15)
        self.assertEqual(decision['execution_trial']['purpose'], 'ceiling_exit')
        self.assertEqual(decision['execution_trial']['original_deadline'], now + 60)
        self.assertEqual(decision['operating']['failure_credit'], 60)

    def test_restart_does_not_forget_material_recovery_for_new_ceiling_danger(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now, actual, cpu in ((0, 16, 40), (3, 15, 80), (6, 16, 40)):
            controller.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=60, power=100),
                now, minimum_level=0, feedback=feedback(now, actual))
        before = controller.export_thermal_memory()
        self.assertTrue(before['failures'])
        restored = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        restored.restore_thermal_memory(before, 9)
        for now in range(9, 310, 3):
            decision = restored.decide(fleet(now, cpu=40, gpu=38, board=46, load=60,
                power=100), now, minimum_level=0, feedback=feedback(now, 16))
            self.assertEqual(decision['next_command'], 16)
            self.assertEqual(restored.export_thermal_memory()['failures'], before['failures'])

    def test_lower_power_recovery_leaves_old_preparing_phase_without_deleting_failure(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        controller.decide(fleet(0, cpu=40, gpu=38, board=46, load=60, power=100),
            0, minimum_level=0, feedback=feedback(0, 10))
        for now, actual, cpu in ((3, 16, 40), (6, 16, 40), (9, 15, 80)):
            controller.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=5, power=45),
                now, minimum_level=0, feedback=feedback(now, actual))
        self.assertEqual(controller._workload_cycle.phase, 'preparing')
        before = controller.export_thermal_memory()['failures']
        self.assertTrue(before)
        for now in range(12, 904, 3):
            decision = controller.decide(fleet(now, cpu=40, gpu=38, board=46, load=5, power=30),
                now, minimum_level=0, feedback=feedback(now, 16))
            if decision['next_command'] == 15:
                break
        self.assertEqual(decision['next_command'], 15)
        self.assertNotEqual(controller._workload_cycle.phase, 'preparing')
        self.assertEqual(controller.export_thermal_memory()['failures'], before)

    def test_sustained_post_confirmation_danger_restores_only_the_last_edge(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 37, 3):
            temperature = 65 if now <= 3 else 65 + .4 * (now - 3)
            decision = self.step(controller, now, 16 if now == 0 else 15, cpu=temperature, board=46)
            if now < 36:
                self.assertNotIsInstance(controller.high_wind.proof, FailedDescentRestoration)
                self.assertEqual(controller.export_thermal_memory()['failures'], [])
        self.assertLess(temperature, 80)
        self.assertEqual(decision['next_command'], 16)
        self.assertIsInstance(controller.high_wind.proof, FailedDescentRestoration)
        self.assertTrue(controller.high_wind.permits(36, 15, 16))
        self.assertFalse(controller.high_wind.permits(36, 14, 16))
        self.assertTrue(controller.export_thermal_memory()['failures'])

    def test_known_danger_at_ten_needs_material_recovery_before_ordinary_retry(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now, actual, cpu in ((0, 11, 40), (3, 10, 80)):
            controller.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=60, power=100),
                now, minimum_level=0, feedback=feedback(now, actual))
        before = controller.export_thermal_memory()['failures']
        self.assertTrue(before)
        for now in range(6, 604, 3):
            decision = controller.decide(fleet(now, cpu=40, gpu=38, board=46, load=60, power=100),
                now, minimum_level=0, feedback=feedback(now, 10))
            self.assertGreaterEqual(decision['next_command'], 10)
            self.assertEqual(controller.export_thermal_memory()['failures'], before)
        for now in range(606, 1204, 3):
            decision = controller.decide(fleet(now, cpu=40, gpu=38, board=46, load=5, power=45),
                now, minimum_level=0, feedback=feedback(now, 10))
            if decision['next_command'] == 9:
                break
        self.assertEqual(decision['next_command'], 9)
        self.assertGreaterEqual(now, 666)
        self.assertEqual(controller.export_thermal_memory()['failures'], before)


if __name__ == '__main__':
    unittest.main()
