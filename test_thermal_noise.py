import unittest
from copy import deepcopy

import test_core
import test_equilibrium

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class ThermalNoiseTests(unittest.TestCase):
    def test_stationary_one_degree_range_can_reach_low_gear(self):
        for phase in (-1, 1):
            with self.subTest(phase=phase):
                controller, actual, successes = Controller(HOSTS), 16, set()
                for now in range(0, 2701, 3):
                    decision = controller.decide(fleet(now, cpu=56 + phase * (.5 if now % 6 else -.5)), now,
                                                 minimum_level=1, feedback=feedback(now, actual))
                    trial = decision['trial']
                    if trial and trial['outcome'] == 'success':
                        self.assertEqual(actual, trial['to'])
                        self.assertGreaterEqual(trial['observation_elapsed'], 120)
                        successes.add(actual)
                    actual = decision['level']
                self.assertEqual(actual, 1)
                self.assertEqual(successes, set(range(1, 10)))
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_isolated_safe_one_degree_step_holds_confirmed_low_gear(self):
        controller, now = test_equilibrium.EquilibriumTests().reach(9, cpu=56)
        decision = controller.decide(fleet(now + 3, cpu=57), now + 3,
                                     minimum_level=0, feedback=feedback(now + 3, 9))
        self.assertEqual(decision['level'], 9)
        self.assertNotEqual(decision['trial']['outcome'], 'failed')
        self.assertFalse(decision['learning_eligible'])

    def test_absolute_limit_still_requests_full_airflow(self):
        controller, now = test_equilibrium.EquilibriumTests().reach(9, cpu=56)
        decision = controller.decide(fleet(now + 3, cpu=73), now + 3,
                                     minimum_level=0, feedback=feedback(now + 3, 9))
        self.assertEqual(decision['level'], 16)
        self.assertEqual(decision['scenario'], 'protect')

    def test_one_degree_rebound_to_new_safe_plateau_does_not_fail_edge(self):
        controller, start = test_equilibrium.EquilibriumTests().reach(9, cpu=56)
        actual = 9
        for age in range(3, 184, 3):
            now = start + age
            decision = controller.decide(fleet(now, cpu=57), now,
                                         minimum_level=0, feedback=feedback(now, actual))
            self.assertLessEqual(decision['level'], 9)
            self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
            actual = decision['level']

    def test_rebound_during_confirmed_probe_waits_then_succeeds_at_same_deadline(self):
        for baseline in (51, 56):
            with self.subTest(baseline=baseline):
                controller, requested = test_equilibrium.EquilibriumTests().pending(cpu=baseline)
                for age in range(3, 124, 3):
                    now = requested + age
                    decision = controller.decide(fleet(now, cpu=baseline if age == 3 else baseline + 1), now,
                                                 minimum_level=0, feedback=feedback(now, 9))
                    self.assertEqual(decision['level'], 9)
                    self.assertEqual(decision['trial']['deadline'], requested + 303)
                    self.assertEqual(decision['trial']['outcome'], 'success' if age == 123 else None)
                    self.assertFalse(decision['learning_eligible'])
                    self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
                    if baseline == 56 and 6 <= age < 36:
                        self.assertEqual(decision['scenario'], 'thermal_uncertain')

    def test_pending_rebound_cannot_renew_execution_deadline(self):
        base, requested = test_equilibrium.EquilibriumTests().pending(cpu=56)
        for delayed in (False, True):
            controller = deepcopy(base)
            for age in range(3, 61, 3):
                now = requested + age
                readback = (feedback(now - 3, 9) if delayed and age >= 39 else
                            feedback(now, 10, execution='pending', confirmed=False))
                decision = controller.decide(fleet(now, cpu=57), now, minimum_level=0, feedback=readback)
                if age < 33:
                    self.assertEqual(decision['level'], 9)
                    self.assertIsNone(decision['trial']['observation_elapsed'])
                if delayed and age >= 42:
                    self.assertEqual(decision['trial']['deadline'], requested + 342)
                else:
                    self.assertEqual(decision['trial']['deadline'], requested + 60)
            self.assertEqual(decision['trial']['outcome'], None if delayed else 'execution_timeout')
            self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_late_rebound_cannot_renew_observation_deadline(self):
        controller, requested = test_equilibrium.EquilibriumTests().pending(wifi=True)
        for age in range(3, 304, 3):
            now = requested + age
            readings = (('wifi', 50),) if age == 3 or age % 15 == 0 else ()
            nodes = test_core.ControlTests().sensor_fleet(now, readings, cpu=57 if age >= 294 else 51)
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 9))
            self.assertEqual(decision['trial']['deadline'], requested + 303)
            if age < 303:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertEqual(decision['level'], 10)
        self.assertIn('cause=nonsettled', ' '.join(decision['reasons']))

    def test_two_coherent_steps_revoke_hold_including_half_degree_steps(self):
        base, start = test_equilibrium.EquilibriumTests().reach(9, cpu=56.01)
        for increment in (.5, 1):
            for spacing in (3, 15, 30):
                with self.subTest(increment=increment, spacing=spacing):
                    controller = deepcopy(base)
                    for age in range(3, spacing + 19, 3):
                        cpu = 56.01 + increment * (1 if age < spacing + 3 else 2)
                        now = start + age
                        decision = controller.decide(fleet(now, cpu=cpu), now,
                                                     minimum_level=0, feedback=feedback(now, 9))
                        if age == 3:
                            self.assertEqual(decision['scenario'], 'rebound')
                            self.assertIn(f'deadline={start + 93}', decision['scenario_detail'])
                        if age >= spacing + 3:
                            self.assertFalse(any('[noise]' in reason and 'acpi' in reason
                                                 for reason in decision['reasons']))
                        if decision['level'] > 9:
                            break
                    self.assertGreaterEqual(decision['level'], 9)
                    self.assertFalse(decision['learning_eligible'])
                    if increment == 1 or spacing == 3:
                        self.assertGreater(decision['level'], 9)
                        self.assertEqual(decision['trial']['outcome'], 'failed')

    def test_quantized_slow_drift_stops_descent_and_escalates_before_target(self):
        base, requested = test_equilibrium.EquilibriumTests().pending(cpu=56)
        for period in (30, 90, 180):
            with self.subTest(period=period):
                controller, actual, lowest = deepcopy(base), 9, 9
                for age in range(0, 1201, 3):
                    now = requested + 3 + age
                    cpu = 56 + .5 * (age // period)
                    decision = controller.decide(fleet(now, cpu=cpu), now,
                                                 minimum_level=0, feedback=feedback(now, actual))
                    lowest = min(lowest, decision['level'])
                    actual = decision['level']
                    if actual > 9:
                        break
                self.assertGreater(actual, 9)
                self.assertLess(cpu, 60)
                self.assertGreaterEqual(lowest, 7)
                self.assertEqual(decision['trial']['outcome'], 'failed')

    def test_near_limit_and_hard_override_existing_pending_rebound(self):
        base, start = test_equilibrium.EquilibriumTests().reach(9, cpu=56)
        for temperature in (57.1, 73, 75):
            controller = deepcopy(base)
            controller.decide(fleet(start + 3, cpu=57), start + 3,
                              minimum_level=0, feedback=feedback(start + 3, 9))
            decision = controller.decide(fleet(start + 6, cpu=temperature), start + 6,
                                         minimum_level=0, feedback=feedback(start + 6, 9))
            self.assertGreater(decision['level'], 9)
            if temperature >= 73:
                self.assertEqual(decision['level'], 16)

    def test_second_small_change_cannot_recenter_or_extend_rebound_hold(self):
        controller, start = test_equilibrium.EquilibriumTests().reach(9, cpu=56)
        for age in range(3, 97, 3):
            now = start + age
            decision = controller.decide(fleet(now, cpu=56.5 if age < 24 else 56.75), now,
                                         minimum_level=0, feedback=feedback(now, 9))
            self.assertEqual(decision['level'], 9)
            if age < 93:
                self.assertEqual(decision['scenario'], 'rebound')
                self.assertIn(f'deadline={start + 93}', decision['scenario_detail'])
                self.assertFalse(decision['learning_eligible'])
            else:
                self.assertNotEqual(decision['scenario'], 'rebound')
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_stationary_looking_alternation_with_accumulating_drift_is_not_noise(self):
        controller, requested = test_equilibrium.EquilibriumTests().pending(cpu=56)
        actual = 9
        for age in range(0, 1201, 3):
            now = requested + 3 + age
            cpu = 56 + .5 * int(age / 90) + (.5 if age % 6 else -.5)
            decision = controller.decide(fleet(now, cpu=cpu), now,
                                         minimum_level=0, feedback=feedback(now, actual))
            actual = decision['level']
            if actual > 9:
                break
        self.assertGreater(actual, 9)
        self.assertLess(cpu, 60)
        self.assertFalse(decision['learning_eligible'])

    def test_fault_power_source_and_identity_changes_preempt_rebound_without_blame(self):
        base, start = test_equilibrium.EquilibriumTests().reach(9, cpu=56)
        for event in ('rpm', 'throttle', 'stale', 'timestamp', 'power', 'source', 'unknown', 'identity'):
            with self.subTest(event=event):
                controller = deepcopy(base)
                controller.decide(fleet(start + 3, cpu=57), start + 3,
                                  minimum_level=0, feedback=feedback(start + 3, 9))
                now = start + 6
                nodes, readback = fleet(now, cpu=57), feedback(now, 9)
                if event == 'rpm':
                    readback['rpm'] = 0
                elif event == 'throttle':
                    nodes[0]['thermal_throttle'] = True
                elif event == 'stale':
                    nodes[0]['requested_mono'] = now - 7
                elif event == 'timestamp':
                    nodes[0]['source_ts'] = now - 3
                elif event == 'power':
                    nodes[0]['power_w'] = 80
                elif event == 'source':
                    nodes[0]['power_source'] = 'changed'
                elif event == 'unknown':
                    nodes[0]['power_w'] = None
                else:
                    nodes[0]['sensors'].append(dict(id='new', group='other', temp=40, calibrated=True))
                decision = controller.decide(nodes, now, minimum_level=0, feedback=readback)
                self.assertGreaterEqual(decision['level'], 10)
                self.assertFalse(decision['learning_eligible'])
                self.assertEqual(decision['trial']['outcome'], 'failed' if event == 'throttle' else 'invalidated')
                self.assertEqual(decision['trial']['outcome'], 'failed' if event == 'throttle' else 'invalidated')

    def test_sustained_load_preempts_pending_rebound(self):
        controller, start = test_equilibrium.EquilibriumTests().reach(9, cpu=56)
        for age in (3, 6, 9):
            now = start + age
            decision = controller.decide(fleet(now, cpu=57, load=30), now,
                                         minimum_level=0, feedback=feedback(now, 9))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_warm_wifi_rebound_keeps_raw_missing_sensor_prediction(self):
        controller, start = test_equilibrium.EquilibriumTests().reach(9, wifi=True, readings=(('wifi', 61),))
        for age in (3, 6, 9):
            now = start + age
            nodes = test_core.ControlTests().sensor_fleet(now, (('wifi', 62),), cpu=56)
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 9))
            self.assertEqual(decision['level'], 9)
        nodes = test_core.ControlTests().sensor_fleet(now + 3, (), cpu=56)
        decision = controller.decide(nodes, now + 3, minimum_level=0, feedback=feedback(now + 3, 9))
        self.assertEqual(decision['level'], 16)
        self.assertIn('传感器消失', ' '.join(decision['reasons']))
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_all_sensor_groups_and_both_host_orders_allow_stationary_descent(self):
        for channel in ('cpu', 'gpu', 'board', 'wifi'):
            for reverse in (False, True):
                with self.subTest(channel=channel, reverse=reverse):
                    controller, actual = Controller(HOSTS), 16
                    for now in range(0, 901, 3):
                        temp = (51 if channel in ('cpu', 'gpu') else 61) + (.5 if now % 6 else -.5)
                        nodes = (test_core.ControlTests().sensor_fleet(now, (('wifi', temp),), cpu=56)
                                 if channel == 'wifi' else fleet(now, **{channel: temp}))
                        decision = controller.decide(nodes[::-1] if reverse else nodes, now,
                                                     minimum_level=9, feedback=feedback(now, actual))
                        actual = decision['level']
                    self.assertEqual(actual, 9)
                    self.assertEqual(decision['trial']['outcome'], 'success')
                    self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')


if __name__ == '__main__':
    unittest.main()
