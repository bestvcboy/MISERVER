import unittest

import test_low_risk_release as low_risk
from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet
from test_source_equilibrium import oscillation
from thermal_policy import ThermalMemory


WAVE_IDENTITY = (HOSTS[0], 'DGX', 'spbm', 'other:wave', 'other')


def returning_wave(now, amplitude=3):
    phase = now % 90
    return 30 + (amplitude if 9 <= phase < 18 else
                 amplitude + .3 if 39 <= phase < 48 else 0)


def controller_with_debt():
    memory = ThermalMemory()
    memory.add_anchor(WAVE_IDENTITY, 28, 45)
    controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
    controller.restore_thermal_memory(memory.export(), 0)
    return controller


def step(controller, now, temp=None):
    nodes = fleet(now, cpu=38, gpu=38, board=46, load=5, power=45)
    nodes[0]['sensors'].append(dict(id=WAVE_IDENTITY[-2], group='other',
        label='wave', calibrated=True, temp=returning_wave(now) if temp is None else temp))
    return controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 10))


def wave_anchor(controller):
    return next(a for a in controller.export_thermal_memory()['anchors']
                if tuple(a['source']) == WAVE_IDENTITY)


class ExcursionRecoveryTests(unittest.TestCase):
    def test_small_equilibrium_retirement_does_not_consume_full_warming_budget(self):
        for amplitude in (1.999999, 2, 2.000001):
            with self.subTest(amplitude=amplitude):
                points, cycle = [], None
                for now in range(0, 481, 3):
                    temp = 30 if now == 0 else 36 if now == 3 else (
                        34 + (amplitude if now % 12 == 3 else 0))
                    points.append((now, temp))
                    cycle = Controller._returned_cycles(points, now, False, cycle)
                if amplitude < 2:
                    self.assertIsNone(cycle.opening)
                else:
                    self.assertEqual(cycle.opening[0], 0)

    def test_completed_growth_yields_to_fresh_small_returning_waves(self):
        for amplitude in (.8, 1):
            for debt in (False, True):
                with self.subTest(amplitude=amplitude, debt=debt):
                    controller = controller_with_debt() if debt else Controller(
                        HOSTS, rules_only=True, high_wind_gate=True)
                    first = None
                    for now in range(0, 901, 3):
                        temp = next((peak for start, peak in ((39, 33), (99, 34), (159, 35))
                            if start <= now < start + 9), 30)
                        if now >= 210:
                            temp = 30 + (amplitude if 9 <= (now - 210) % 60 < 18 else 0)
                        decision = step(controller, now, temp)
                        if decision['level'] != 10:
                            first = now
                            self.assertEqual(decision['level'], 9)
                            self.assertEqual(decision['execution_trial']['status'], 'pending')
                            break
                    self.assertIsNotNone(first, 'Old growth cannot veto a fresh bounded equilibrium')
                    self.assertGreaterEqual(first, 168 + 180)

    def test_new_small_wave_equilibrium_preserves_positive_drift_protection(self):
        for drift in (0, .001):
            with self.subTest(drift=drift):
                controller = controller_with_debt()
                first = None
                for now in range(0, 901, 3):
                    value = None if now < 210 else 36 if now == 210 else (
                        34 + (1 if 9 <= (now - 210) % 60 < 18 else 0) + drift * (now - 210))
                    decision = step(controller, now, value)
                    if decision['level'] != 10:
                        first = now
                        self.assertEqual(decision['level'], 9)
                        self.assertEqual(decision['execution_trial']['status'], 'pending')
                        break
                if drift:
                    self.assertIsNone(first)
                    self.assertEqual(wave_anchor(controller)['anchor_c'], 28)
                else:
                    self.assertIsNotNone(first)
                    self.assertGreaterEqual(first, 213 + 180)

    def test_completed_growing_pulses_yield_to_fresh_flat_evidence(self):
        for debt in (False, True):
            with self.subTest(debt=debt):
                controller = controller_with_debt() if debt else Controller(
                    HOSTS, rules_only=True, high_wind_gate=True)
                first = None
                for now in range(0, 901, 3):
                    temp = next((peak for start, peak in ((39, 33), (99, 34), (159, 35))
                        if start <= now < start + 9), 30)
                    decision = step(controller, now, temp)
                    if decision['level'] != 10:
                        first = now
                        self.assertEqual(decision['level'], 9)
                        self.assertEqual(decision['execution_trial']['status'], 'pending')
                        break
                self.assertIsNotNone(first, 'Completed growth must not permanently veto a fresh flat equilibrium')
                self.assertGreaterEqual(first, 168 + 180 + 180)

    def test_recent_completed_floor_drift_is_not_diluted_by_old_flat_returns(self):
        points = []
        for now in range(0, 1981, 3):
            floor = 40 + .005 * max(0, now - 1800)
            value = floor + (16.5 if now < 30 else 2) if 9 <= now % 30 < 18 else floor
            points.append((now, value))
        risk = self.cycle_risk(points, target=65, rise=.005, slope=.005,
            guard_projected=points[-1][1] + .005 * 30)
        self.assertEqual(risk.kind, 'refused')
        self.assertAlmostEqual(risk.margin_c, 7.6)

    def test_two_increasing_returns_without_certificate_cannot_use_stationary_fallback(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 241, 3):
            phase = now - 30
            temp = 41.17 if phase < 0 else 42.6 if phase == 99 else oscillation(phase)
            decision = step(controller, now, temp)
            self.assertEqual(decision['level'], 10 if now < 240 else 9)

    def test_missing_recent_cycle_rate_uses_original_raw_risk_without_phase_exemption(self):
        points = [(now, 41 if now >= 210 else 43 if 9 <= now % 30 < 18 else 40)
            for now in range(0, 391, 3)]
        risk = self.cycle_risk(points, target=65)
        self.assertEqual((risk.kind, risk.margin_c), ('ready', 24))
        self.assertEqual(self.cycle_risk(points, target=65, guard_projected=57.000001).kind, 'refused')

    def test_new_flat_equilibrium_above_old_floor_can_supersede_unreturned_history(self):
        controller = controller_with_debt()
        first = None
        for now in range(0, 901, 3):
            value = None if now < 210 else 36 if now == 210 else 34
            decision = step(controller, now, value)
            if decision['level'] != 10:
                first = now
                self.assertEqual(decision['level'], 9)
                self.assertEqual(decision['execution_trial']['status'], 'pending')
                break
        self.assertIsNotNone(first, 'A proven new flat equilibrium cannot require return to the original floor')
        self.assertGreaterEqual(first, 213 + 180 + 180)
        anchor = wave_anchor(controller)
        self.assertEqual((anchor['anchor_c'], anchor['peak_c']), (34, 34))

    def cycle_risk(self, points, *, target=60, **changes):
        cycle = None
        for index, (now, _) in enumerate(points):
            history = points[:index + 1]
            cycle = Controller._returned_cycles(history, now, False, cycle)
        raw = dict(temp=points[-1][1], target=target, guard_projected=points[-1][1],
            rise=0, slope=0, covered=True, rapid_rise=False, persistent_heat=False,
            uncertain=True, step_recent=False)
        raw.update(changes)
        source = dict(uncertain=True, release_margin=target - raw['temp'])
        return Controller._fixed_release_risk(raw, source, points, points[-1][0], False, cycle=cycle)

    def test_returned_cycle_floor_not_positive_phase_drives_bound_projection(self):
        points = [(now, oscillation(now)) for now in range(0, 361, 3)]
        self.assertEqual(self.cycle_risk(points, target=50.4).kind, 'ready')
        self.assertEqual(self.cycle_risk(points, target=50.4).margin_c, 8)
        self.assertEqual(self.cycle_risk(points, target=50.399999).kind, 'refused')
        self.assertEqual(self.cycle_risk(points, guard_projected=52).kind, 'ready')
        self.assertEqual(self.cycle_risk(points, guard_projected=52.000001).kind, 'refused')

    def test_positive_completed_cycle_floor_uses_full_180_second_projection(self):
        points = [(now, (44 if 9 <= now < 18 else 43 if
            39 <= now < 168 and (now - 39) % 30 < 9 else 40) + .001 * now)
            for now in range(0, 211, 3)]
        forecast = 44.015 + 180 * .001
        risk = self.cycle_risk(points, target=forecast + 8)
        self.assertEqual((risk.kind, risk.margin_c), ('ready', 8))
        self.assertEqual(self.cycle_risk(points, target=forecast + 7.999999).kind, 'refused')

    def test_floor_projection_needs_completed_continuous_cycles(self):
        points = [(now, oscillation(now)) for now in range(0, 361, 3)]
        for case in (points[60:], [p for p in points if p[0] not in (150, 153)]):
            self.assertEqual(self.cycle_risk(case, target=50.4).kind, 'refused')

    def test_mixed_size_completed_returns_establish_bound_without_three_large_peaks(self):
        points = [(now, 41.26 if now in (189, 279) else oscillation(now))
                  for now in range(0, 361, 3)]
        risk = self.cycle_risk(points, target=50.4)
        self.assertEqual((risk.kind, risk.margin_c), ('ready', 8))
        self.assertEqual(self.cycle_risk(points, target=50.399999).kind, 'refused')

    def test_cycle_floor_cannot_hide_new_peak_persistence_or_sustained_ramp(self):
        points = [(now, oscillation(now)) for now in range(0, 361, 3)]
        self.assertEqual(self.cycle_risk(points[:-1] + [(360, 42.400001)], target=50.4).kind, 'refused')
        self.assertNotEqual(self.cycle_risk(points, persistent_heat=True).kind, 'ready')
        self.assertEqual(self.cycle_risk(points, rapid_rise=True).kind, 'refused')
        ramp = [(now, value if now <= 300 else 40 + .03 * (now - 300)) for now, value in points]
        self.assertEqual(self.cycle_risk(ramp).kind, 'refused')

    def test_wider_returning_wave_recovers_historical_debt_and_requests_adjacent(self):
        controller = controller_with_debt()
        peak_credit, returns, previous = 0, 0, 30
        for now in range(0, 901, 3):
            temp = returning_wave(now)
            returns += temp == 30 and previous > 30
            previous = temp
            decision = step(controller, now, temp)
            peak_credit = max(peak_credit, decision['operating']['observe_credit'])
            if decision['level'] != 10:
                self.assertEqual(decision['level'], 9)
                self.assertEqual(decision['execution_trial']['status'], 'pending')
                self.assertGreaterEqual(returns, 3, 'need three completed rise/return cycles')
                self.assertGreaterEqual(now, 180)
                anchor = wave_anchor(controller)
                self.assertGreater(anchor['anchor_c'], 28)
                self.assertLessEqual(anchor['peak_c'] - anchor['anchor_c'], 2)
                return
        self.assertEqual(peak_credit, 180, 'fixture must earn a full observation window')
        self.fail(f"no adjacent 10-to-9 request by {now}s; actual=10 "
            f"request={decision['level']} peak_credit={peak_credit} "
            f"completed_returns={returns} anchor={wave_anchor(controller)}")

    def test_genuinely_growing_pulses_do_not_recover_debt_or_descend(self):
        controller = controller_with_debt()
        for now in range(0, 361, 3):
            decision = step(controller, now, returning_wave(now, 3 + .6 * (now // 90)))
            self.assertEqual(decision['level'], 10, f'growing pulse envelope at {now}s')
        self.assertEqual(wave_anchor(controller)['anchor_c'], 28)

    def test_monotone_warming_does_not_recover_debt_or_descend(self):
        controller = controller_with_debt()
        for now in range(0, 361, 3):
            decision = step(controller, now, 30 + .02 * now)
            self.assertEqual(decision['level'], 10, f'monotone warming at {now}s')
        self.assertEqual(wave_anchor(controller)['anchor_c'], 28)

    def prime(self, controller, stop=207, temp=None):
        for now in range(0, stop + 1, 3):
            decision = step(controller, now, temp)
            self.assertEqual(decision['level'], 10)
        return decision

    def test_new_unreturned_peak_cannot_use_previous_returns(self):
        controller = controller_with_debt()
        self.prime(controller)
        for now in range(210, 240, 3):
            decision = step(controller, now, 34)
            self.assertEqual(decision['level'], 10, f'unreturned new peak at {now}s')
        self.assertEqual(wave_anchor(controller)['anchor_c'], 28)

    def test_raw_projection_eight_degree_boundary_still_controls_release(self):
        for endpoint, expected in ((52, 9), (52.000001, 10)):
            with self.subTest(endpoint=endpoint):
                controller = controller_with_debt()
                self.prime(controller, stop=204, temp=50.5)
                decision = step(controller, 207, 51.5)
                self.assertEqual(decision['level'], 10)
                decision = step(controller, 210, endpoint)
                self.assertEqual(decision['demand'], 0)
                self.assertEqual(decision['level'], expected)
                if expected == 9:
                    self.assertEqual(decision['execution_trial']['status'], 'pending')
                else:
                    self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_gap_cannot_borrow_pre_gap_observation_or_anchor_recovery(self):
        controller = controller_with_debt()
        before = self.prime(controller)
        self.assertGreater(before['operating']['observe_credit'], 0)
        decision = step(controller, 219)
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['operating']['observe_credit'], 0)
        self.assertEqual(wave_anchor(controller)['anchor_c'], 28)
        for now in range(222, 399, 3):
            decision = step(controller, now)
            self.assertEqual(decision['level'], 10, f'only {now - 219}s since gap')
            self.assertLessEqual(decision['operating']['observe_credit'], now - 219)
            self.assertEqual(wave_anchor(controller)['anchor_c'], 28)

    def test_alternating_growing_peaks_below_older_ceiling_are_not_ready(self):
        peaks = (49, 47, 46, 47.2, 46.2, 47.4, 46.4)
        points = [(now, peaks[(now - 9) // 24] if
            9 <= now < 162 and (now - 9) % 24 < 9 else 44)
            for now in range(0, 181, 3)]
        risk = low_risk.LowRiskReleaseTests().risk(points, target=65)
        self.assertGreaterEqual(risk.margin_c, 8)
        self.assertNotEqual(risk.kind, 'ready',
            'alternating upper and lower peak subsequences grow below the old 49 C ceiling')

    def test_unreturned_warming_excursion_survives_history_eviction(self):
        controller = controller_with_debt()
        self.prime(controller)
        for now in range(210, 601, 3):
            temp = 36 if now == 210 else 34 + .001 * (now - 210)
            decision = step(controller, now, temp)
            self.assertEqual(decision['level'], 10, f'unreturned warming excursion at {now}s')
            self.assertEqual(wave_anchor(controller)['anchor_c'], 28)
        retained = controller.history[HOSTS[0]][WAVE_IDENTITY[-2]]
        self.assertGreater(retained[0][0], 210, 'exercise actual eviction of the excursion origin')
        self.assertLess(max(value for _, value in retained), 36)

    def test_growth_below_prior_bound_cannot_execute_or_repay_debt(self):
        controller = controller_with_debt()
        for now in range(0, 601, 3):
            if now < 210:
                temp = 36 if 9 <= now < 18 else returning_wave(now)
            else:
                cycle, phase = divmod(now - 210, 24)
                peak = 33 - cycle % 2 + .2 * (cycle // 2)
                temp = peak if 9 <= phase < 18 else 30
            decision = step(controller, now, temp)
            self.assertEqual(decision['level'], 10, f'growth below old ceiling at {now}s')
            self.assertEqual(wave_anchor(controller)['anchor_c'], 28)

    def test_positive_recovered_wave_risk_does_not_repay_historical_debt(self):
        points = [(now, low_risk.recovered_pulses(now) + .001 * now)
                  for now in range(0, 181, 3)]
        assess = low_risk.LowRiskReleaseTests().risk
        risk = assess(points, target=50.546)
        self.assertEqual((risk.kind, risk.margin_c), ('ready', 8))
        self.assertEqual(assess(points, target=50.545).kind, 'refused')
        controller = controller_with_debt()
        for now in range(0, 901, 3):
            decision = step(controller, now, low_risk.recovered_pulses(now) + .001 * now)
            self.assertEqual(decision['level'], 10, f'positive whole-window drift at {now}s')
            self.assertEqual(wave_anchor(controller)['anchor_c'], 28)


if __name__ == '__main__':
    unittest.main()
