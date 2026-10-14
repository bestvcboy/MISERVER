import unittest
from copy import deepcopy
import test_core

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class EquilibriumTests(unittest.TestCase):
    def step(self, controller, now, actual, *, cpu=51, readback=None, wifi=False, readings=(('wifi', 50),),
             minimum=0, manual=None, **values):
        nodes = (test_core.ControlTests().sensor_fleet(now, readings, cpu=cpu, **values) if wifi
                 else fleet(now, cpu=cpu, **values))
        return controller.decide(nodes, now, minimum_level=minimum, manual_level=manual,
                                 feedback=feedback(now, actual) if readback is None else readback)

    def reach(self, level, **values):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 3001, 3):
            decision = self.step(controller, now, actual, **values)
            if actual == level and decision['trial']['outcome'] in ('success', 'recovery_released'):
                return controller, now
            actual = decision['level']
        self.fail(f'no confirmed {level}')

    def pending(self, **values):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1201, 3):
            decision = self.step(controller, now, actual, **values)
            if decision['level'] == 9:
                self.assertEqual(actual, 10)
                self.assertEqual(decision['trial']['status'], 'pending')
                return controller, now
            actual = decision['level']
        self.fail('no warm adjacent probe')

    def run_flat(self, cpu, minimum):
        controller, actual, successes = Controller(HOSTS), 16, set()
        confirmed = {}
        for now in range(0, 2701, 3):
            decision = controller.decide(fleet(now, cpu=cpu), now,
                                         minimum_level=minimum, feedback=feedback(now, actual))
            trial = decision['trial']
            if trial and trial['kind'] == 'probe':
                if trial['status'] == 'observing':
                    confirmed.setdefault(trial['to'], now)
                if trial['outcome'] == 'success':
                    self.assertIn(trial['to'], confirmed)
                    self.assertGreaterEqual(now - confirmed[trial['to']], 120)
                    self.assertEqual(actual, trial['to'])
                    successes.add(actual)
            actual = decision['level']
        self.assertEqual(actual, minimum)
        self.assertEqual(successes, set(range(minimum, 10)))
        return controller, decision

    def test_flat51_reaches_separately_confirmed_low_gears(self):
        for minimum in (1, 4, 0):
            with self.subTest(minimum=minimum):
                self.run_flat(51, minimum)

    def test_flat45_still_reaches_zero(self):
        controller, start = self.reach(0, cpu=45)
        decision = self.step(controller, start + 3, 0, cpu=45)
        self.assertEqual(decision['level'], 0)
        self.assertEqual(decision['trial']['outcome'], 'recovery_released')
        self.assertFalse(decision['learning_eligible'])

    def test_source_requires_90_covered_seconds_then_60_admission(self):
        controller, confirmed = self.reach(10)
        for age in range(3, 151, 3):
            decision = self.step(controller, confirmed + age, 10)
            self.assertEqual(decision['level'], 10 if age < 150 else 9)
        self.assertIn('cold=False', self.step(controller, confirmed + 153, 9)['scenario_detail'])

    def test_source_and_pending_samples_cannot_backdate_destination_evidence(self):
        controller, requested = self.pending()
        for age in range(3, 45, 3):
            decision = self.step(controller, requested + age, 10,
                                 readback=feedback(requested + age - 9, 10, execution='pending', confirmed=False))
            self.assertIsNone(decision['trial']['observation_elapsed'])
        received = requested + 45
        decision = self.step(controller, received, 9, readback=feedback(received - 9, 9))
        self.assertEqual(decision['trial']['observation_elapsed'], 0)
        self.assertEqual(decision['trial']['deadline'], received + 300)
        for age in range(3, 121, 3):
            decision = self.step(controller, received + age, 9)
            self.assertEqual(decision['trial']['outcome'], 'success' if age == 120 else None)

    def test_repeated_readback_cannot_admit_or_complete_but_keeps_elapsed_history(self):
        controller, confirmed = self.reach(10)
        for age in range(3, 151, 3):
            now = confirmed + age
            decision = self.step(controller, now, 10, readback=feedback(now - (3 if age == 150 else 0), 10))
            self.assertEqual(decision['level'], 10)
        decision = self.step(controller, now + 3, 10)
        self.assertEqual(decision['level'], 10)
        decision = self.step(controller, now + 6, 10)
        self.assertEqual(decision['level'], 9)
        requested = now + 6
        for age in range(3, 124, 3):
            now = requested + age
            decision = self.step(controller, now, 9, readback=feedback(now - (3 if age == 123 else 0), 9))
            self.assertIsNone(decision['trial']['outcome'])
        self.assertIsNone(self.step(controller, now + 3, 9)['trial']['outcome'])
        self.assertEqual(self.step(controller, now + 6, 9)['trial']['outcome'], 'success')

    def test_each_gear_can_settle_its_own_small_bounded_rebound(self):
        controller, actual, changed, base = Controller(HOSTS), 16, 0, 51
        prior, successes, first_success = 16, set(), {}
        for now in range(0, 3001, 3):
            if actual != prior:
                changed, prior = now, actual
                if actual < 10:
                    base += .06
            cpu = base - (.06 * max(0, 1 - (now - changed) / 30) if actual < 10 else 0)
            decision = self.step(controller, now, actual, cpu=cpu)
            trial = decision['trial']
            if trial and trial['kind'] == 'probe' and trial['outcome'] == 'success':
                self.assertGreaterEqual(now - changed, 120)
                first_success.setdefault(actual, now)
                successes.add(actual)
            if decision['level'] < actual < 10:
                self.assertGreaterEqual(now - first_success[actual], 60)
            actual = decision['level']
        self.assertEqual(successes, set(range(10)))
        self.assertEqual(actual, 0)
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_sustained_cooling_with_or_without_falling_workload_cannot_admit(self):
        for falling in (False, True):
            with self.subTest(falling=falling):
                controller, confirmed = self.reach(10, cpu=58, load=20, power=50)
                for age in range(3, 481, 3):
                    decision = self.step(controller, confirmed + age, 10, cpu=58 - .025 * age,
                                         load=max(0, 20 - .04 * age) if falling else 20,
                                         power=max(30, 50 - .04 * age) if falling else 50)
                    self.assertGreaterEqual(decision['level'], 10)
                    self.assertNotEqual(decision['trial']['outcome'], 'success')
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_fixed_mean_confounds_in_both_directions_before_and_during_search(self):
        for phase in ('source', 'probe', 'success'):
            for field, delta in (('cpu_load', 15), ('gpu_load', 15), ('power_w', 10)):
                for sign in (-1, 1):
                    with self.subTest(phase=phase, field=field, sign=sign):
                        load = 20 if sign < 0 else 5
                        values = dict(load=load, power=50)
                        if phase == 'probe':
                            controller, start = self.pending(**values)
                            start += 3
                            self.step(controller, start, 9, **values)
                        else:
                            controller, start = self.reach(10 if phase == 'source' else 9, **values)
                        actual = 10 if phase == 'source' else 9
                        for age in range(3, 31, 3):
                            now = start + age
                            nodes = fleet(now, cpu=51, **values)
                            nodes[0][field] += sign * delta
                            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, actual))
                        self.assertGreaterEqual(decision['level'], 10)
                        self.assertEqual(decision['trial']['outcome'], 'invalidated')
                        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
                        self.assertIn('fixed_delta=', ' '.join(decision['reasons']))

    def test_mean_confounds_on_same_frame_as_hard_heat_do_not_lock_an_edge(self):
        for sign in (-1, 1):
            controller, requested = self.pending(power=50)
            for age in range(3, 34, 3):
                nodes = fleet(requested + age, cpu=73 if age == 33 else 51, power=50 if age == 3 else 50 + sign * 10)
                decision = controller.decide(nodes, requested + age, minimum_level=0,
                                             feedback=feedback(requested + age, 9))
            self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['trial']['outcome'], 'invalidated')
            self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_quantized_stationary_warm_trace_recovers_and_probes_below_ten(self):
        controller, actual, successes = Controller(HOSTS), 16, set()
        for now in range(0, 1201, 3):
            decision = self.step(controller, now, actual, cpu=51 + (.5 if now % 6 else -.5))
            if decision['trial'] and decision['trial']['outcome'] == 'success':
                successes.add(actual)
            actual = decision['level']
        self.assertLess(actual, 10)
        self.assertIn(9, successes)
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_cross_gear_drift_keeps_fixed_envelope_and_persistent_heat(self):
        for rate in (.004, .01, .012):
            with self.subTest(rate=rate):
                controller, requested = self.pending(cpu=56)
                actual, lowest = 9, 9
                for age in range(0, 1000, 3):
                    now = requested + 3 + age
                    decision = self.step(controller, now, actual, cpu=56 + rate * age)
                    actual, lowest = decision['level'], min(lowest, decision['level'])
                    if actual >= 10:
                        break
                self.assertGreaterEqual(actual, 10)
                self.assertLess(56 + rate * age, 60)
                if rate >= .01:
                    self.assertEqual(lowest, 9)
                self.assertEqual(decision['trial']['outcome'], 'failed')

    def test_worst_channel_margin_and_manual_cold_only_scope(self):
        for values in (dict(cpu=57.1), dict(gpu=57.1), dict(board=62.1), dict(manual=0)):
            with self.subTest(values=values):
                controller, actual = Controller(HOSTS), 16
                for now in range(0, 1201, 3):
                    decision = self.step(controller, now, actual, **values)
                    self.assertGreaterEqual(decision['level'], 10)
                    actual = decision['level']
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_each_warm_sparse_timeout_needs_fresh_qualification(self):
        controller, requested = self.pending(wifi=True)
        for attempt in (0, 1):
            for age in range(3, 304, 3):
                now = requested + age
                decision = self.step(controller, now, 9, wifi=True,
                                     readings=(('wifi', 50),) if age == 3 or age % 15 == 0 else ())
                if age < 303:
                    self.assertIsNone(decision['trial']['outcome'])
            self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
            self.assertIn('cause=missing_samples', ' '.join(decision['reasons']))
            rolled_back = now
            for age in range(3, 604, 3):
                now = rolled_back + age
                decision = self.step(controller, now, 10, wifi=True)
                if decision['level'] == 9:
                    self.assertEqual(age, 156)
                    requested = now
                    break
                self.assertEqual(decision['level'], 10)
            self.assertEqual(decision['level'], 9)
            self.assertEqual(decision['trial']['status'], 'pending')
            self.assertFalse(decision['retry_blocked'])
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_deadline_hazard_precedes_sparse_timeout_and_requires_safe_recovery(self):
        controller, requested = self.pending(wifi=True)
        for age in range(3, 304, 3):
            now = requested + age
            decision = self.step(controller, now, 9, wifi=True, cpu=73 if age == 303 else 51,
                                 readings=(('wifi', 50),) if age == 3 or age % 15 == 0 else ())
        self.assertEqual(decision['level'], 16)
        self.assertEqual(decision['trial']['outcome'], 'failed')
        self.assertEqual(decision['trial']['outcome'], 'failed')
        controller.protect('restart current evidence')
        actual = 16
        for stamp in range(now + 3, now + 1204, 3):
            decision = self.step(controller, stamp, actual, wifi=True)
            actual = decision['level']
            if actual == 9:
                break
            self.assertGreaterEqual(actual, 10)
        self.assertEqual(actual, 9)
        self.assertEqual(decision['trial']['status'], 'pending')
        self.assertFalse(decision['learning_eligible'])

    def test_warm_context_or_motor_interruptions_invalidate_before_heat_attribution(self):
        base, requested = self.pending()
        self.step(base, requested + 3, 9)
        for event in ('rpm0', 'source', 'identity', 'mode', 'floor'):
            with self.subTest(event=event):
                controller, now = deepcopy(base), requested + 6
                nodes, options, readback = fleet(now, cpu=73), {}, feedback(now, 9)
                if event == 'rpm0':
                    readback['rpm'] = 0
                elif event == 'source':
                    nodes[0]['power_source'] = 'changed'
                elif event == 'identity':
                    nodes[0]['sensors'].append(dict(id='new', group='other', temp=40, calibrated=True))
                elif event == 'mode':
                    options['manual_level'] = 0
                decision = controller.decide(nodes, now, minimum_level=4 if event == 'floor' else 0,
                                             feedback=readback, **options)
                self.assertEqual(decision['level'], 16)
                self.assertEqual(decision['trial']['outcome'], 'invalidated')
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_late_rebound_and_continuing_cooling_cannot_extend_absolute_deadline(self):
        for late_rebound in (False, True):
            with self.subTest(late_rebound=late_rebound):
                controller, requested = self.pending()
                for age in range(0, 301, 3):
                    now = requested + 3 + age
                    cpu = 51 - .025 * age
                    if late_rebound and age > 285:
                        cpu = 43.875 + .04 * (age - 285)
                    decision = self.step(controller, now, 9, cpu=cpu)
                    self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout' if age == 300 else None)
                self.assertEqual(decision['level'], 10)
                self.assertIn('cause=nonsettled', ' '.join(decision['reasons']))
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_full_coverage_arriving_at_exact_deadline_cannot_succeed(self):
        controller, requested = self.pending(wifi=True)
        for age in range(3, 304, 3):
            now = requested + age
            present = age == 3 or age < 210 and age % 15 == 0 or age >= 213
            decision = self.step(controller, now, 9, wifi=True, readings=(('wifi', 50),) if present else ())
            self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout' if age == 303 else None)
        self.assertEqual(decision['level'], 10)
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_local_reference_detects_reversal_within_global_tolerance(self):
        controller, requested = self.pending(load=14)
        for age in range(3, 184, 3):
            now = requested + age
            decision = self.step(controller, now, 9, load=28)
        self.assertEqual(decision['level'], 8)
        confirmed = now + 3
        self.step(controller, confirmed, 8, load=28)
        for age in range(3, 34, 3):
            decision = self.step(controller, confirmed + age, 8, load=0)
            if decision['trial']['outcome'] == 'invalidated':
                break
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_source_mean_change_restarts_full_evidence_before_any_anchor(self):
        controller, confirmed = self.reach(10, power=50)
        for age in range(3, 184, 3):
            now = confirmed + age
            decision = self.step(controller, now, 10, power=40)
            self.assertEqual(decision['level'], 10 if age < 183 else 9)
        self.assertEqual(decision['trial']['status'], 'pending')

    def test_warm_per_sensor_clearance_requires_its_own_covered_window(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                controller, requested = self.pending(wifi=True)
                for age in range(3, 304, 3):
                    now = requested + age
                    present = age == 3 or age % 15 == 0
                    decision = self.step(controller, now, 9, wifi=True,
                                         gpu=40 + .019 * (min(age, 150) if recovered else age),
                                         readings=(('wifi', 50),) if present else ())
                    if age < 303:
                        self.assertIsNone(decision['trial']['outcome'])
                self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
                self.assertIn('cause=missing_samples' if recovered else 'cause=nonsettled',
                              ' '.join(decision['reasons']))
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')


if __name__ == '__main__':
    unittest.main()
