import math
import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class ReviewRegressions(unittest.TestCase):
    def test_idle_unresolved_lower_endpoint_recovers_only_after_power_improves(self):
        original = self.controller()
        for now in range(0, 211, 3):
            self.step(original, now, 8)
        self.step(original, 213, 7)
        restarted, actual = self.controller(), 6
        restarted.restore_thermal_memory(original.export_thermal_memory(), 1000)
        for now in range(1000, 1301, 3):
            actual = self.step(restarted, now, actual)['level']
        self.assertEqual(actual, 6)
        self.assertTrue(restarted.export_thermal_memory()['failures'])
        proven_failure = restarted.export_thermal_memory()
        for failure in proven_failure['failures']:
            failure['failure_class'] = 'thermal_failure'
        first_down = None
        for now in range(1303, 2204, 3):
            before = actual
            actual = self.step(restarted, now, actual, power=36)['level']
            if actual < before and first_down is None:
                first_down = now
        self.assertFalse(restarted.export_thermal_memory()['failures'])
        self.assertIsNotNone(first_down)
        self.assertGreaterEqual(first_down, 1303 + 60 + 180)
        self.assertLess(actual, 6)
        confirmed = self.controller()
        confirmed.restore_thermal_memory(proven_failure, 3000)
        for now in range(3000, 3901, 3):
            self.assertEqual(self.step(confirmed, now, 6, power=36)['level'], 6)
        self.assertTrue(confirmed.export_thermal_memory()['failures'])

    def controller(self):
        return Controller(HOSTS, rules_only=True, high_wind_gate=True)

    def step(self, controller, now, actual=10, rebound=0, load=5, power=45):
        return controller.decide(fleet(now, cpu=38 + rebound, gpu=38 + rebound,
            board=46 + rebound, load=load, power=power), now, minimum_level=0,
            feedback=feedback(now, actual))

    def test_fractional_cadence_creates_anchors_and_stops_unbounded_warming(self):
        for cadence in ('fractional', 'jittered'):
            with self.subTest(cadence=cadence):
                controller, actual, held = self.controller(), 10, False
                for i in range(301):
                    now = i * 3.001 if cadence == 'fractional' else i * 3 + .2 * math.sin(i)
                    decision = self.step(controller, now, actual, rebound=now / 120)
                    actual = decision['level']
                    held |= decision['control_status']['primary_reason'] == 'warming_budget_exhausted'
                anchors = controller.export_thermal_memory()['anchors']
                self.assertEqual(len(anchors), 12)
                self.assertTrue(held)
                self.assertTrue(all(row['peak_c'] - row['anchor_c'] > 2 for row in anchors))
                self.assertGreaterEqual(actual, 9)

    def test_fractional_cadence_updates_existing_anchor_peaks(self):
        controller = self.controller()
        for now in range(0, 91, 3):
            self.step(controller, now, rebound=now / 120)
        initial = controller.export_thermal_memory()['anchors']
        self.assertEqual(len(initial), 12)
        for i in range(1, 181):
            now = 90 + i * 3.001
            self.step(controller, now, rebound=now / 120)
        current = controller.export_thermal_memory()['anchors']
        self.assertTrue(all(new['peak_c'] > old['peak_c'] + 2 for old, new in zip(initial, current)))

    def test_fractional_plateau_rebases_exhausted_budget(self):
        controller, actual = self.controller(), 10
        for now in range(0, 481, 3):
            actual = self.step(controller, now, actual, rebound=now / 120)['level']
        self.assertTrue(any(row['peak_c'] - row['anchor_c'] > 2
                            for row in controller.export_thermal_memory()['anchors']))
        start, resumed = actual, None
        for i in range(1, 241):
            now = 480 + i * 3.001
            decision = self.step(controller, now, actual, rebound=4)
            actual = decision['level']
            if actual < start:
                resumed = now
                break
        self.assertIsNotNone(resumed)
        self.assertLessEqual(resumed, 690)

    def test_restart_other_endpoint_restricts_failed_level_without_freezing_higher_levels(self):
        original = self.controller()
        for now in range(0, 211, 3):
            self.step(original, now, 8, load=25, power=70)
        self.step(original, 213, 7, load=25, power=70)
        saved = original.export_thermal_memory()
        self.assertEqual(saved['open_edge']['stage'], 'confirmed')
        for endpoint in (9, 6, 16):
            with self.subTest(endpoint=endpoint):
                restarted, actual = self.controller(), endpoint
                restarted.restore_thermal_memory(saved, 1000)
                self.assertFalse(restarted.high_wind.allowed)
                for now in range(1000, 1901, 3):
                    decision = self.step(restarted, now, actual, load=25, power=70)
                    self.assertLessEqual(abs(decision['level'] - actual), 1)
                    actual = decision['level']
                self.assertFalse(restarted._restored_edge)
                restrictions = restarted.export_thermal_memory()['failures']
                self.assertTrue(restrictions)
                self.assertEqual({row['failed_level'] for row in restrictions}, {7})
                self.assertEqual({row['failure_class'] for row in restrictions}, {'unresolved_descent'})
                self.assertGreaterEqual(actual, min(endpoint, 8))
                if endpoint == 9:
                    self.assertEqual(actual, 8)
                start = actual
                for now in range(1903, 2804, 3):
                    actual = self.step(restarted, now, actual, load=10, power=56)['level']
                self.assertFalse(restarted.export_thermal_memory()['failures'])
                self.assertLess(actual, start)


if __name__ == '__main__':
    unittest.main()
