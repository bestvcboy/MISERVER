import math
import asyncio
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from core import Controller, Journal
from policy_memory import ThermalMemoryGate, ThermalMemoryStore, memory_digest
from service import atomic_write
from test_core import HOSTS
import test_enhanced_exit_r3 as exit_tests
from test_scenarios import feedback, fleet
from thermal_policy import MAX_FAILURES, ThermalMemory, policy_spec, source_key


def danger(safe=16, controller=None, load=60, power=100):
    controller = controller or Controller(HOSTS, rules_only=True, high_wind_gate=True)
    for now, actual, cpu in ((0, safe, 40), (3, safe - 1, 80), (6, safe, 40)):
        controller.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=load, power=power),
            now, minimum_level=0, feedback=feedback(now, actual))
    return controller


def step(controller, now, actual=16, **values):
    options = dict(cpu=40, gpu=38, board=46, load=5, power=45)
    options.update(values)
    return controller.decide(fleet(now, **options), now, minimum_level=0,
        feedback=feedback(now, actual))


class CandidateMemoryTests(unittest.TestCase):
    def test_new_danger_roundtrip_and_legacy_representation(self):
        legacy = exit_tests.EnhancedExitR3Tests().restored().export_thermal_memory()
        self.assertNotIn('ceiling_dangers', legacy)
        self.assertEqual(ThermalMemory.restore(legacy).export(), legacy)
        payload = danger().export_thermal_memory()
        self.assertTrue(payload.get('ceiling_dangers'), 'new observed danger provenance missing')
        self.assertEqual(ThermalMemory.restore(payload).export(), payload)
        self.assertEqual(payload['policy_version'], 'closed-loop-r1')
        self.assertEqual(policy_spec()['version'], 'enhanced80-r4')

    def test_metadata_validation(self):
        payload = danger().export_thermal_memory()
        self.assertTrue(payload.get('ceiling_dangers'), 'new observed danger provenance missing')
        row = payload['ceiling_dangers'][0]
        for change in (dict(safe_level=10), dict(failed_level=13), dict(safe_level=True),
                dict(source=['bad']), dict(baseline=[60, 60, math.inf]),
                dict(baseline=[60, -1, 100]), dict(baseline=[60, 60, 1001]),
                dict(baseline=[60, True, 100]), dict(extra=1)):
            with self.subTest(change=change):
                bad = deepcopy(payload)
                bad['ceiling_dangers'] = [dict(row, **change)]
                with self.assertRaises(ValueError):
                    ThermalMemory.restore(bad)
        for rows in (None, {}, [row, row], [row] * (MAX_FAILURES + 1)):
            bad = deepcopy(payload)
            bad['ceiling_dangers'] = rows
            with self.assertRaises(ValueError):
                ThermalMemory.restore(bad)
        for mode in ('dangling', 'unresolved'):
            bad = deepcopy(payload)
            if mode == 'dangling':
                bad['failures'] = []
            else:
                for failure in bad['failures']:
                    failure['failure_class'] = 'unresolved_descent'
            with self.assertRaises(ValueError):
                ThermalMemory.restore(bad)

    def test_occupied_slot_preserves_history_and_records_fresh_event(self):
        controller = exit_tests.EnhancedExitR3Tests().restored()
        before = deepcopy(controller.export_thermal_memory()['failures'])
        for now in range(0, 61, 3):
            step(controller, now, load=60, power=100)
        step(controller, 63, actual=15, cpu=80, load=60, power=100)
        payload = controller.export_thermal_memory()
        old = before[0]
        self.assertIn(old, payload['failures'])
        rows = [r for r in payload.get('ceiling_dangers', [])
            if r['source'] == old['source'] and r['failed_level'] == old['failed_level']]
        self.assertEqual(len(rows), 1, 'occupied slot lost new event provenance')
        self.assertEqual(rows[0]['baseline'], [60, 60, 100])
        self.assertNotEqual(rows[0]['baseline'], old['baseline'])
        memory = ThermalMemory.restore(payload)
        key = tuple(old['source'])
        memory.remember_ceiling_danger(key, safe_level=16, failed_level=15, baseline=[5, 5, 45])
        updated = memory.export()
        self.assertIn(old, updated['failures'])
        self.assertIn(dict(source=old['source'], safe_level=16, failed_level=15,
            baseline=[5, 5, 45]), updated['ceiling_dangers'])
        self.assertEqual(ThermalMemory.restore(updated).export(), updated)

    def test_metadata_edge_is_independent_of_preserved_historical_safe_level(self):
        memory = ThermalMemory()
        key = source_key(fleet(0)[0], (HOSTS[0], 'sensor', 'acpi'))
        memory.add_failure(key, dict(safe_level=16, failed_level=14, baseline=[5, 5, 45],
            automatic=True, failure_class='thermal_failure'))
        before = memory.export()['failures']
        memory.remember_ceiling_danger(key, safe_level=15, failed_level=14, baseline=[60, 60, 100])
        payload = memory.export()
        self.assertEqual(payload['failures'], before)
        self.assertEqual(payload['ceiling_dangers'][0]['safe_level'], 15)
        self.assertEqual(ThermalMemory.restore(payload).export(), payload)

    def test_slot_conflict_and_capacity_latch_without_dangling_metadata(self):
        key = source_key(fleet(0)[0], (HOSTS[0], 'sensor', 'acpi'))
        for mode in ('conflict', 'capacity'):
            memory = ThermalMemory()
            count = MAX_FAILURES if mode == 'capacity' else 1
            for index in range(count):
                memory.add_failure(key[:3] + (str(index), key[4]), dict(safe_level=16,
                    failed_level=15, baseline=[5, 5, 45], automatic=True,
                    failure_class='unresolved_descent'))
            target = key[:3] + ('0', key[4]) if mode == 'conflict' else key
            memory.add_failure(target, dict(safe_level=16, failed_level=15,
                baseline=[60, 60, 100], automatic=True, failure_class='thermal_failure'))
            memory.remember_ceiling_danger(target, safe_level=16, failed_level=15,
                baseline=[60, 60, 100])
            self.assertTrue(memory.saturated)
            self.assertNotIn('ceiling_dangers', memory.export())
            ThermalMemory.restore(memory.export())


