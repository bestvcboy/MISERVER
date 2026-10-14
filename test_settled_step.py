import unittest

from core import Controller
import test_core
from test_core import HOSTS
from test_scenarios import feedback


class SettledStepTests(unittest.TestCase):
    def run_history(self, temperature, end=360, override=None, times=None):
        controller, actual, decisions = Controller([HOSTS[-1]]), 16, []
        for now in range(0, end + 1, 3) if times is None else times:
            node = test_core.ControlTests().sensor_fleet(now, (('nvme', temperature(now)),), cpu=51)[-1]
            readback = feedback(now, actual)
            if now == end and override:
                override(node, readback)
            decision = controller.decide([node], now, minimum_level=0, feedback=readback)
            decisions.append(decision)
            actual = decision['level']
        return decisions

    def test_one_degree_step_keeps_qualified_admission_running(self):
        decisions = self.run_history(lambda now: 41 if now < 273 else 42)
        self.assertIn('search_ready=24/60s', decisions[-2]['scenario_detail'])
        self.assertEqual((decisions[-1]['level'], decisions[-1]['scenario']), (10, 'steady_low'))
        self.assertIn('search_ready=27/60s', decisions[-1]['scenario_detail'])

    def test_one_step_reaches_next_probe_without_historical_tie_reset(self):
        decisions = self.run_history(lambda now: 41 if now < 273 else 42, end=516)
        first = next(3 * index for index, decision in enumerate(decisions)
                     if decision['trial'] and decision['trial']['kind'] == 'probe')
        self.assertEqual(first, 393)
        self.assertEqual((decisions[-1]['level'], decisions[-1]['trial']['outcome']), (9, 'success'))
        self.assertFalse(decisions[-1]['learning_eligible'])

    def test_two_half_degree_steps_remain_drift(self):
        decisions = self.run_history(lambda now: 41 if now < 240 else 41.5 if now < 300 else 42)
        self.assertEqual(decisions[-1]['scenario'], 'warming_hold')
        self.assertFalse(decisions[-1]['learning_eligible'])

    def test_identical_bin_intervals_distinguish_isolated_step_from_two_increments(self):
        for repeated, scenario in ((False, 'steady_low'), (True, 'warming_hold')):
            with self.subTest(repeated=repeated):
                decision = self.run_history(
                    lambda now: 40.5 if repeated and now < 210 else 41 if now < 273 else 42)[-1]
                self.assertEqual((decision['level'], decision['scenario']), (10, scenario))
                self.assertEqual(any('[drift]' in reason for reason in decision['reasons']), repeated)
                self.assertEqual(any('[noise]' in reason and 'nvme' in reason
                                     for reason in decision['reasons']), not repeated)
                self.assertFalse(decision['learning_eligible'])

    def test_jitter_and_bin_boundary_offsets_keep_collision_pair_separate(self):
        for offset in (-.25, 0, .25):
            times = [stamp + (offset if stamp % 60 else 0) for stamp in range(0, 361, 3)]
            for repeated, scenario in ((False, 'steady_low'), (True, 'warming_hold')):
                with self.subTest(offset=offset, repeated=repeated):
                    decision = self.run_history(
                        lambda now: 40.5 if repeated and now < 209 else 41 if now < 272 else 42,
                        times=times)[-1]
                    self.assertEqual((decision['level'], decision['scenario']), (10, scenario))
                    self.assertFalse(decision['learning_eligible'])

    def test_odd_and_even_bin_counts_do_not_reset_isolated_step_admission(self):
        for omitted in (None, 246, 300):
            with self.subTest(omitted=omitted):
                decisions = self.run_history(lambda now: 41 if now < 273 else 42,
                                             times=[stamp for stamp in range(0, 361, 3) if stamp != omitted])
                self.assertEqual((decisions[-1]['level'], decisions[-1]['scenario']), (10, 'steady_low'))
                self.assertIn('search_ready=27/60s', decisions[-1]['scenario_detail'])

    def test_additional_changes_and_oversized_jump_cannot_be_excused(self):
        for name, temperature in (
                ('tiny_increment', lambda now: 40.999 if now < 210 else 41 if now < 273 else 42),
                ('down_up', lambda now: 40.5 if 210 <= now < 213 else 41 if now < 273 else 42),
                ('retained_boundary', lambda now: 40.5 if now <= 180 else 41 if now < 273 else 42),
                ('oversized', lambda now: 41 if now < 273 else 42.000001)):
            with self.subTest(name=name):
                decision = self.run_history(temperature)[-1]
                self.assertEqual((decision['level'], decision['scenario']), (10, 'warming_hold'))
                self.assertFalse(decision['learning_eligible'])

    def test_headroom_equality_is_inclusive_but_no_temperature_tolerance_is_added(self):
        for high in (62, 62.000001):
            with self.subTest(high=high):
                decision = self.run_history(lambda now: high - 1 if now < 273 else high)[-1]
                self.assertEqual((decision['level'], decision['scenario']), (10, 'rebound'))
                self.assertFalse(decision['learning_eligible'])
                later = self.run_history(lambda now: high - 1 if now < 273 else high, end=600)[-1]
                self.assertLess(later['level'], 10) if high == 62 else self.assertEqual(later['level'], 10)

    def test_sampling_gap_restarts_protection_instead_of_borrowing_history(self):
        decisions = self.run_history(lambda now: 41 if now < 273 else 42,
                                     times=[stamp for stamp in range(0, 361, 3) if stamp not in (354, 357)])
        self.assertEqual((decisions[-1]['level'], decisions[-1]['scenario']), (16, 'protect'))
        self.assertFalse(decisions[-1]['learning_eligible'])

    def test_another_drifting_sensor_survives_both_sensor_and_fleet_orders(self):
        for reverse_hosts in (False, True):
            for reverse_sensors in (False, True):
                with self.subTest(reverse_hosts=reverse_hosts, reverse_sensors=reverse_sensors):
                    controller, actual = Controller(HOSTS), 16
                    for now in range(0, 361, 3):
                        nodes = test_core.ControlTests().sensor_fleet(now, (
                            ('nvme', 41 if now < 273 else 42),
                            ('nic', 40.5 if now < 210 else 41 if now < 273 else 42)))
                        if reverse_sensors:
                            for node in nodes:
                                node['sensors'].reverse()
                        decision = controller.decide(nodes[::-1] if reverse_hosts else nodes, now,
                                                     minimum_level=0, feedback=feedback(now, actual))
                        actual = decision['level']
                    self.assertEqual((decision['level'], decision['scenario']), (10, 'warming_hold'))
                    self.assertTrue(any('[drift]' in reason and 'nic' in reason for reason in decision['reasons']))
                    self.assertFalse(any('[drift]' in reason and 'nvme' in reason for reason in decision['reasons']))
                    self.assertFalse(decision['learning_eligible'])

    def test_wifi_drift_and_sparse_plateau_cannot_authorize_a_probe(self):
        for period in (3, 15):
            with self.subTest(period=period):
                controller, actual = Controller([HOSTS[-1]]), 16
                for now in range(0, 361, 3):
                    temperature = (47 if now < 240 else 48 if now < 300 else 49) if period == 3 else (
                        41 if now < 273 else 42)
                    readings = (('wifi', temperature),) if now % period == 0 else ()
                    node = test_core.ControlTests().sensor_fleet(now, readings)[-1]
                    decision = controller.decide([node], now, minimum_level=0, feedback=feedback(now, actual))
                    self.assertGreaterEqual(decision['level'], 10)
                    self.assertFalse(decision['learning_eligible'])
                    actual = decision['level']
                self.assertEqual(decision['scenario'], 'warming_hold' if period == 3 else 'protect')
                if period == 15:
                    self.assertIn('earned=0/30s', decision['scenario_detail'])

    def test_continuing_slow_rise_remains_drift(self):
        decisions = self.run_history(lambda now: 40 + now / 120)
        self.assertIn(decisions[-1]['scenario'], ('warming_hold', 'rebound'))
        self.assertFalse(decisions[-1]['learning_eligible'])

    def test_protection_overrides_settled_step(self):
        def hot(node, fan):
            next(sensor for sensor in node['sensors'] if sensor['group'] == 'nvme')['temp'] = 75

        for name, override in (
                ('power', lambda node, fan: node.update(power_w=80)),
                ('stopped_motor', lambda node, fan: fan.update(rpm=0)),
                ('offline', lambda node, fan: node.update(online=False)),
                ('raw_heat', hot)):
            with self.subTest(name=name):
                decision = self.run_history(lambda now: 41 if now < 273 else 42, override=override)[-1]
                if name == 'power':
                    self.assertEqual(decision['level'], 10)
                    self.assertFalse(decision['learning_eligible'])
                else:
                    self.assertEqual((decision['level'], decision['scenario']), (16, 'protect'))


