import json
import tempfile
import unittest
from copy import deepcopy

import test_core
from core import Controller, Journal
from test_core import HOSTS, node


def fleet(now, **values):
    values.setdefault("cpu", 51)
    nodes = [node(host, now, **values) for host in HOSTS]
    for index, sample in enumerate(nodes):
        sample['power_source'] = 'spbm' if index < 2 else 'estimate:gpu'
    return nodes


def feedback(now, actual=16, **changes):
    result = dict(configured=True, confirmed=True, level=actual, rpm=900,
                  power='on', mode='favorite', fault=False, execution='matched',
                  observed_mono=now)
    result.update(changes)
    return result


def heat_to_target_risk(controller, requested, make_nodes=fleet):
    for age in range(3, 94, 3):
        now = requested + age
        decision = controller.decide(make_nodes(now, cpu=51 + (age - 3) * .1), now,
                                     minimum_level=0, feedback=feedback(now, 9))
        if decision['level'] >= 10:
            assert decision['scenario'] in ('rollback', 'recovery', 'graded_pending'), decision
            assert decision['trial']['outcome'] == 'failed', decision
            return now, decision
    raise AssertionError('target-risk fixture never reached real rollback')


class ScenarioTests(unittest.TestCase):
    def step(self, controller, now, actual=16, nodes=None, readback=None, **options):
        return controller.decide(fleet(now) if nodes is None else nodes, now,
                                 minimum_level=0,
                                 feedback=feedback(now, actual) if readback is None else readback,
                                 **options)

    def baseline(self, controller, stop=60, **options):
        for now in range(0, stop + 1, 3):
            decision = self.step(controller, now, **options)
        return decision

    def test_startup_missing_feedback_and_explicit_baseline(self):
        controller = Controller(HOSTS)
        for now in range(0, 303, 3):
            decision = controller.decide(fleet(now), now, minimum_level=0)
            self.assertEqual(decision['level'], 16)
        self.assertFalse(decision['learning_eligible'])
        controller = Controller(HOSTS)
        self.assertEqual(self.baseline(controller, 57)['level'], 16)
        decision = self.step(controller, 60)
        self.assertEqual(decision['level'], 15)
        self.assertEqual(decision['trial']['status'], 'pending')
        self.assertIsNone(decision['trial']['observation_elapsed'])

    def test_delayed_actual_confirmation_starts_observation(self):
        controller = Controller(HOSTS)
        self.baseline(controller)
        for now in range(63, 90, 3):
            decision = self.step(controller, now, readback=feedback(now, 16, confirmed=False, execution='pending'))
            self.assertEqual(decision['level'], 15)
            self.assertIsNone(decision['trial']['observation_elapsed'])
        decision = self.step(controller, 90, actual=15)
        self.assertEqual(decision['trial']['outcome'], 'recovery_released')
        self.assertFalse(decision['learning_eligible'])
        for now in range(93, 120, 3):
            self.assertEqual(self.step(controller, now, actual=15)['level'], 15)
        self.assertEqual(self.step(controller, 120, actual=15)['level'], 14)

    def test_invalid_feedback_fails_closed(self):
        cases = [dict(configured=False), dict(confirmed=False, execution='unknown'),
                 dict(level=True), dict(level=-1), dict(level=17), dict(rpm=0),
                 dict(rpm=float('nan')), dict(observed_mono=-10), dict(observed_mono=1),
                 dict(observed_mono=True), dict(power='off'), dict(mode='auto'),
                 dict(fault=True), dict(execution='fault')]
        for changes in cases:
            with self.subTest(changes=changes):
                decision = self.step(Controller(HOSTS), 0, readback=feedback(0, **changes))
                self.assertEqual(decision['level'], 16)
                self.assertEqual(decision['scenario'], 'protect')
                self.assertFalse(decision['learning_eligible'])

    def test_protect_is_public_and_journal_keeps_scenario(self):
        controller = Controller(HOSTS)
        self.baseline(controller)
        decision = controller.protect('storage')
        self.assertEqual(decision['level'], 16)
        self.assertFalse(decision['learning_eligible'])
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append(dict(ts=1, nodes=[], decision=decision))
            saved = journal.history(0, 2)['samples'][0]['decision']
        for field in ('scenario', 'scenario_detail', 'learning_eligible', 'trial', 'retry_blocked'):
            self.assertEqual(saved[field], decision[field])
        json.dumps(decision, allow_nan=False)

    def reach(self, level):
        controller = Controller(HOSTS)
        actual = 16
        for now in range(0, 4000, 3):
            decision = self.step(controller, now, actual=actual)
            if decision['level'] == level and decision['trial']['outcome'] in ('success', 'recovery_released'):
                return controller, now
            actual = decision['level']
        self.fail(f'no confirmed level {level}')

    def probe(self):
        controller, end = self.reach(10)
        for now in range(end + 3, end + 154, 3):
            decision = self.step(controller, now, actual=10)
            if decision['level'] == 9:
                break
        self.assertEqual(decision['level'], 9)
        self.assertIsNone(decision['trial']['outcome'])
        return controller, now

    def test_isolated_cpu_gpu_spike_is_not_sustained_load_proof(self):
        for component in ('cpu_load', 'gpu_load'):
            with self.subTest(component=component):
                controller, end = self.reach(14)
                for elapsed in (3, 6, 9):
                    nodes = fleet(end + elapsed)
                    nodes[-1][component] = 70
                    decision = self.step(controller, end + elapsed, actual=14, nodes=nodes)
                    self.assertEqual(decision['level'], 14)
                self.assertIn('[load]', ' '.join(decision['reasons']))
                self.assertFalse(decision['learning_eligible'])

    def test_target_risk_rolls_back_and_fault_or_mode_requires_new_current_evidence(self):
        controller, requested = self.probe()
        now, decision = heat_to_target_risk(controller, requested)
        self.assertGreaterEqual(decision['level'], 10)
        self.assertEqual(decision['trial']['outcome'], 'failed')
        self.assertFalse(decision['retry_blocked'])
        for manual in (0, None):
            self.assertEqual(controller.protect('fault pulse')['level'], 16)
            actual = 16
            for age in range(3, 1203, 3):
                decision = self.step(controller, now + age, actual=actual, manual_level=manual,
                                     nodes=fleet(now + age, cpu=45 if manual == 0 else 51))
                self.assertFalse(decision['learning_eligible'])
                if decision['level'] == 9:
                    break
                self.assertGreaterEqual(decision['level'], 10)
                actual = decision['level']
            self.assertEqual(decision['level'], 9)
            self.assertEqual(decision['trial']['status'], 'pending')
            self.assertFalse(decision['retry_blocked'])
            now += age
        self.assertEqual(self.baseline(Controller(HOSTS))['level'], 15)

    def test_decelerating_rebound_below_ten_waits_for_settlement(self):
        controller, end = self.reach(9)
        for now in range(end + 3, end + 61, 3):
            decision = self.step(controller, now, actual=9)
        self.assertEqual(decision['level'], 8)
        confirmed = end + 63
        for now in range(confirmed, confirmed + 124, 3):
            age = now - confirmed
            cpu = 51 + .03 * min(age, 30) + .01 * min(max(age - 30, 0), 30)
            decision = self.step(controller, now, actual=8, nodes=fleet(now, cpu=cpu))
            self.assertEqual(decision['level'], 8)
            if age < 120:
                self.assertNotEqual(decision['trial']['outcome'], 'success')
        self.assertEqual(decision['trial']['outcome'], 'success')

    def test_trial_confounds_are_not_failed_thermal_edges(self):
        for kind in ('source', 'power', 'manual', 'gear', 'missing_power'):
            with self.subTest(kind=kind):
                controller, requested = self.probe()
                self.step(controller, requested + 3, actual=9)
                nodes = fleet(requested + 6)
                options = {}
                actual = 9
                if kind == 'source':
                    nodes[-1]['power_source'] = 'other'
                elif kind == 'power':
                    nodes[-1]['power_w'] = 80
                elif kind == 'missing_power':
                    nodes[-1]['power_w'] = None
                elif kind == 'manual':
                    options['manual_level'] = 0
                else:
                    actual = 8
                decision = self.step(controller, requested + 6, actual=actual, nodes=nodes, **options)
                self.assertFalse(decision['learning_eligible'])
                self.assertEqual(decision['trial']['outcome'], 'invalidated')
                self.assertFalse(decision['retry_blocked'])

    def test_duplicate_feedback_does_not_create_observation_coverage(self):
        controller, requested = self.probe()
        for age in range(3, 123, 3):
            now = requested + age
            stamp = now - (3 if age % 6 == 0 else 0)
            decision = self.step(controller, now, readback=feedback(stamp, 9))
            self.assertNotEqual(decision['trial']['outcome'], 'success')
        decision = self.step(controller, requested + 123, actual=9)
        self.assertIsNone(decision['trial']['outcome'])
        for age in range(126, 247, 3):
            decision = self.step(controller, requested + age, actual=9)
        self.assertEqual(decision['trial']['outcome'], 'success')
        controller, requested = self.probe()
        self.step(controller, requested + 3, actual=9)
        for age in (6, 9, 12):
            decision = self.step(controller, requested + age, readback=feedback(requested + 3, 9))
            self.assertEqual(decision['trial']['observation_elapsed'], 0)
            self.assertEqual(decision['trial']['status'], 'observing')
        self.assertEqual(self.step(controller, requested + 15, readback=feedback(requested + 3, 9))['scenario'], 'protect')

    def test_power_unknown_freezes_admission_and_policy_can_only_strengthen(self):
        for power in (None, float('nan'), float('inf')):
            controller = Controller(HOSTS)
            for now in range(0, 303, 3):
                nodes = fleet(now)
                nodes[-1]['power_w'] = power
                decision = self.step(controller, now, nodes=nodes)
                self.assertEqual(decision['level'], 16)
                self.assertFalse(decision['learning_eligible'])
        controller = Controller(HOSTS)
        self.baseline(controller)
        decision = self.step(controller, 63, actual=15,
                             power_thresholds={HOSTS[0]: dict(source='spbm', threshold_w=40)},
                             nodes=fleet(63, power=45))
        self.assertEqual(decision['level'], 15)
        self.assertFalse(decision['learning_eligible'])

    def test_intermittent_wifi_and_duplicate_fan_snapshots_complete_descent(self):
        controller = Controller(HOSTS)
        changes, outcomes = [], []
        last = 16
        sensors = test_core.ControlTests().sensor_fleet
        for now in range(0, 130, 3):
            present = now % 6 == 0
            nodes = sensors(now, (("wifi", 50),) if present else ())
            independent_readback_stamp = now - now % 6
            actual = 16 if independent_readback_stamp < 66 else 15 if independent_readback_stamp < 102 else 14
            decision = self.step(controller, now, readback=feedback(independent_readback_stamp, actual), nodes=nodes)
            if decision['level'] != last:
                changes.append((now, decision['level']))
                last = decision['level']
            if decision['trial'] and decision['trial']['outcome'] == 'recovery_released':
                outcomes.append(decision['trial']['to'])
            if not present:
                self.assertFalse(decision['learning_eligible'])
                for host in HOSTS[2:]:
                    observation = controller.expected[host]['wifi:/sys/devices/wifi0:1:wifi0']
                    self.assertEqual(observation['seen_at'], now - 3)
        self.assertEqual(changes, [])
        self.assertEqual(outcomes, [])
        actual = 16
        for now in range(132, 196, 3):
            decision = self.step(controller, now, actual=actual, nodes=sensors(now))
            actual = decision['level']
        self.assertEqual(actual, 15)

    def test_wifi_exact_grace_and_unsafe_last_reading(self):
        sensors = test_core.ControlTests().sensor_fleet
        for temperature in (50, 64, 75):
            with self.subTest(temperature=temperature):
                controller = Controller(HOSTS)
                self.step(controller, 0, nodes=sensors(0, (("wifi", temperature),)))
                for now in range(3, 31, 3):
                    decision = self.step(controller, now, nodes=sensors(now, ()))
                    if temperature == 50:
                        self.assertNotIn('传感器消失', ' '.join(decision['reasons']))
                    else:
                        self.assertIn('传感器消失', ' '.join(decision['reasons']))
                decision = self.step(controller, 30.000001, nodes=sensors(30.000001, ()))
                self.assertEqual(decision['level'], 16)
                self.assertIn('传感器消失', ' '.join(decision['reasons']))

    def test_fixed_reference_load_change_restores_ten_without_claiming_loaded_evidence(self):
        controller, end = self.reach(8)
        actual = 8
        for now in range(end + 3, end + 34, 3):
            decision = self.step(controller, now, actual=actual, nodes=fleet(now, load=15))
            self.assertEqual(decision['level'], 8 if now < end + 30 else 10)
            self.assertNotEqual(decision['scenario'], 'unverified_load')
            self.assertFalse(decision['learning_eligible'])
            actual = decision['level']
        self.assertEqual(controller.trial['outcome'], 'invalidated')
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_all_manual_levels_use_confirmed_steps_and_never_automatic_success(self):
        for manual in range(17):
            with self.subTest(manual=manual):
                controller = Controller(HOSTS)
                actual = 16
                for now in range(0, 4000, 3):
                    decision = self.step(controller, now, actual=actual, manual_level=manual, nodes=fleet(now, cpu=42))
                    self.assertGreaterEqual(decision['level'], manual)
                    if actual == manual and decision['level'] == manual and decision['scenario'] == 'manual':
                        break
                    actual = decision['level']
                end = now
                self.assertEqual(decision['level'], manual)
                self.assertEqual(decision['scenario'], 'manual')
                if manual < 10:
                    self.assertEqual(decision['trial']['outcome'], 'manual_complete')
                elif manual < 16:
                    self.assertEqual(decision['trial']['outcome'], 'recovery_released')
                hot = self.step(controller, end + 3, actual=manual, manual_level=manual,
                                nodes=fleet(end + 3, cpu=73))
                self.assertEqual(hot['level'], 16)

    def test_faults_in_every_phase_and_manual_mode_preserve_guards(self):
        phases = {}
        controller = Controller(HOSTS)
        self.baseline(controller, 57)
        phases['startup'] = (deepcopy(controller), 60, 16)
        self.step(controller, 60)
        phases['pending_release'] = (deepcopy(controller), 63, 16)
        self.step(controller, 63, actual=15)
        phases['released'] = (deepcopy(controller), 66, 15)
        controller, requested = self.probe()
        phases['pending_probe'] = (deepcopy(controller), requested + 3, 10)
        self.step(controller, requested + 3, actual=9)
        phases['observing'] = (deepcopy(controller), requested + 6, 9)
        now, rollback = heat_to_target_risk(controller, requested + 3)
        self.assertEqual(controller.phase, 'rollback')
        self.assertGreaterEqual(rollback['level'], 10)
        phases['rollback'] = (deepcopy(controller), now + 3, 9)
        held, end = self.reach(14)
        phases['hold'] = (held, end + 3, 14)
        kinds = ('cpu', 'gpu', 'board', 'power', 'learned', 'rapid', 'projection', 'throttle',
                 'offline', 'missing', 'stale', 'source', 'ambiguous', 'sensor', 'load_missing', 'fan')
        for phase, (saved, now, actual) in phases.items():
            for manual in (None, 0, 7, 16):
                for host in range(4):
                    for kind in kinds:
                        with self.subTest(phase=phase, manual=manual, host=host, kind=kind):
                            controller = deepcopy(saved)
                            event_time = now
                            if kind == 'rapid':
                                rapid_start = max(sample['temp'] for sample in saved.expected[HOSTS[host]].values()
                                                  if sample['group'] in ('cpu', 'acpi'))
                                first = fleet(now)
                                first[host] = fleet(now, cpu=68.2)[host]
                                self.step(controller, now, actual=actual, nodes=first, manual_level=manual)
                                event_time += 3
                            nodes = fleet(event_time)
                            sample = nodes[host]
                            options = dict(manual_level=manual)
                            readback = feedback(event_time, actual)
                            if kind in ('cpu', 'gpu', 'board', 'power', 'rapid', 'projection'):
                                values = {'cpu': dict(cpu=73), 'gpu': dict(gpu=73), 'board': dict(board=75),
                                          'power': dict(power=80), 'rapid': dict(cpu=69.4) if kind == 'rapid' else {},
                                          'projection': dict(cpu=73)}[kind]
                                nodes[host] = fleet(event_time, **values)[host]
                            elif kind == 'learned':
                                sample['power_w'] = 55
                                authorized_spbm_node = nodes[0]
                                sample = authorized_spbm_node
                                sample['power_w'] = 55
                                options['power_thresholds'] = {HOSTS[0]: dict(source='spbm', threshold_w=50)}
                            elif kind == 'offline':
                                sample['online'] = False
                            elif kind == 'missing':
                                nodes.pop(host)
                            elif kind == 'stale':
                                sample['requested_mono'] = now - 7
                            elif kind == 'source':
                                sample['source_ts'] = now - 3
                            elif kind in ('throttle', 'ambiguous'):
                                sample['thermal_throttle' if kind == 'throttle' else 'ambiguous_sensors'] = True
                            elif kind == 'sensor':
                                sample['sensors'].pop()
                            elif kind == 'load_missing':
                                sample['cpu_load'] = None
                            else:
                                readback['fault'] = True
                            decision = self.step(controller, event_time, nodes=nodes, readback=readback, **options)
                            self.assertFalse(decision['learning_eligible'])
                            if kind in ('power', 'learned', 'rapid'):
                                self.assertGreaterEqual(decision['level'], 10)
                                if kind == 'rapid' and 69.4 + 12 >= 75 or manual == 16:
                                    self.assertEqual(decision['level'], 16)
                            else:
                                self.assertEqual(decision['level'], 16)
                                self.assertEqual(decision['scenario'], 'protect')

    def test_confirmed_trial_thermal_emergencies_record_failure_and_request_full_airflow(self):
        for kind in ('cpu', 'gpu', 'board', 'rapid', 'projection', 'throttle'):
            with self.subTest(kind=kind):
                controller = Controller(HOSTS)
                self.baseline(controller)
                self.step(controller, 63, actual=15)
                event_time = 66
                if kind == 'rapid':
                    self.step(controller, 66, actual=15, nodes=fleet(66, cpu=68.2))
                    event_time = 69
                values = {'cpu': dict(cpu=73), 'gpu': dict(gpu=73), 'board': dict(board=75),
                          'rapid': dict(cpu=69.4), 'projection': dict(cpu=73), 'throttle': {}}[kind]
                nodes = fleet(event_time, **values)
                if kind == 'throttle':
                    nodes[-1]['thermal_throttle'] = True
                decision = self.step(controller, event_time, actual=15, nodes=nodes)
                self.assertEqual(decision['level'], 16)
                self.assertEqual(decision['trial']['outcome'], 'failed')
                self.assertFalse(decision['retry_blocked'])

    def test_target_escalation_rolls_back_and_pending_is_not_proven_failure(self):
        for confirmed_before_headroom_loss in (False, True):
            with self.subTest(confirmed=confirmed_before_headroom_loss):
                controller = Controller(HOSTS)
                for now in range(0, 61, 3):
                    self.step(controller, now, nodes=fleet(now, cpu=58))
                if confirmed_before_headroom_loss:
                    self.step(controller, 63, actual=15, nodes=fleet(63, cpu=58))
                now = 66 if confirmed_before_headroom_loss else 63
                decision = self.step(controller, now, actual=15 if confirmed_before_headroom_loss else 16,
                                     nodes=fleet(now, cpu=58.01))
                self.assertEqual(decision['level'], 15 if confirmed_before_headroom_loss else 16)
                self.assertEqual(decision['trial']['outcome'], 'recovery_released' if confirmed_before_headroom_loss else 'invalidated')
                self.assertFalse(decision['retry_blocked'])

    def test_residual_heat_and_learning_policy_do_not_authorize_loaded_descent(self):
        controller, end = self.reach(14)
        for age in (3, 6, 9):
            decision = self.step(controller, end + age, actual=14, nodes=fleet(end + age, load=70))
        self.assertEqual(decision['level'], 14)
        for age in range(12, 135, 3):
            now = end + age
            decision = self.step(controller, now, nodes=fleet(now, cpu=53.1),
                                 power_thresholds={HOSTS[0]: dict(source='spbm', threshold_w=50)})
            self.assertGreaterEqual(decision['level'], 10)
            self.assertFalse(decision['learning_eligible'])

    def test_public_boundary_validates_manual_and_floor(self):
        for invalid in (True, -1, 17, 1.5, '0'):
            for key in ('manual_level', 'minimum_level'):
                with self.subTest(invalid=invalid, key=key), self.assertRaises(ValueError):
                    Controller(HOSTS).decide(fleet(0), 0, feedback=feedback(0), **{key: invalid})

    def test_bounded_drift_and_quantization_can_settle_without_range_veto(self):
        for shape in ('creep', 'growing_peaks'):
            with self.subTest(shape=shape):
                controller, requested = self.probe()
                for age in range(3, 124, 3):
                    now = requested + age
                    if shape == 'creep':
                        cpu = 51 + (age - 3) * .012
                    else:
                        cpu = 51 + 1.1 + age * .003 if age % 30 == 15 else 51
                    decision = self.step(controller, now, actual=9, nodes=fleet(now, cpu=cpu))
                    self.assertEqual(decision['level'], 9)
                    self.assertEqual(decision['trial']['outcome'], 'success' if age == 123 else None)

    def test_restart_and_fault_require_actual_sixteen_and_fresh_baseline(self):
        for external in (False, True):
            with self.subTest(external=external):
                controller = Controller(HOSTS)
                if external:
                    self.baseline(controller)
                    controller.protect('transient fault cleared between monitor frames')
                start = 63 if external else 0
                for now in range(start, start + 123, 3):
                    decision = self.step(controller, now, actual=14)
                    self.assertEqual(decision['level'], 16)
                    self.assertFalse(decision['learning_eligible'])
                for now in range(start + 123, start + 153, 3):
                    self.assertEqual(self.step(controller, now)['level'], 16)
                self.assertEqual(self.step(controller, start + 153)['level'], 15)

    def test_failed_trial_is_recorded_and_every_fault_requires_new_safe_release(self):
        controller = Controller(HOSTS)
        self.baseline(controller)
        self.step(controller, 63, actual=15)
        decision = self.step(controller, 66, actual=15, nodes=fleet(66, cpu=73))
        self.assertEqual(decision['trial']['outcome'], 'failed')
        for kind in ('fan', 'telemetry', 'power', 'throttle', 'manual'):
            branch = deepcopy(controller)
            nodes = fleet(69)
            readback = feedback(69)
            options = {}
            if kind == 'fan':
                readback['fault'] = True
            elif kind == 'telemetry':
                nodes.pop()
            elif kind == 'power':
                nodes[-1]['power_w'] = 80
            elif kind == 'throttle':
                nodes[-1]['thermal_throttle'] = True
            else:
                options['manual_level'] = 16
            decision = self.step(branch, 69, nodes=nodes, readback=readback, **options)
            self.assertEqual(decision['level'], 16)
            self.assertFalse(decision['learning_eligible'])
            branch.protect('another fault')
            for now in range(72, 900, 3):
                decision = self.step(branch, now, nodes=fleet(now, power=10))
                self.assertFalse(decision['learning_eligible'])
                if decision['level'] == 15:
                    break
                self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['level'], 15)
            self.assertEqual(decision['trial']['kind'], 'release')
            self.assertEqual(decision['trial']['status'], 'pending')
            self.assertGreaterEqual(now - 72, 30)

    def test_stable_learning_eligibility_requires_a_confirmed_floor(self):
        controller = Controller(HOSTS)
        for now in range(0, 151, 3):
            decision = self.step(controller, now, manual_level=16)
            self.assertFalse(decision['learning_eligible'])
        duplicate = self.step(controller, 153, manual_level=16, readback=feedback(150))
        self.assertFalse(duplicate['learning_eligible'])
        resumed = self.step(controller, 156, manual_level=16)
        self.assertFalse(resumed['learning_eligible'])

    def test_ten_percent_does_not_invalidate_background_trial_or_authorize_learning(self):
        controller, requested = self.probe()
        self.step(controller, requested + 3, actual=9, nodes=fleet(requested + 3, load=5))
        decision = self.step(controller, requested + 6, actual=9, nodes=fleet(requested + 6, load=10))
        self.assertEqual(decision['level'], 9)
        self.assertIsNone(decision['trial']['outcome'])
        self.assertFalse(decision['learning_eligible'])

    def test_duplicate_feedback_keeps_real_increasing_temperature_peaks(self):
        from test_core import ControlTests
        fixture = ControlTests()
        for gap in ('duplicate', 'wifi'):
            with self.subTest(gap=gap):
                controller, actual = Controller(HOSTS), 16
                for now in range(0, 1000, 3):
                    decision = self.step(controller, now, actual=actual, nodes=fixture.sensor_fleet(now))
                    actual = decision['level']
                    if actual == 9:
                        break
                requested = now
                for age in range(3, 124, 3):
                    now = requested + age
                    stamp = now - (3 if gap == 'duplicate' and age % 6 == 0 else 0)
                    peak = {18: 1.1, 48: 1.2, 78: 1.3, 108: 1.4}.get(age, 0)
                    readings = () if gap == 'wifi' and peak else (('wifi', 50),)
                    nodes = fixture.sensor_fleet(now, readings, cpu=51 + peak)
                    decision = self.step(controller, now, nodes=nodes, readback=feedback(stamp, 9))
                self.assertEqual(decision['level'], 9)
                self.assertIsNone(decision['trial']['outcome'])
                points = controller.history[HOSTS[0]]['acpi:0:acpitz']
                self.assertEqual(max(value for _, value in points), 52.4)
                for age in range(126, 247, 3):
                    now = requested + age
                    decision = self.step(controller, now, nodes=fixture.sensor_fleet(now), actual=9)
                    if decision['trial']['outcome'] == 'success':
                        break
                self.assertEqual(decision['trial']['outcome'], 'success')
                self.assertGreaterEqual(decision['trial']['observation_elapsed'], 120)
                self.assertFalse(decision['learning_eligible'])
                now += 3
                decision = self.step(controller, now, nodes=fixture.sensor_fleet(now, cpu=73),
                                     readback=feedback(now - 3, 9))
                self.assertEqual(decision['level'], 16)
                self.assertEqual(decision['trial']['outcome'], 'failed')

    def test_duplicate_fan_snapshot_cannot_hide_broken_admission_margin(self):
        controller = Controller(HOSTS)
        self.baseline(controller, 54)
        decision = self.step(controller, 57, nodes=fleet(57, cpu=53.1), readback=feedback(54))
        self.assertEqual(decision['level'], 16)
        self.assertEqual(self.step(controller, 60)['level'], 16)
        self.assertIsNone(controller.trial)

    def test_confounded_rebound_immediately_restores_warming_floor(self):
        for change in ('source', 'actual'):
            with self.subTest(change=change):
                controller, end = self.reach(9)
                for now in range(end + 3, end + 61, 3):
                    self.step(controller, now, actual=9)
                confirmed = end + 63
                for age in range(0, 34, 3):
                    nodes = fleet(confirmed + age, cpu=51 + age * .06)
                    if age == 33 and change == 'source':
                        nodes[-1]['power_source'] = 'changed'
                    actual = 9 if age == 33 and change == 'actual' else 8
                    decision = self.step(controller, confirmed + age, actual=actual, nodes=nodes)
                self.assertEqual(decision['scenario'], 'confounded')
                self.assertEqual(decision['level'], 10)
                self.assertEqual(decision['trial']['outcome'], 'invalidated')


if __name__ == '__main__':
    unittest.main()