class CandidateRecoveryTests(unittest.TestCase):
    def test_restart_restores_restrictions_without_credit_at_high_and_ordinary_endpoint(self):
        for safe in (16, 11):
            for recovered in (False, True):
                with self.subTest(safe=safe, recovered=recovered):
                    payload = danger(safe).export_thermal_memory()
                    restored = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                    restored.restore_thermal_memory(payload, 9)
                    actual = safe if safe == 16 else 10
                    first_drop = None
                    for now in range(9, 610, 3):
                        decision = step(restored, now, actual,
                            load=5 if recovered else 60, power=45 if recovered else 100)
                        self.assertEqual(restored.export_thermal_memory()['failures'], payload['failures'])
                        if decision['next_command'] < actual:
                            first_drop = now
                            break
                    if recovered:
                        self.assertIsNotNone(first_drop)
                        self.assertGreaterEqual(first_drop, 69)
                    else:
                        self.assertIsNone(first_drop)
                        self.assertEqual(decision['operating']['failure_credit'], 0)

    def test_material_recovery_is_per_host_and_both_load_components(self):
        for mode in ('power', 'gpu', 'other_host'):
            controller = danger()
            for now in range(9, 160, 3):
                nodes = fleet(now, cpu=40, gpu=38, board=46, load=5, power=45)
                for node in nodes:
                    if mode == 'power':
                        node['power_w'] = 100
                    elif mode == 'gpu':
                        node['gpu_load'] = 61
                    elif node['host'] == HOSTS[0]:
                        node.update(cpu_load=60, gpu_load=60, power_w=100)
                decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 16))
                self.assertEqual(decision['next_command'], 16)
                self.assertEqual(decision['operating']['failure_credit'], 0)
            self.assertEqual(decision['control_status']['blocking_reason'],
                'ceiling_requalification_material_recovery')
            if mode == 'other_host':
                self.assertEqual(decision['control_status']['blocking_source'][0], HOSTS[0])

    def test_independent_idle_jitter_and_known_cycle(self):
        controller = danger()
        for now in range(9, 244, 3):
            nodes = fleet(now, cpu=50 + (0, .2, 0, -.2)[now // 3 % 4], gpu=45, board=50)
            for index, node in enumerate(nodes):
                node.update(cpu_load=(5, 15)[(index + now // 3) % 2],
                    gpu_load=(12, 3)[(index + now // 3) % 2],
                    power_w=(45, 79.9)[(index + now // 3) % 2])
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 16))
            if decision['next_command'] == 15:
                break
        self.assertEqual(decision['next_command'], 15)
        self.assertEqual(decision['operating']['failure_credit'], 60)

    def test_diagnostics_report_predicate_and_actual_source(self):
        controller = exit_tests.EnhancedExitR3Tests().restored()
        decision = step(controller, 0)
        self.assertEqual(decision['control_status']['primary_reason'], 'ceiling_exit_requalifying')
        self.assertEqual(decision['control_status']['next_condition'], 'ceiling_requalification_collecting')
        self.assertIsNone(decision['control_status']['blocking_source'])
        self.assertEqual(decision['control_status']['context_kind'], 'idle')
        for now in range(3, 58, 3):
            step(controller, now)
        decision = step(controller, 60, load=15.001)
        self.assertEqual(decision['control_status']['blocking_reason'], 'ceiling_requalification_context_changed')
        self.assertEqual(decision['operating']['failure_credit'], 0)

    def test_clock_diagnostics_and_nonfinite_baseline_never_donate_credit(self):
        for mode in ('feedback', 'source', 'gap', 'nonfinite'):
            with self.subTest(mode=mode):
                controller = exit_tests.EnhancedExitR3Tests().restored()
                for now in range(0, 31, 3):
                    step(controller, now)
                original = controller._ceiling_requalification
                target = controller.thermal_view.observations[-1][0]
                def qualify(frame, *args, **kwargs):
                    if mode == 'source':
                        view = controller.thermal_view
                        controller.thermal_view = replace(view, observations=tuple(
                            (key, temp, stamp - 3 if key == target else stamp)
                            for key, temp, stamp in view.observations))
                    return original(frame, *args, **kwargs)
                with patch.object(controller, '_ceiling_requalification', side_effect=qualify):
                    now = 36 if mode == 'gap' else 33
                    nodes = fleet(now, cpu=40, gpu=38, board=46, load=5, power=45)
                    if mode == 'nonfinite':
                        nodes[2]['gpu_load'] = math.nan
                    decision = controller.decide(nodes, now, minimum_level=0,
                        feedback=feedback(now, 16, observed_mono=30 if mode == 'feedback' else now))
                condition = 'advancing_evidence' if mode in ('feedback', 'source') else 'fresh_evidence'
                self.assertEqual(decision['control_status']['blocking_reason'], 'ceiling_requalification_' + condition)
                self.assertEqual(decision['operating']['failure_credit'], 30 if condition == 'advancing_evidence' else 0)
                self.assertEqual(decision['next_command'], 16)
                self.assertEqual(decision['control_status']['blocking_source'], target if mode == 'source' else None)

    def test_temperature_rejections_keep_complete_credit_and_name_source(self):
        for mode, expected in (('slope', 'temperature_slope'), ('range', 'temperature_range'),
                ('median', 'temperature_median'), ('projection', 'temperature_window'),
                ('persistent', 'persistent_heat'), ('bad_source', 'fresh_evidence')):
            controller = exit_tests.EnhancedExitR3Tests().restored()
            original = controller._ceiling_requalification
            def qualify(frame, *args, **kwargs):
                target = next(k for k in frame['evidence'] if k[0] == HOSTS[2])
                if mode == 'projection' and frame['now'] == 60:
                    frame['evidence'][target]['guard_projected'] = 80
                if mode == 'persistent':
                    frame['evidence'][target]['persistent_heat'] = True
                if mode == 'bad_source' and frame['now'] == 60:
                    frame['telemetry_fault'] = True
                return original(frame, *args, **kwargs)
            if mode == 'persistent':
                controller = danger()
                original = controller._ceiling_requalification
            with patch.object(controller, '_ceiling_requalification', side_effect=qualify):
                for now in range(9 if mode == 'persistent' else 0, 61, 3):
                    nodes = fleet(now, cpu=40, gpu=38, board=46, load=5, power=45)
                    if mode in ('slope', 'range', 'median'):
                        value = 40 + (.012 * now if mode == 'slope' else
                            2.1 if mode == 'range' and now == 30 else
                            .4 if mode == 'median' and now >= 45 else 0)
                        nodes[2]['sensors'][0]['temp'] = value
                    decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 16))
            self.assertEqual(decision['next_command'], 16, mode)
            self.assertEqual(decision['control_status']['blocking_reason'], 'ceiling_requalification_' + expected)
            if mode in ('slope', 'range', 'median', 'projection', 'persistent'):
                self.assertEqual(decision['control_status']['blocking_source'][0], HOSTS[2])
            else:
                self.assertIsNone(decision['control_status']['blocking_source'])
            if mode in ('slope', 'range', 'median'):
                self.assertEqual(decision['operating']['failure_credit'], 60)

    def test_r4_history_whitelist_does_not_infer_old_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            for now, spec in ((100, policy_spec()), (103, dict(version='closed-loop-r1'))):
                journal.append(dict(ts=now, decision=dict(policy_spec=spec)))
            samples = journal.history(0, 104)['samples']
            self.assertNotIn('policy_spec', samples[0]['decision'])
            self.assertEqual(samples[1]['decision']['policy_spec'],
                dict(version='enhanced80-r4', enhanced_admission_c=80))

    def test_history_preserves_only_valid_observed_context_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            for now, kind in ((100, 'idle'), (103, 'strict'), (106, 'invented'), (109, None)):
                journal.append(dict(ts=now, decision=dict(control_status=dict(
                    primary_reason='ceiling_exit_requalifying', release_basis='ceiling80', context_kind=kind))))
            samples = {sample['ts']: sample for sample in journal.history(0, 110)['samples']}
            self.assertEqual(samples[100]['decision']['control_status'].get('context_kind'), 'idle')
            self.assertEqual(samples[103]['decision']['control_status'].get('context_kind'), 'strict')
            for now in (106, 109):
                self.assertNotIn('context_kind', samples[now]['decision']['control_status'])


class CandidateDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_metadata_digest_blocks_failed_and_older_acknowledgements(self):
        controller = danger()
        old = controller.export_thermal_memory()
        memory = controller._thermal_memory
        for key in list(memory.ceiling_dangers):
            memory.remember_ceiling_danger(key[:5], safe_level=16, failed_level=15, baseline=[50, 50, 90])
        new = controller.export_thermal_memory()
        self.assertNotEqual(memory_digest(old), memory_digest(new))
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(call, *args):
            entered.set()
            await release.wait()
            return call(*args)
        store = Mock(confirmed_digest=None)
        store.load.return_value = None
        store.save.side_effect = memory_digest
        gate = ThermalMemoryGate(store)
        holder = Mock()
        holder.export_thermal_memory.return_value = old
        await gate.initialize(holder, 9)
        with patch('policy_memory.asyncio.to_thread', side_effect=delayed):
            gate.publish(holder)
            await entered.wait()
            holder.export_thermal_memory.return_value = new
            gate.invalidate()
            gate.publish(holder)
            self.assertFalse(gate.ready)
            release.set()
            await gate.task
        self.assertFalse(gate.ready)
        store.save.side_effect = OSError('save unavailable')
        gate.publish(holder)
        await gate.task
        self.assertFalse(gate.ready)
        store.save.side_effect = memory_digest
        gate.publish(holder)
        await gate.task
        self.assertTrue(gate.ready)
        with tempfile.TemporaryDirectory() as directory:
            saved = ThermalMemoryStore(Path(directory), {'device': 1}, atomic_write, Mock())
            saved.save(new)
            reopened = ThermalMemoryStore(Path(directory), {'device': 1}, atomic_write, Mock())
            restored = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            restored_gate = ThermalMemoryGate(reopened)
            await restored_gate.initialize(restored, 9)
            self.assertFalse(restored_gate.ready)
            restored_gate.publish(restored)
            await restored_gate.task
            self.assertTrue(restored_gate.ready)
            for now in range(9, 100, 3):
                decision = step(restored, now, load=60, power=100)
                self.assertEqual(decision['next_command'], 16)
                self.assertEqual(decision['operating']['failure_credit'], 0)


if __name__ == '__main__':
    unittest.main()
