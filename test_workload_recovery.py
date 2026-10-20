import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


def frame(controller, now, *, active=False, cpu=38, readback=None, missing_identity=False, idle_jitter=False):
    nodes = fleet(now, cpu=cpu, gpu=38, board=46,
        load=60 if active else 5, power=100 if active else 45)
    nodes[0]['sensors'].append(dict(id='wifi:repeated', group='wifi', label='repeated',
        calibrated=True, temp=42 if now % 12 == 0 else 40))
    if missing_identity:
        nodes[0]['hostname_verified'] = False
    if idle_jitter:
        for node in nodes:
            node.update(cpu_load=12 if now % 18 < 9 else 1, gpu_load=0,
                power_w=57 if now % 18 < 9 else 42)
    return controller.decide(nodes, now, minimum_level=0,
        feedback=feedback(now, 10) if readback is None else readback)


def prepared():
    controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
    for now in range(0, 69, 3):
        decision = frame(controller, now, active=now >= 60)
    if decision['control_status']['workload_phase'] != 'preparing':
        raise AssertionError('Fixture did not enter preparation')
    return controller


class WorkloadRecoveryTests(unittest.TestCase):
    def test_idle_context_jitter_does_not_require_loaded_capacity_stationarity(self):
        controller = prepared()
        for now in range(69, 130, 3):
            decision = frame(controller, now, idle_jitter=True)
            self.assertEqual(decision['level'], 10)
            if now < 129:
                self.assertEqual(decision['control_status']['workload_phase'], 'preparing')
        self.assertEqual(decision['control_status']['workload_phase'], 'idle')
        self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_idle_context_jitter_does_not_hide_identity_failure(self):
        controller = prepared()
        for now in range(69, 114, 3):
            frame(controller, now, idle_jitter=True)
        frame(controller, 114, idle_jitter=True, missing_identity=True)
        for now in range(117, 177, 3):
            decision = frame(controller, now, idle_jitter=True)
            self.assertEqual(decision['control_status']['workload_phase'], 'preparing')

    def test_idle_retirement_does_not_require_silent_sensors_or_grant_descent(self):
        controller = prepared()
        retired = None
        for now in range(69, 154, 3):
            decision = frame(controller, now)
            self.assertEqual(decision['level'], 10)
            if decision['control_status']['workload_phase'] == 'idle':
                retired = now
                self.assertGreaterEqual(retired, 129)
                self.assertEqual(decision['operating']['observe_credit'], 0)
                break
        self.assertIsNotNone(retired, 'Ended load remains preparing under safe returning Wi-Fi pulses')
        for now in range(retired + 3, retired + 180, 3):
            decision = frame(controller, now)
            self.assertEqual(decision['level'], 10, 'Retirement cannot donate preparation credit to descent')

    def test_active_pulsing_load_keeps_original_preparation_gate(self):
        controller = prepared()
        for now in range(69, 301, 3):
            decision = frame(controller, now, active=True)
            self.assertEqual(decision['control_status']['workload_phase'], 'preparing')
            self.assertEqual(decision['level'], 10)

    def test_hot_or_unverified_or_frozen_evidence_cannot_retire_preparation(self):
        for options in (dict(cpu=55), dict(missing_identity=True),
                dict(readback=feedback(66, 10))):
            with self.subTest(options=options):
                controller = prepared()
                for now in range(69, 181, 3):
                    decision = frame(controller, now, **options)
                    self.assertNotEqual(decision['control_status']['workload_phase'], 'idle')
                for now in range(183, 243, 3):
                    decision = frame(controller, now)
                    self.assertNotEqual(decision['control_status']['workload_phase'], 'idle')

    def test_gap_does_not_complete_pre_gap_idle_interval(self):
        controller = prepared()
        for now in range(69, 114, 3):
            frame(controller, now)
        for now in range(123, 183, 3):
            decision = frame(controller, now)
            self.assertNotEqual(decision['control_status']['workload_phase'], 'idle')

    def test_returning_workload_restarts_idle_retirement_interval(self):
        controller = prepared()
        for now in range(69, 114, 3):
            frame(controller, now)
        frame(controller, 114, active=True)
        for now in range(117, 177, 3):
            decision = frame(controller, now)
            self.assertNotEqual(decision['control_status']['workload_phase'], 'idle')


if __name__ == '__main__':
    unittest.main()