class SettledStepCoverageTests(unittest.TestCase):
    def test_two_exact_constant_runs_and_temperature_bounds(self):
        for delta, high, accepted in ((.5, 42, True), (1, 62, True), (1.000001, 42, False),
                                       (1, 62.000001, False), (0, 42, False), (-1, 42, False)):
            with self.subTest(delta=delta, high=high):
                points = [(stamp, high - delta if stamp < 90 else high) for stamp in range(0, 181, 3)]
                original = points.copy()
                self.assertIs(Controller._settled_step(points, 180, 65, False), accepted)
                self.assertEqual(points, original)

    def test_pre_and_post_need_their_own_thirty_seconds(self):
        for change, accepted in ((30, False), (33, True), (150, True), (153, False)):
            with self.subTest(change=change):
                points = [(stamp, 41 if stamp < change else 42) for stamp in range(0, 181, 3)]
                self.assertIs(Controller._settled_step(points, 180, 65, False), accepted)

    def test_full_coverage_endpoint_and_strict_chronology(self):
        points = [(stamp, 41 if stamp < 90 else 42) for stamp in range(0, 181, 3)]
        self.assertTrue(Controller._settled_step(points, 180, 65, False))
        for name, history in (('short', points[1:]), ('stale', points[:-1]), ('empty', []),
                              ('duplicate', points[:20] + points[19:]),
                              ('reversed', points[:20] + [points[21], points[20]] + points[22:])):
            with self.subTest(name=name):
                self.assertFalse(Controller._settled_step(history, 180, 65, False))

    def test_ordinary_and_wifi_gaps_keep_existing_limits_and_bin_counts(self):
        for wifi, spacing, accepted in ((False, 6, True), (False, 9, False),
                                         (True, 10, True), (True, 12, False), (True, 15, False)):
            with self.subTest(wifi=wifi, spacing=spacing):
                points = [(stamp, 41 if stamp < 90 else 42) for stamp in range(0, 181, spacing)]
                self.assertIs(Controller._settled_step(points, 180, 65, wifi), accepted)
        for wifi, omitted, accepted in ((False, (99,), True), (False, (99, 102), False),
                                         (True, (99, 102, 105), True), (True, (99, 102, 105, 108), False)):
            with self.subTest(wifi=wifi, omitted=omitted):
                points = [(stamp, 41 if stamp < 90 else 42) for stamp in range(0, 181, 3) if stamp not in omitted]
                self.assertIs(Controller._settled_step(points, 180, 65, wifi), accepted)

    def test_every_retained_change_counts_even_before_coverage_boundary(self):
        points = [(stamp, 41 if stamp < 90 else 42) for stamp in range(0, 181, 3)]
        self.assertTrue(Controller._settled_step([(-3, 41)] + points, 180, 65, False))
        for history in ([(-3, 40.999)] + points, [(0, 40.999)] + points[1:],
                        points[:20] + [(60, 40.5)] + points[21:], points[:-1] + [(180, 42.000001)]):
            with self.subTest(history=history):
                self.assertFalse(Controller._settled_step(history, 180, 65, False))


if __name__ == '__main__':
    unittest.main()
