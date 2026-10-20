import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class FixedNoiseTests(unittest.TestCase):
    def controller(self):
        return Controller(HOSTS, rules_only=True, high_wind_gate=True)

    def sample(self, controller, now, actual=8, *, cpu=47, board=54, wifi=None):
        nodes = fleet(now, cpu=cpu, gpu=44, board=board, power=45, load=5)
        if wifi is not None:
            nodes[1]['sensors'].append(dict(id='wifi:repro:temp1', group='wifi',
                label='temp1', temp=wifi, calibrated=True))
        return controller.decide(nodes, now, minimum_level=0,
            feedback=feedback(now, actual))

    def test_safe_three_point_wifi_rise_does_not_request_ten(self):
        controller = self.controller()
        for now in range(0, 99, 3):
            value = 45 if now < 93 else 47 if now == 93 else 49
            decision = self.sample(controller, now, wifi=value)
        self.assertLessEqual(decision['level'], 8)
        self.assertEqual(decision['demand'], 0)
        self.assertFalse(decision['retry_blocked'])

    def test_production_path_classifies_stationary_noise(self):
        controller = self.controller()
        classified = []
        for now in range(0, 123, 3):
            decision = self.sample(controller, now, cpu=43 + .5 * (now % 6 == 0))
            classified.extend(reason for reason in decision['reasons']
                if reason.startswith('[noise]'))
            self.assertLessEqual(decision['level'], 8)
        self.assertTrue(classified)

    def test_raw_critical_temperature_remains_independent(self):
        controller = self.controller()
        for now in range(0, 93, 3):
            self.sample(controller, now, cpu=43 + .5 * (now % 6 == 0))
        decision = self.sample(controller, 93, cpu=75)
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['normal_ceiling'], 10)
        self.assertEqual(controller.motion.actual_c, 75)

    def test_same_source_near_target_still_requests_cooling(self):
        controller = self.controller()
        for now in range(0, 93, 3):
            self.sample(controller, now)
        decision = self.sample(controller, 93, board=66)
        self.assertGreater(decision['level'], 8)
        self.assertGreater(decision['demand'], 8)

    def test_cold_cpu_drift_does_not_borrow_stationary_board_margin(self):
        controller = self.controller()
        actual = 10
        edges = []
        for now in range(0, 241, 3):
            decision = self.sample(controller, now, actual,
                cpu=45 + now * .001, board=57.001)
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertEqual(edges, [(210, 10, 9)])

    def test_sustained_same_source_heating_still_increases(self):
        controller = self.controller()
        decisions = []
        for now in range(0, 123, 3):
            decisions.append(self.sample(controller, now,
                cpu=47 if now < 60 else 47 + (now - 60) * .2))
        self.assertTrue(any(d['level'] > 8 for d in decisions))

    def test_warming_source_with_less_than_eight_headroom_blocks_descent(self):
        controller = self.controller()
        for now in range(0, 241, 3):
            decision = self.sample(controller, now, 10,
                cpu=53 + now * .001, board=50)
            self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['demand'], 0)

    def test_raw_rapid_critical_projection_still_preempts_filter(self):
        controller = self.controller()
        for now in range(0, 93, 3):
            self.sample(controller, now)
        for now, cpu in ((93, 50), (96, 53), (99, 56)):
            decision = self.sample(controller, now, cpu=cpu)
        self.assertGreater(decision['level'], 8)
        self.assertTrue(any(r.startswith('[hard]') for r in decision['reasons']))

    def test_unknown_identity_cannot_classify_noise(self):
        controller = self.controller()
        for now in range(0, 93, 3):
            self.sample(controller, now, cpu=43 + .5 * (now % 6 == 0))
        nodes = fleet(93, cpu=43, gpu=44, board=54, power=45, load=5)
        nodes[1].update(hostname=None, hostname_verified=False)
        decision = controller.decide(nodes, 93, minimum_level=0, feedback=feedback(93, 8))
        self.assertEqual(decision['operating']['observe_credit'], 0)
        self.assertFalse(any(r.startswith('[noise]') for r in decision['reasons']))

    def test_zero_rpm_cannot_classify_noise_or_earn_credit(self):
        controller = self.controller()
        for now in range(0, 93, 3):
            self.sample(controller, now, cpu=43 + .5 * (now % 6 == 0))
        nodes = fleet(93, cpu=43, gpu=44, board=54, power=45, load=5)
        decision = controller.decide(nodes, 93, minimum_level=0,
            feedback=feedback(93, 8, rpm=0))
        self.assertEqual(decision['operating']['observe_credit'], 0)
        self.assertFalse(any(r.startswith('[noise]') for r in decision['reasons']))

    def test_external_protection_rebuilds_noise_coverage(self):
        controller = self.controller()
        for now in range(0, 93, 3):
            self.sample(controller, now, cpu=43 + .5 * (now % 6 == 0))
        controller.protect('storage interruption', 91)
        decision = self.sample(controller, 93, cpu=43)
        self.assertEqual(decision['operating']['observe_credit'], 0)
        self.assertFalse(any(r.startswith('[noise]') for r in decision['reasons']))

    def test_power_context_change_requires_new_noise_coverage(self):
        controller = self.controller()
        for now in range(0, 93, 3):
            self.sample(controller, now, cpu=43 + .5 * (now % 6 == 0))
        for now in range(93, 123, 3):
            nodes = fleet(now, cpu=43 + .5 * (now % 6 == 0),
                gpu=44, board=54, power=60, load=5)
            decision = controller.decide(nodes, now, minimum_level=0,
                feedback=feedback(now, 8))
            self.assertFalse(any(r.startswith('[noise]') for r in decision['reasons']))

    def test_repeated_source_timestamp_cannot_classify_noise(self):
        controller = self.controller()
        for now in range(0, 93, 3):
            self.sample(controller, now, cpu=43 + .5 * (now % 6 == 0))
        nodes = fleet(93, cpu=43, gpu=44, board=54, power=45, load=5)
        nodes[1]['source_ts'] -= 3
        decision = controller.decide(nodes, 93, minimum_level=0, feedback=feedback(93, 8))
        self.assertEqual(decision['operating']['observe_credit'], 0)
        self.assertFalse(any(r.startswith('[noise]') for r in decision['reasons']))

    def test_manual_mode_does_not_use_new_noise_acceptance(self):
        controller = self.controller()
        for now in range(0, 123, 3):
            nodes = fleet(now, cpu=43 + .5 * (now % 6 == 0),
                gpu=44, board=54, power=45, load=5)
            decision = controller.decide(nodes, now, minimum_level=8,
                manual_level=8, feedback=feedback(now, 8))
            self.assertFalse(any(r.startswith('[noise]') for r in decision['reasons']))

    def test_corroborated_wifi_forecast_increases_before_actual_target(self):
        controller = self.controller()
        raised = []
        for now in range(0, 159, 3):
            wifi = 45 if now <= 90 else 45 + (now - 90) * .2
            decision = self.sample(controller, now, wifi=wifi)
            if decision['level'] > 8:
                raised.append((now, wifi, decision))
        self.assertTrue(raised)
        self.assertLess(max(wifi for _, wifi, _ in raised), 60)
        self.assertFalse(any(r.startswith('[hard]') for _, _, d in raised for r in d['reasons']))

    def test_workload_epoch_end_starts_release_credit_at_zero(self):
        controller = self.controller()
        actual, seen_workload = 8, False
        for now in range(0, 150, 3):
            nodes = fleet(now, cpu=47, gpu=44, board=54, power=45,
                load=35 if now < 30 else 5)
            decision = controller.decide(nodes, now, minimum_level=0,
                feedback=feedback(now, actual))
            actual = decision['level']
            active = any(r.startswith('[workload]') for r in decision['reasons'])
            if seen_workload and not active:
                self.assertEqual(decision['operating']['observe_credit'], 0)
                break
            seen_workload = seen_workload or active
        else:
            self.fail('workload epoch did not complete in the fixture')

    def test_other_idle_node_variation_does_not_disable_wifi_acceptance(self):
        controller = self.controller()
        for now in range(0, 99, 3):
            nodes = fleet(now, cpu=47, gpu=44, board=54, power=45, load=5)
            nodes[0]['cpu_load'] = 1 + (now % 18) / 3 * 2.5
            nodes[1]['sensors'].append(dict(id='wifi:repro:temp1', group='wifi',
                label='temp1', temp=45 if now < 93 else 47 if now == 93 else 49,
                calibrated=True))
            decision = controller.decide(nodes, now, minimum_level=0,
                feedback=feedback(now, 8))
        self.assertEqual(decision['demand'], 0)
        self.assertEqual(decision['level'], 8)

    def test_idle_variation_does_not_create_new_release_reset(self):
        controller = self.controller()
        actual, edges = 10, []
        for now in range(0, 241, 3):
            nodes = fleet(now, cpu=47, gpu=44, board=54, power=45, load=5)
            nodes[0]['cpu_load'] = 1 + (now % 18) / 3 * 2.5
            decision = controller.decide(nodes, now, minimum_level=0,
                feedback=feedback(now, actual))
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertEqual(edges, [(210, 10, 9)])

    def test_same_source_180_second_risk_eight_degree_boundary_is_strict(self):
        for endpoint, expected in ((51.82, 9), (51.821, 10)):
            with self.subTest(endpoint=endpoint):
                controller = self.controller()
                for now in range(0, 211, 3):
                    decision = self.sample(controller, now, 10,
                        cpu=endpoint + (now - 210) * .001, board=50)
                self.assertEqual(decision['level'], expected)

    def test_interrupted_feedback_does_not_bridge_release_credit(self):
        for flag, value in (('protection_interrupted', True),
                            ('readback_failure', 'initial_transport')):
            with self.subTest(flag=flag):
                controller = self.controller()
                for now in range(0, 132, 3):
                    decision = controller.decide(
                        fleet(now, cpu=47, gpu=44, board=54, power=45, load=5),
                        now, minimum_level=0, feedback=feedback(now, 10,
                            **({flag: value} if now == 123 else {})))
                    if now in (123, 126):
                        self.assertEqual(decision['operating']['observe_credit'], 0)
                self.assertEqual(decision['operating']['observe_credit'], 3)

    def test_unstable_idle_source_uses_robust_provisional_forecast(self):
        controller = self.controller()
        for now in range(0, 99, 3):
            nodes = fleet(now, cpu=47, gpu=44, board=54, power=45, load=5)
            nodes[1]['cpu_load'] = 1 + (now % 18) / 3 * 2.5
            nodes[1]['sensors'].append(dict(id='wifi:repro:temp1', group='wifi',
                label='temp1', temp=45 if now < 93 else 47 if now == 93 else 49,
                calibrated=True))
            decision = controller.decide(nodes, now, minimum_level=0,
                feedback=feedback(now, 8))
        self.assertEqual(decision['demand'], 0)
        self.assertEqual(decision['level'], 8)
        self.assertEqual(decision['operating']['observe_credit'], 0)
        self.assertTrue(any(r.startswith('[provisional]') and HOSTS[1] in r
            for r in decision['reasons']))

    def test_provisional_source_keeps_raw_release_margin(self):
        controller = self.controller()
        for now in range(0, 96, 3):
            nodes = fleet(now, cpu=53.9 if now < 93 else 54.4,
                gpu=44, board=54, power=45 if now < 93 else 60, load=5)
            decision = controller.decide(nodes, now, minimum_level=0,
                feedback=feedback(now, 10))
            if now == 90:
                self.assertGreater(decision['operating']['observe_credit'], 0)
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['demand'], 0)
        self.assertEqual(decision['operating']['observe_credit'], 0)


if __name__ == '__main__':
    unittest.main()
