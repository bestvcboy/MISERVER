import unittest
from unittest.mock import patch

from core import Controller, HighWindAuthority, NoAscent, TenLevelNonRecovery
import service
from test_core import HOSTS
from test_scenarios import feedback, fleet
from test_service import FakeFan


class ThermalCapacityPolicyTests(unittest.TestCase):
    def step(self, controller, now, actual=10, cpu=65, **changes):
        return controller.decide(fleet(now, cpu=cpu, gpu=45, board=50,
            load=changes.pop('load', 5), power=changes.pop('power', 60)), now,
            minimum_level=0, feedback=feedback(now, actual, **changes))

    def test_actual_ten_above_working_target_cannot_grant_empirical_eleven(self):
        for cpu in (60.2, 65, 70, 75, 79.999):
            with self.subTest(cpu=cpu):
                c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                for now in range(0, 904, 3):
                    d = self.step(c, now, cpu=cpu, load=70, power=100)
                    self.assertLessEqual(d['level'], 10)
                    self.assertEqual(d['high_wind_limit'], 10)
                    self.assertIsNone(c._empirical.deadline)
                    self.assertFalse(c.high_wind.permits(now, 10, 11))

    def test_first_current80_admission_needs_no_old_capacity_window(self):
        c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        d = self.step(c, 100, cpu=80)
        self.assertEqual((d['level'], d['high_wind_limit']), (11, 16))
        self.assertEqual(d['execution_trial']['purpose'], 'reserve')
        self.assertEqual(d['execution_trial']['original_deadline'], 160)

    def test_old_capacity_proof_below80_is_invalid_even_with_time_remaining(self):
        for temperature in (65, 79.999):
            a = HighWindAuthority(True, (HOSTS[0], 'DGX', 'cpu'), 100, temperature,
                TenLevelNonRecovery(60, 180, 600, 10, 11))
            self.assertFalse(a.valid_at(100))
            self.assertEqual(a.ceiling_at(100), 10)
            self.assertFalse(a.permits(100, 10, 11))

    def test_ascent_proofs_cannot_authorize_high_band_descent(self):
        c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(c, 100, cpu=80)
        for actual, target, authority in ((11, 10, c.high_wind),
            (16, 15, HighWindAuthority(True, (HOSTS[0], 'DGX', 'cpu'), 100, 80))):
            device = FakeFan(['on', 'favorite', actual, 900])
            with patch('service.time.monotonic', return_value=100):
                service.fan_apply(device, lambda: service.FanRequest(target, high_wind=authority),
                    service.MotorSchedule())
            self.assertEqual(device.writes, [])

    def test_current80_request_has_worker_fixed_six_second_expiry(self):
        c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(c, 100, cpu=80)
        authority = c.high_wind
        self.assertEqual(authority.observed_mono, 99.5)
        for clock, expected in ((105.499, [11]), (105.5, []), (106, [])):
            device = FakeFan(['on', 'favorite', 10, 900])
            with patch('service.time.monotonic', return_value=clock):
                service.fan_apply(device, lambda: service.FanRequest(11, protected=True,
                    high_wind=authority, motion=c.motion), service.MotorSchedule())
            self.assertEqual(device.writes, expected)

    def test_new_cold_sample_revokes_unused_ascent_even_with_repeated_fan_stamp(self):
        c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(c, 100, cpu=80)
        d = self.step(c, 103, cpu=79.999, observed_mono=100)
        self.assertEqual(d['high_wind_limit'], 10)
        self.assertFalse(c.high_wind.permits(103, 10, 16))
        self.assertIsNone(c._empirical.deadline)

    def test_switching_cpu_to_gpu_below80_never_transfers_an_empirical_grant(self):
        c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 604, 3):
            d = c.decide(fleet(now, cpu=65 if now < 300 else 50,
                gpu=45 if now < 300 else 65, board=50, load=5, power=60), now,
                minimum_level=0, feedback=feedback(now, 10))
            self.assertLessEqual(d['level'], 10)
            self.assertEqual(d['high_wind_limit'], 10)

    def test_fractional_clocks_do_not_ratchet_below80_or_delay_ceiling_exit(self):
        for interval in (3.001, 2.999):
            c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            for tick in range(100):
                d = self.step(c, tick * interval)
                self.assertLessEqual(d['level'], 10)
            c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            d = self.step(c, 100, 16)
            self.assertEqual(d['level'], 15)
            self.assertIsInstance(c.high_wind.proof, NoAscent)
            self.assertEqual(d['execution_trial']['original_deadline'], 160)

    def test_hot80_then_cold_high_actual_is_adjacent_without_sixty_second_recovery(self):
        c = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(c, 100, 16, cpu=80)
        d = self.step(c, 103, 16, cpu=65)
        self.assertEqual((d['level'], d['high_wind_limit']), (15, 10))
        self.assertEqual(d['execution_trial']['purpose'], 'ceiling_exit')
        self.assertIsNone(c._empirical.deadline)

    def test_admission_needs_actual_running_advancing_feedback(self):
        for actual, changes in ((9, {}), (10, {'rpm': 0}),
                                (10, {'confirmed': False, 'execution': 'pending'}),
                                (10, {'observed_mono': 0})):
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            for now in range(0, 244, 3):
                decision = self.step(controller, now, actual, **changes)
                self.assertLessEqual(decision['level'], 10)

    def test_below_working_or_real_cooling_never_proves_insufficiency(self):
        for cooling in (False, True):
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            for now in range(0, 244, 3):
                decision = self.step(controller, now, cpu=69 - now * .02 if cooling else 50,
                                     load=70, power=100)
                self.assertLessEqual(decision['level'], 10)

    def test_idle_class_boundary_cannot_complete_old_window(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 181, 3):
            decision = self.step(controller, now, load=15 if now < 150 else 16)
            self.assertLessEqual(decision['level'], 10)

    def test_explicit_missing_proof_fails_closed(self):
        authority = HighWindAuthority(True, (HOSTS[0], 'DGX', 'cpu'), 100, 80, proof=None)
        self.assertFalse(authority.valid_at(100))
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        controller.high_wind = authority
        self.assertEqual(controller.effective_limit(100), 10)
        device = FakeFan(['on', 'favorite', 10, 900])
        with patch('service.time.monotonic', return_value=100):
            service.fan_apply(device, lambda: service.FanRequest(16, high_wind=authority), service.MotorSchedule())
        self.assertEqual(device.writes, [])

    def test_revocation_and_fault_never_force_high_actual_to_ten(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(controller, 0, 16, cpu=80)
        nodes = fleet(3, cpu=65)
        nodes[0]['online'] = False
        decision = controller.decide(nodes, 3, minimum_level=0, feedback=feedback(3, 16))
        self.assertEqual(decision['level'], 16)
        self.assertEqual(decision['high_wind_limit'], 10)

    def test_unpermitted_worker_holds_running_high_actual(self):
        device = FakeFan(['on', 'favorite', 16, 900])
        with patch('service.time.monotonic', return_value=100):
            result = service.fan_apply(device, lambda: service.FanRequest(10), service.MotorSchedule())
        self.assertEqual(device.writes, [])
        self.assertEqual(result['level'], 16)

    def test_unpermitted_high_hold_never_activates_power_or_favorite_mode(self):
        for actual in (['off', 'favorite', 16, 0], ['on', 'auto', 16, 900]):
            device = FakeFan(actual)
            with patch('service.time.monotonic', return_value=100):
                service.fan_apply(device, lambda: service.FanRequest(10), service.MotorSchedule())
            self.assertEqual(device.writes, [])

    def test_brief_noncritical_warming_is_not_ten_level_insufficiency(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now, cpu in ((0, 45), (3, 46), (6, 47), (9, 48)):
            decision = controller.decide(fleet(now, cpu=cpu, gpu=45, board=50), now,
                minimum_level=0, feedback=feedback(now, 10))
            self.assertLessEqual(decision['level'], 10)

    def test_critical_permission_can_activate_known_high_state(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(controller, 100, 16, cpu=80)
        for actual in (['off', 'favorite', 16, 0], ['on', 'auto', 16, 900]):
            device = FakeFan(actual)
            with patch('service.time.monotonic', return_value=100):
                service.fan_apply(device, lambda: service.FanRequest(16, high_wind=controller.high_wind,
                    motion=controller.motion), service.MotorSchedule())
            self.assertTrue(device.writes)

    def test_async_protection_uses_its_current_clock_for_public_authority(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(controller, 100, 10, cpu=80)
        decision = controller.protect('asynchronous fault', 107)
        self.assertEqual(decision['high_wind_limit'], 10)
        self.assertEqual(decision['high_wind']['ceiling'], 10)
        self.assertEqual(decision['high_wind']['reason'], 'expired')
        self.assertIsNone(decision['high_wind_witness'])

    def test_critical_protection_does_not_wait_for_ten_level_experiment(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        decision = controller.decide(fleet(100, cpu=80, gpu=45, board=50), 100,
            minimum_level=0, feedback=feedback(100, 10))
        self.assertEqual(decision['level'], 11)

    def test_safe_high_band_exit_is_one_step_and_can_restore_exact_endpoint(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 304, 3):
            decision = self.step(controller, now, 16, cpu=50)
            if decision['level'] < 16:
                break
        self.assertEqual(decision['level'], 15)
        self.assertEqual(now, 0)
        with patch('service.time.monotonic', return_value=now):
            device = FakeFan(['on', 'favorite', 16, 900])
            service.fan_apply(device, lambda: service.FanRequest(decision['level'],
                high_wind=controller.high_wind, motion=controller.motion), service.MotorSchedule())
        self.assertEqual(device.writes, [15])
        confirmed = now + 3
        self.step(controller, confirmed, 15, cpu=50)
        for age in range(3, 28, 3):
            decision = self.step(controller, confirmed + age, 15, cpu=50 + age * .1)
            self.assertEqual(decision['level'], 15)
            self.assertEqual(controller.export_thermal_memory()['failures'], [])
        age = 30
        decision = self.step(controller, confirmed + age, 15, cpu=80)
        self.assertEqual(decision['level'], 16)
        with patch('service.time.monotonic', return_value=confirmed + age):
            device = FakeFan(['on', 'favorite', 15, 900])
            service.fan_apply(device, lambda: service.FanRequest(16,
                high_wind=controller.high_wind, motion=controller.motion), service.MotorSchedule())
        self.assertEqual(device.writes, [16])

    def test_pending_exact_restore_renews_through_expiry_and_motor_wait(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 304, 3):
            decision = self.step(controller, now, 16, cpu=50)
            if decision['level'] == 15:
                break
        confirmed = now + 3
        self.step(controller, confirmed, 15, cpu=50)
        for age in range(3, 28, 3):
            now = confirmed + age
            decision = self.step(controller, now, 15, cpu=50 + age * .1)
            self.assertEqual(decision['level'], 15)
            self.assertEqual(controller.export_thermal_memory()['failures'], [])
        now = confirmed + 30
        decision = self.step(controller, now, 15, cpu=80)
        self.assertEqual(decision['level'], 16)
        deadline = decision['execution_trial']['original_deadline']
        schedule = service.MotorSchedule()
        schedule.last_attempt = now - 1
        device = FakeFan(['on', 'favorite', 15, 900])
        for delay in (0, 3, 6, 9):
            if delay:
                decision = self.step(controller, now + delay, 15, cpu=80,
                    confirmed=False, execution='pending')
            self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
            with patch('service.time.monotonic', return_value=now + delay):
                service.fan_apply(device, lambda: service.FanRequest(decision['level'],
                    high_wind=controller.high_wind, motion=controller.motion), schedule)
            self.assertEqual(device.writes, [16] if delay == 9 else [])

    def test_cooled_ceiling_edge_does_not_replay_earlier_hot_restoration(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.step(controller, 0, 16, cpu=50)
        self.step(controller, 3, 15, cpu=50)
        hot = self.step(controller, 6, 15, cpu=80)
        self.assertEqual(hot['level'], 16)
        deadline = hot['execution_trial']['original_deadline']
        cold = self.step(controller, 9, 15, cpu=61, confirmed=False, execution='pending')
        self.assertEqual(cold['level'], 15)
        self.assertEqual(cold['execution_trial']['original_deadline'], deadline)
        self.assertFalse(controller.high_wind.permits(9, 15, 16))
        self.assertTrue(controller.export_thermal_memory()['failures'])
        device = FakeFan(['on', 'favorite', 15, 900])
        with patch('service.time.monotonic', return_value=9):
            service.fan_apply(device, lambda: service.FanRequest(cold['level'],
                high_wind=controller.high_wind, motion=controller.motion), service.MotorSchedule())
        self.assertEqual(device.writes, [])


if __name__ == '__main__':
    unittest.main()
