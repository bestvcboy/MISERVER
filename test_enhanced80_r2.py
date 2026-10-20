import unittest
import tempfile
import json
from copy import deepcopy

from core import (Controller, Journal, normalize, FailedDescentRestoration, HighWindAuthority, Measured80,
    MeasuredCritical, NoAscent, SafeEndpointRestoration, SourceFaultProtection, TenLevelNonRecovery)
from test_core import HOSTS
from test_scenarios import feedback, fleet
from thermal_policy import HIGH_EXIT_TAG, ThermalMemory, source_key


class Ceiling80Tests(unittest.TestCase):
    def controller(self):
        return Controller(HOSTS, rules_only=True, high_wind_gate=True)

    def test_high_failed_edge_requalifies_only_after_existing_recovery_observation(self):
        for name, base_load, base_power, load, power, manual, earliest in (
                ('lower_load', 60, 100, 10, 56, None, 69),
                ('lower_power', 5, 45, 5, 30, None, 69),
                ('manual_lower_power', 5, 45, 5, 30, 16, 69),
                ('manual_lower_load35', 35, 60, 5, 40, 16, 69),
                ('manual_lower_load60', 60, 100, 10, 56, 16, 69)):
            with self.subTest(name=name):
                c = self.controller()
                for now, actual, cpu in ((0, 16, 40), (3, 15, 80), (6, 16, 40)):
                    c.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=base_load, power=base_power),
                        now, minimum_level=0, manual_level=manual, feedback=feedback(now, actual))
                failures = deepcopy(c.export_thermal_memory()['failures'])
                self.assertEqual(len(failures), 4)
                resumed = None
                for now in range(9, 1204, 3):
                    d = c.decide(fleet(now, cpu=40, gpu=38, board=46, load=load, power=power),
                        now, minimum_level=0, manual_level=manual, feedback=feedback(now, 16))
                    self.assertEqual(c.operating.observe_credit, 0)
                    self.assertFalse(c.operating.evidence_qualified)
                    self.assertIsNone(c._thermal_memory.open_edge)
                    if now <= 33:
                        self.assertEqual(c.export_thermal_memory()['failures'], failures)
                        self.assertEqual(d['level'], 16)
                    if d['level'] == 15:
                        resumed = now
                        self.assertEqual(c.export_thermal_memory()['failures'], failures)
                        self.assertEqual(d['execution_trial']['purpose'], 'ceiling_exit')
                        break
                self.assertIsNotNone(resumed, 'A real failed edge must not remain permanently locked after qualified recovery')
                self.assertGreaterEqual(resumed, earliest)

    def test_rejected_source_retires_only_affected_descent_attribution(self):
        for confound in ('duplicate', 'unverified'):
            for unrelated_hot in (False, True):
                with self.subTest(confound=confound, unrelated_hot=unrelated_hot):
                    c = self.controller()
                    for now in (0, 3, 6, 9, 12, 15):
                        nodes = []
                        for index, host in enumerate(HOSTS):
                            raw = dict(hostname='DGX', timestamp=now, cpu_percent=5, gpu_percent=5,
                                system_power_w=45, system_power_source='spbm', temperatures=dict(gpu=38,
                                    acpi=[dict(zone='0', type='acpitz', temp=80 if now >= 6
                                        and (index == 0 or unrelated_hot) else 40)],
                                    mlx=[dict(label='asic', temp=46)]))
                            if index == 0 and now == 6 and confound == 'duplicate':
                                raw['temperatures']['acpi'].append(deepcopy(raw['temperatures']['acpi'][0]))
                            sample = normalize(host, raw, now, now)
                            if index == 0 and now == 6 and confound == 'unverified':
                                sample['hostname_verified'] = False
                            nodes.append(sample)
                        d = c.decide(nodes, now, minimum_level=0,
                            feedback=feedback(now, 16 if now == 0 else 15))
                        if now == 6:
                            self.assertFalse(c.thermal_view.complete)
                            self.assertIsNotNone(d)
                    failures = c.export_thermal_memory()['failures']
                    self.assertFalse(any(row['source'][0] == HOSTS[0] for row in failures))
                    if unrelated_hot:
                        self.assertTrue(failures, 'Rejected source must not erase other hosts\' real failures')
                    else:
                        self.assertFalse(failures)

    def test_high_failure_recovery_credit_restarts_after_untrusted_observation(self):
        for interruption in ('source', 'endpoint', 'gap', 'load', 'hot'):
            with self.subTest(interruption=interruption):
                c = self.controller()
                for now, actual, cpu in ((0, 16, 40), (3, 15, 80), (6, 16, 40)):
                    c.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=60, power=100),
                        now, minimum_level=0, feedback=feedback(now, actual))
                failures = deepcopy(c.export_thermal_memory()['failures'])
                for now in range(9, 61, 3):
                    c.decide(fleet(now, cpu=40, gpu=38, board=46, load=10, power=56),
                        now, minimum_level=0, feedback=feedback(now, 16))
                self.assertGreater(c.operating.failure_credit, 0)
                nodes = fleet(63, cpu=40, gpu=38, board=46, load=10, power=56)
                observed = feedback(63, 16)
                if interruption == 'source':
                    nodes[0]['hostname_verified'] = False
                elif interruption == 'endpoint':
                    observed.update(confirmed=False, execution='unknown', rpm=None)
                elif interruption == 'load':
                    for node in nodes:
                        node.update(cpu_load=60, gpu_load=60, power_w=100)
                elif interruption == 'hot':
                    nodes = fleet(63, cpu=80, gpu=38, board=46, load=10, power=56)
                now = 69 if interruption == 'gap' else 63
                if interruption == 'gap':
                    nodes = fleet(now, cpu=40, gpu=38, board=46, load=10, power=56)
                    observed = feedback(now, 16)
                c.decide(nodes, now, minimum_level=0, feedback=observed)
                self.assertEqual(c.operating.failure_credit, 0)
                self.assertEqual(c.export_thermal_memory()['failures'], failures)
                for stamp in range(now + 3, now + 60, 3):
                    c.decide(fleet(stamp, cpu=40, gpu=38, board=46, load=10, power=56),
                        stamp, minimum_level=0, feedback=feedback(stamp, 16))
                    self.assertEqual(c.export_thermal_memory()['failures'], failures)
                for stamp in range(now + 60, now + 303, 3):
                    d = c.decide(fleet(stamp, cpu=40, gpu=38, board=46, load=10, power=56),
                        stamp, minimum_level=0, feedback=feedback(stamp, 16))
                    if d['level'] == 15:
                        break
                self.assertEqual(d['level'], 15)
                self.assertEqual(c.export_thermal_memory()['failures'], failures)

    def test_interrupted_attribution_cannot_revive_when_old_state_returns(self):
        for interruption in ('load', 'identity', 'endpoint', 'missing'):
            with self.subTest(interruption=interruption):
                c = self.controller()
                for now in (0, 3, 6, 9, 12):
                    nodes = fleet(now, cpu=58 if now >= 6 else 40, load=5, power=45)
                    actual = 16 if now == 0 else 15
                    if now == 6:
                        if interruption == 'load':
                            for node in nodes:
                                node.update(cpu_load=90, gpu_load=90, power_w=140)
                        elif interruption == 'identity':
                            for node in nodes:
                                node['hostname'] = 'replacement'
                        elif interruption == 'endpoint':
                            actual = 14
                        else:
                            nodes = []
                    c.decide(nodes, now, minimum_level=0, feedback=feedback(now, actual))
                self.assertFalse(c._thermal_memory.failures)
                self.assertNotIsInstance(c.high_wind.proof, FailedDescentRestoration)

    def test_hot_confirmation_records_failure_and_blocks_repeating_edge_after_cooling(self):
        c = self.controller()
        c.decide(fleet(0, cpu=40, load=5, power=45), 0, minimum_level=0, feedback=feedback(0, 16))
        d = c.decide(fleet(3, cpu=80, load=5, power=45), 3, minimum_level=0, feedback=feedback(3, 15))
        failures = c.export_thermal_memory()['failures']
        self.assertTrue(failures)
        self.assertTrue(all(row['failure_class'] == 'thermal_failure' for row in failures))
        self.assertEqual(d['level'], 16)
        for now in (6, 9, 12, 33):
            d = c.decide(fleet(now, cpu=40, load=5, power=45), now, minimum_level=0, feedback=feedback(now, 16))
        self.assertEqual(d['level'], 16)
        self.assertEqual(d['control_status']['primary_reason'], 'ceiling_exit_requalifying')
        self.assertEqual(c.export_thermal_memory()['failures'], failures)

    def test_measured80_selects_only_adjacent_enhancement_at_confirmed_endpoint(self):
        for actual, target in ((8, 10), (10, 11), (11, 12), (15, 16), (16, 16)):
            with self.subTest(actual=actual):
                c = self.controller()
                d = c.decide(fleet(100, cpu=80, gpu=38, board=46, load=5, power=45), 100,
                    minimum_level=0, feedback=feedback(100, actual))
                self.assertEqual((d['level'], d['demand']), (target, target))
                d = c.decide(fleet(103, cpu=79.999, gpu=38, board=46, load=5, power=45), 103,
                    minimum_level=0, feedback=feedback(103, target))
                self.assertEqual(d['normal_ceiling'], 10)
                self.assertLessEqual(d['demand'], 10)
                if target > 10:
                    self.assertEqual(d['level'], target - 1)

    def test_stalled_source_fault_occurrence_does_not_renew_from_request_clock(self):
        c = self.controller()
        stamps = []
        for now in (100, 103, 106, 109, 112):
            nodes = fleet(now, cpu=50)
            nodes[0]['source_ts'] = 100
            c.decide(nodes, now, minimum_level=0, feedback=feedback(now, 10))
            if now > 100:
                stamps.append(c.high_wind.observed_mono)
                self.assertEqual(c.high_wind.valid_at(now), now < 109)
        self.assertEqual(stamps, [103] * 4)
        nodes = fleet(115, cpu=50)
        nodes[0]['source_ts'] = 99
        c.decide(nodes, 115, minimum_level=0, feedback=feedback(115, 10))
        self.assertFalse(c.high_wind.valid_at(115))
        c.decide(fleet(118, cpu=50), 118, minimum_level=0, feedback=feedback(118, 10))
        self.assertTrue(c.thermal_view.complete)
        nodes = fleet(121, cpu=50)
        nodes[0]['source_ts'] = 118
        c.decide(nodes, 121, minimum_level=0, feedback=feedback(121, 10))
        self.assertTrue(c.high_wind.valid_at(121))

    def test_real_history_retains_exception_source_and_validated_policy_only(self):
        c = self.controller()
        nodes = fleet(100)
        nodes[0]['hostname_verified'] = False
        d = c.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
        self.assertEqual(d['demand'], 12)
        self.assertEqual(d['normal_ceiling'], 10)
        self.assertIsNone(d['transition_target'])
        self.assertEqual(d['control_status']['phase'], 'protecting')
        self.assertEqual(d['high_wind']['exception'], 'SourceFaultProtection')
        self.assertEqual(len(d['high_wind']['source']), 5)
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.append(dict(ts=100, decision=d, nodes=[]))
            old = deepcopy(d)
            old['policy_spec'] = dict(version='enhanced80-r2', enhanced_admission_c='80')
            journal.append(dict(ts=103, decision=old, nodes=[]))
            rows = sorted(journal.history(100, 104)['samples'], key=lambda row: row['ts'])
        public = rows[0]['decision']
        self.assertEqual(public['policy_spec'], dict(version=d['policy_spec']['version'], enhanced_admission_c=80))
        self.assertEqual(public['high_wind']['exception'], 'SourceFaultProtection')
        self.assertEqual(public['high_wind']['source'], list(d['high_wind']['source']))
        self.assertEqual(len(public['control_status']['blocking_source']), 5)
        self.assertNotIn('policy_spec', rows[1]['decision'])

    def test_real_failure_history_and_ui_use_the_recorded_eighty_policy(self):
        from test_policy_ui import PolicyUITests
        c = self.controller()
        c.decide(fleet(0, cpu=40, load=5, power=45), 0, minimum_level=0, feedback=feedback(0, 16))
        d = c.decide(fleet(3, cpu=80, load=5, power=45), 3, minimum_level=0, feedback=feedback(3, 15))
        self.assertEqual(d['high_wind']['exception'], 'FailedDescentRestoration')
        self.assertEqual(d['control_status']['release_basis'], 'protection')
        self.assertEqual((d['demand'], d['normal_ceiling']), (16, 10))
        self.assertIsNone(d['transition_target'])
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.append(dict(ts=3, decision=d, nodes=[]))
            public = journal.history(3, 4)['samples'][0]['decision']
        self.assertEqual(public['high_wind']['source'], list(d['high_wind']['source']))
        self.assertEqual(public['high_wind']['exception'], 'FailedDescentRestoration')
        PolicyUITests().run_node('status.decision = ' + json.dumps(public) + r''';
status.high_wind_gate = true;
status.high_wind_limit = 10;
render();
assert.match(get('policy-detail').textContent, /普通增强门槛为可信实温80°C/);
assert.match(history({decision:status.decision}), /普通增强门槛为可信实温80°C/);
assert.ok(!get('policy-detail').textContent.includes('危急75°C均为暂定工程值'));
''')

    def test_loaded_varying_manual_and_automatic_six_adjacent_steps(self):
        for temperature in (65, 79.999):
            for manual in (None, 16):
                with self.subTest(temperature=temperature, manual=manual):
                    c, actual, pending, commands = self.controller(), 16, None, []
                    for now in range(0, 210, 3):
                        if pending is not None:
                            actual, pending = pending, None
                        d = c.decide(fleet(now, cpu=temperature, gpu=52, board=64,
                            load=60 if now % 2 else 90, power=100 if now % 2 else 140), now,
                            minimum_level=16 if manual else 0, manual_level=manual,
                            feedback=feedback(now, actual))
                        self.assertEqual(d['high_wind_limit'], 10)
                        self.assertLessEqual(d['demand'], 10)
                        op = d.get('execution_trial') or {}
                        if op.get('status') == 'pending' and op.get('purpose') == 'ceiling_exit':
                            if not commands or commands[-1] != op['to']:
                                commands.append(op['to'])
                                pending = op['to']
                                self.assertEqual(op['to'], actual - 1)
                        if actual == 10:
                            break
                    self.assertEqual(commands, [15, 14, 13, 12, 11, 10])
                    self.assertEqual(c.operating.observe_credit, 0)
                    self.assertIsNone(c._thermal_memory.open_edge)

    def test_all_groups_are_hot_witnesses_and_unrelated_gap_survives(self):
        for group in ('acpi', 'gpu', 'mlx'):
            c, nodes = self.controller(), fleet(100)
            for sensor in nodes[0]['sensors']:
                if sensor['group'] == group:
                    sensor['temp'] = 80
            nodes.pop()
            d = c.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
            self.assertEqual(d['high_wind_limit'], 16, group)

    def test_memory_adjacent_purpose_strict_and_representation_preserved(self):
        payload = ThermalMemory().export()
        payload['intent'] = dict(from_level=16, to_level=15, purpose='ceiling_exit', state='pending')
        self.assertEqual(ThermalMemory.restore(payload).export(), payload)
        self.assertEqual(payload['policy_version'], 'closed-loop-r1')
        for changes in (dict(to_level=10), dict(from_level=10, to_level=9),
                dict(purpose='ceiling_exit_other'), dict(extra=True)):
            bad = deepcopy(payload)
            bad['intent'].update(changes)
            with self.assertRaises(ValueError):
                ThermalMemory.restore(bad)

    def test_all_protected_groups_and_nonwitnesses(self):
        for group in ('cpu', 'acpi', 'gpu', 'nvme', 'nic', 'mlx', 'wifi', 'other', 'spbm', 'ambient'):
            nodes, c = fleet(100), self.controller()
            nodes[0]['sensors'].append(dict(id=group + ':test', group=group, temp=80,
                calibrated=group != 'spbm'))
            d = c.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
            self.assertEqual(d['high_wind_limit'], 10 if group in ('spbm', 'ambient') else 16, group)

    def test_complete_inventory_and_fault_recovery_strict_ttl(self):
        c = self.controller()
        nodes = fleet(100)
        nodes[0]['sensors'].pop()
        d = c.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 16))
        self.assertIsInstance(c.high_wind.proof, SourceFaultProtection)
        self.assertFalse(c.thermal_view.complete)
        self.assertIsNone(c.high_wind.actual_c)
        self.assertTrue(c.high_wind.valid_at(105.999))
        self.assertFalse(c.high_wind.valid_at(106))
        old = deepcopy(nodes)
        c.decide(old, 106, minimum_level=0, feedback=feedback(106, 16))
        self.assertFalse(c.high_wind.valid_at(106))
        c.decide(fleet(109), 109, minimum_level=0, feedback=feedback(109, 16))
        self.assertTrue(c.thermal_view.complete)
        self.assertNotIsInstance(c.high_wind.proof, SourceFaultProtection)

    def test_old_admission_and_restoration_labels_cannot_bypass_eighty(self):
        witness = (HOSTS[0], 'DGX', 'acpi:0:acpitz')
        for proof in (MeasuredCritical(), TenLevelNonRecovery(60, 180, 600, 10, 11),
                SafeEndpointRestoration(15, 16, 160)):
            a = HighWindAuthority(True, witness, 100, 79.999, proof)
            self.assertEqual(a.normal_ceiling_at(100), 10)
            self.assertFalse(a.permits(100, 10 if isinstance(proof, TenLevelNonRecovery) else 15, 16))

    def test_original_deadline_timeout_wins_and_late_feedback_is_physical_only(self):
        for at, outcome in ((59.999, 'confirmed'), (60, 'execution_timeout')):
            c = self.controller()
            c.decide(fleet(0), 0, minimum_level=0, feedback=feedback(0, 16))
            for now in range(3, 58, 3):
                d = c.decide(fleet(now), now, minimum_level=16, manual_level=16,
                    feedback=feedback(now, 16, confirmed=False, execution='pending'))
                self.assertEqual(d['execution_trial']['original_deadline'], 60)
            d = c.decide(fleet(at), at, minimum_level=0, feedback=feedback(at, 15))
            self.assertEqual(d['execution_trial']['status'], outcome)
            self.assertEqual(d['execution_trial']['original_deadline'], 60)
            if at == 60:
                self.assertNotEqual(c.operating.confirmed_level, 15)
                d = c.decide(fleet(63), 63, minimum_level=0, feedback=feedback(63, 15))
                self.assertEqual(d['execution_trial']['status'], 'execution_timeout')
                self.assertEqual(c.operating.confirmed_level, 15)

    def test_duplicate_zero_rpm_unknown_and_nonfavorite_do_not_confirm(self):
        for change in (dict(observed_mono=0), dict(rpm=0), dict(mode='auto'),
                dict(power='off'), dict(level=None)):
            c = self.controller()
            c.decide(fleet(0), 0, minimum_level=0, feedback=feedback(0, 16))
            d = c.decide(fleet(3), 3, minimum_level=0, feedback=feedback(3, 15, **change))
            self.assertNotEqual(d['execution_trial']['status'], 'confirmed')

    def legacy_payload(self, stage='confirmed'):
        nodes = fleet(0, cpu=50)
        key = source_key(nodes[0], (HOSTS[0], nodes[0]['sensors'][0]['id'], 'acpi'))
        m = ThermalMemory()
        m.add_anchor(key, 50, 53)
        m.open_edge = dict(from_level=16, to_level=15, automatic=True, release_basis=HIGH_EXIT_TAG,
            stage=stage, sources=[dict(source=list(key), baseline=[0, 0, 30], temp_c=50,
                attributable=True, exit_origin_c=50)])
        m.intent = dict(from_level=16, to_level=15, purpose='release', state='confirmed')
        return m.export(), key

    def test_restart_tagged_stages_coexist_with_new_intent_without_forgiving_memory(self):
        for stage in ('pending', 'confirmed', 'unresolved'):
            for actual in (16, 15):
                payload, key = self.legacy_payload(stage)
                c = self.controller()
                c.restore_thermal_memory(payload, 100)
                before = c.export_thermal_memory()
                d = c.decide(fleet(100, cpu=65, load=90, power=140), 100,
                    minimum_level=0, feedback=feedback(100, actual))
                after = c.export_thermal_memory()
                self.assertEqual(d['execution_trial']['to'], actual - 1)
                self.assertEqual(after['open_edge'], before['open_edge'])
                self.assertEqual(after['anchors'], before['anchors'])
                self.assertEqual(after['failures'], before['failures'])
                self.assertEqual(ThermalMemory.restore(after).export(), after)
                self.assertEqual(after['intent']['purpose'], 'ceiling_exit')
                self.assertEqual(d['execution_trial']['original_deadline'], 160)
        bad = deepcopy(after)
        bad['open_edge']['sources'][0]['exit_origin_c'] = True
        with self.assertRaises(ValueError):
            ThermalMemory.restore(bad)

    def test_actual_failure_requires_current_attribution_and_exact_endpoint(self):
        c = self.controller()
        for now in range(0, 34, 3):
            c.decide(fleet(now, cpu=50), now, minimum_level=0, feedback=feedback(now, 15))
        payload, key = self.legacy_payload()
        c._thermal_memory = ThermalMemory.restore(payload)
        nodes = fleet(33, cpu=50)
        identity = c.operating.identity
        c._policy['last_downward'] = dict(edge=(16, 15), identity=identity,
            reference={(key[0], key[3]): 50}, baselines={key[0]: [0, 0, 30]}, automatic=True)
        c._policy['operation'] = None
        d = c.decide(fleet(36, cpu=59), 36, minimum_level=0, feedback=feedback(36, 15))
        self.assertIsInstance(c.high_wind.proof, FailedDescentRestoration)
        self.assertEqual((d['level'], d['high_wind_limit']), (16, 10))
        self.assertTrue(c.high_wind.permits(36, 15, 16))
        self.assertFalse(c.high_wind.permits(36, 14, 16))
        self.assertEqual(len(c._thermal_memory.failures), 1)

    def test_occupied_failure_slot_is_explicit_memory_conflict(self):
        payload, key = self.legacy_payload()
        m = ThermalMemory.restore(payload)
        unresolved = dict(safe_level=16, failed_level=15, baseline=[0, 0, 30],
            automatic=True, failure_class='unresolved_descent')
        m.add_failure(key, unresolved)
        m.add_failure(key, dict(unresolved, failure_class='thermal_failure'))
        self.assertTrue(m.saturated)
        self.assertEqual(m.failures[key + (15,)]['failure_class'], 'unresolved_descent')
        self.assertEqual(ThermalMemory.restore(m.export()).export(), m.export())

    def test_new_ceiling_step_retains_actual_failure_protection_without_learning_edge(self):
        c = self.controller()
        d = c.decide(fleet(0, cpu=40, load=5, power=45), 0, minimum_level=0,
            feedback=feedback(0, 16))
        self.assertEqual(d['level'], 15)
        c.decide(fleet(3, cpu=40, load=5, power=45), 3, minimum_level=0,
            feedback=feedback(3, 15))
        d = c.decide(fleet(6, cpu=80, load=5, power=45), 6, minimum_level=0,
            feedback=feedback(6, 15))
        self.assertEqual(d['level'], 16)
        self.assertIsInstance(c.high_wind.proof, FailedDescentRestoration)
        self.assertEqual(d['high_wind']['ceiling'], 10)
        self.assertIsNone(c._thermal_memory.open_edge)
        self.assertTrue(c._thermal_memory.failures)


if __name__ == '__main__':
    unittest.main()
