import unittest
from copy import deepcopy

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


def recovered_pulses(now):
    phase = now % 150
    return 40 + (2 if 15 <= phase < 24 else 2.3 if 60 <= phase < 69 else 0)


class LowRiskReleaseTests(unittest.TestCase):
    def risk(self, points, *, target=60, wifi=False, pending=False, source_margin=None, **changes):
        raw = dict(temp=points[-1][1], target=target, guard_projected=points[-1][1],
            rise=0, slope=0, covered=True, rapid_rise=False, persistent_heat=False,
            uncertain=True, step_recent=False)
        raw.update(changes)
        source = dict(uncertain=True, release_margin=target - raw['temp'])
        if source_margin is not None:
            source['release_margin'] = source_margin
        return Controller._fixed_release_risk(raw, source, points, points[-1][0], wifi,
            pending=pending)

    def test_recovered_expanding_cold_pulses_are_eligible(self):
        points = [(now, recovered_pulses(now)) for now in range(0, 181, 3)]
        risk = self.risk(points)
        self.assertEqual(risk.kind, 'ready', risk)
        self.assertGreaterEqual(risk.margin_c, 8)

    def test_full_window_peak_needs_eight_degrees_even_when_current_is_cold(self):
        points = [(now, recovered_pulses(now)) for now in range(0, 181, 3)]
        for target, kind in ((50.3, 'ready'), (50.299, 'refused')):
            with self.subTest(target=target):
                self.assertEqual(self.risk(points, target=target).kind, kind)
        self.assertEqual(self.risk(points, target=50.3).margin_c, 8)

    def test_full_window_peak_and_positive_drift_share_projection_budget(self):
        points = [(now, recovered_pulses(now) + .001 * now) for now in range(0, 181, 3)]
        for target, kind in ((50.546, 'ready'), (50.545, 'refused')):
            with self.subTest(target=target):
                self.assertEqual(self.risk(points, target=target).kind, kind)
        self.assertEqual(self.risk(points, target=50.546).margin_c, 8)

    def test_current_uncapped_projection_keeps_its_own_eight_degree_boundary(self):
        points = [(now, recovered_pulses(now)) for now in range(0, 181, 3)]
        for guard, kind in ((52, 'ready'), (52.001, 'refused'), (75, 'refused')):
            with self.subTest(guard=guard):
                self.assertEqual(self.risk(points, guard_projected=guard).kind, kind)

    def test_recovery_cannot_increase_ordinary_source_margin(self):
        points = [(now, recovered_pulses(now)) for now in range(0, 181, 3)]
        self.assertEqual(self.risk(points, source_margin=4).margin_c, 4)

    def test_rapid_persistent_and_missing_raw_coverage_still_refuse(self):
        points = [(now, recovered_pulses(now)) for now in range(0, 181, 3)]
        for changes in (dict(rapid_rise=True), dict(persistent_heat=True), dict(covered=False)):
            with self.subTest(changes=changes):
                self.assertEqual(self.risk(points, **changes).kind, 'refused')

    def test_unreturned_new_peak_cannot_use_previously_recovered_range(self):
        points = [(now, recovered_pulses(now)) for now in range(0, 181, 3)]
        points[-1] = (180, 42.301)
        self.assertEqual(self.risk(points).kind, 'refused')
        points[-2:] = [(177, 44), (180, 41)]
        self.assertEqual(self.risk(points).kind, 'refused')

    def test_recent_rising_or_elevated_median_is_not_recovered(self):
        for shape in ('rising', 'elevated'):
            with self.subTest(shape=shape):
                points = [(now, recovered_pulses(now) if now <= 150 else
                    40 + (now - 150) * .011 if shape == 'rising' else 40.6)
                    for now in range(0, 181, 3)]
                self.assertEqual(self.risk(points).kind, 'observe_only')
                self.assertGreaterEqual(self.risk(points).margin_c, 8)
                self.assertEqual(self.risk(points, pending=True).kind, 'refused')

    def test_long_block_medians_must_stay_bounded(self):
        points = [(now, recovered_pulses(now) + (.5 if now > 120 else 0))
                  for now in range(0, 181, 3)]
        self.assertEqual(self.risk(points).kind, 'observe_only')
        self.assertEqual(self.risk(points, pending=True).kind, 'refused')

    def test_sustained_ramp_is_not_a_recovered_excursion(self):
        points = [(now, recovered_pulses(now) if now <= 90 else 40 + (now - 90) * .03)
                  for now in range(0, 181, 3)]
        self.assertEqual(self.risk(points).kind, 'refused')

    def test_wifi_recovery_requires_its_own_window_and_margin(self):
        points = [(now, recovered_pulses(now)) for now in range(0, 181, 3)]
        self.assertEqual(self.risk(points, wifi=True).kind, 'ready')
        self.assertEqual(self.risk(points, wifi=True, target=50.299).kind, 'refused')

    def test_recovery_does_not_mutate_input_evidence(self):
        points = [(now, recovered_pulses(now)) for now in range(0, 181, 3)]
        saved = deepcopy(points)
        self.risk(points)
        self.assertEqual(points, saved)

    def test_whole_controller_recovered_pulses_request_adjacent_descent(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 721, 3):
            nodes = fleet(now, cpu=recovered_pulses(now), gpu=38, board=46, load=5, power=45)
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 10))
            if decision['level'] < 10:
                self.assertEqual(decision['level'], 9)
                self.assertGreaterEqual(now, 180)
                self.assertEqual(decision['execution_trial']['status'], 'pending')
                self.assertEqual(decision['demand'], 0)
                return
        self.fail(f"no adjacent 10-to-9 request; demand={decision['demand']} "
            f"credit={decision['operating']['observe_credit']} "
            f"reason={decision['control_status']['primary_reason']}")

    def test_whole_controller_gap_identity_and_hot_source_revoke_credit(self):
        for case in ('gap', 'identity', 'missing', 'hot', 'zero_rpm'):
            with self.subTest(case=case):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                for now in range(0, 238, 3):
                    nodes = fleet(now, cpu=recovered_pulses(now), gpu=38, board=46, load=5, power=45)
                    decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 10))
                self.assertGreater(decision['operating']['observe_credit'], 0)
                now = 246 if case == 'gap' else 240
                nodes = fleet(now, cpu=recovered_pulses(now), gpu=38, board=46, load=5, power=45)
                physical = feedback(now, 10)
                if case == 'identity':
                    nodes[0]['hostname'] = 'replacement'
                elif case == 'missing':
                    nodes[0]['sensors'].pop()
                elif case == 'hot':
                    nodes[-1]['sensors'][0]['temp'] = nodes[-1]['cpu_temp'] = 75
                elif case == 'zero_rpm':
                    physical['rpm'] = 0
                decision = controller.decide(nodes, now, minimum_level=0, feedback=physical)
                self.assertEqual(decision['operating']['observe_credit'], 0)
                self.assertGreaterEqual(decision['level'], 10)

    def test_whole_controller_growing_pulses_never_accumulate_a_descent(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 721, 3):
            cpu = 40 + (2 + now // 60 * .2 if 15 <= now % 60 < 24 else 0)
            decision = controller.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=5, power=45),
                now, minimum_level=0, feedback=feedback(now, 10))
            self.assertGreaterEqual(decision['level'], 10)
            self.assertLess(decision['operating']['observe_credit'], 180)


if __name__ == '__main__':
    unittest.main()
