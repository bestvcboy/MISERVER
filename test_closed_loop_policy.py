import json
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from core import Controller, FailedDescentRestoration, Journal, Measured80
from thermal_policy import MAX_BYTES, MAX_FAILURES, MAX_SOURCES, ThermalMemory, policy_spec
from test_core import HOSTS
from test_scenarios import feedback, fleet


class ClosedLoopPolicyTests(unittest.TestCase):
    def controller(self):
        return Controller(HOSTS, rules_only=True, high_wind_gate=True)

    def step(self, controller, now, actual=10, *, rebound=0, load=5, power=45, nodes=None, **options):
        return controller.decide(nodes or fleet(now, cpu=38 + rebound, gpu=38 + rebound,
            board=46 + rebound, load=load, power=power), now, minimum_level=0,
            feedback=feedback(now, actual), **options)

    def test_slow_rebound_requests_before_old_570_seconds(self):
        controller, actual, edges = self.controller(), 16, []
        for now in range(0, 721, 3):
            decision = self.step(controller, now, actual, rebound=min(3, now / 120))
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertTrue(edges)
        self.assertLessEqual(edges[0][0], 243)
        self.assertTrue(all(before - after == 1 for _, before, after in edges))
        self.assertGreaterEqual(len(edges), 2, edges)

    def test_stable_loaded_consumes_preparation_then_descends(self):
        controller, actual, edges = self.controller(), 10, []
        for now in range(0, 721, 3):
            decision = self.step(controller, now, actual, load=60, power=100)
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertTrue(edges)
        self.assertLessEqual(edges[0][0], 243)
        self.assertLess(actual, 8)
        self.assertEqual(decision['control_status']['workload_phase'], 'stable_loaded')

    def test_warm_plateau_rebases_without_a_second_observation(self):
        controller, actual, edges = self.controller(), 10, []
        for now in range(0, 900, 3):
            decision = self.step(controller, now, actual, rebound=min(4, now / 120))
            if decision['level'] < actual:
                edges.append(now)
            actual = decision['level']
        resumed = [stamp for stamp in edges if stamp >= 480]
        self.assertTrue(resumed, edges)
        self.assertLessEqual(resumed[0], 666, edges)

    def test_infinite_slow_warming_holds_with_named_reason_then_protects(self):
        controller, actual, held = self.controller(), 10, False
        for now in range(0, 800, 3):
            decision = self.step(controller, now, actual, rebound=now / 120)
            held |= decision['control_status']['primary_reason'] == 'warming_budget_exhausted'
            actual = decision['level']
        self.assertTrue(held)
        self.assertEqual(actual, 9)
        self.assertEqual(controller._policy['last_downward']['edge'], (10, 9))
        hot = fleet(801, cpu=80, gpu=38, board=46, load=5, power=45)
        frames = {}
        advance = controller._advance_fixed

        def inspect(frame):
            fields = ('demand', 'power_warning', 'power_guard', 'thermal_risk')
            raw = tuple(frame[key] for key in fields)
            result = advance(frame)
            self.assertEqual(tuple(frame[key] for key in fields), raw)
            frames[frame['now']] = raw
            return result

        with patch.object(controller, '_advance_fixed', side_effect=inspect):
            decision = self.step(controller, 801, actual, nodes=hot)
        self.assertEqual(frames[801], (16, False, False, True))
        self.assertIsInstance(controller.high_wind.proof, Measured80)
        self.assertEqual(controller.high_wind.normal_ceiling_at(801), 16)
        self.assertEqual((decision['level'], controller.motion.restore_level,
            controller.motion.restore_from, controller.motion.deadline), (10, 10, 9, 861))
        self.assertEqual(decision['control_status']['phase'], 'recovering')
        self.assertEqual(decision['control_status']['primary_reason'], 'attributed_descent_failure')
        self.assertEqual((decision['execution_trial']['from'], decision['execution_trial']['to'],
            decision['execution_trial']['purpose'], decision['execution_trial']['status']),
            (9, 10, 'rollback', 'pending'))
        failures = controller.export_thermal_memory()['failures']
        self.assertEqual(len(failures), len(HOSTS))
        self.assertEqual({tuple(row['source']) for row in failures},
            {(node['host'], 'DGX', node['power_source'], 'acpi:0:acpitz', 'acpi') for node in hot})
        self.assertTrue(all((row['safe_level'], row['failed_level'], row['baseline'],
            row['automatic'], row['failure_class']) == (10, 9, [5, 5, 45], True, 'thermal_failure')
            for row in failures))
        self.assertTrue(decision['retry_blocked'])
        actual, last_increase, increases = decision['level'], 804, []
        for now in range(804, 955, 3):
            decision = self.step(controller, now, actual,
                nodes=fleet(now, cpu=80, gpu=38, board=46, load=5, power=45))
            self.assertGreaterEqual(decision['level'], actual)
            self.assertEqual(controller.export_thermal_memory()['failures'], failures)
            self.assertTrue(decision['retry_blocked'])
            if decision['level'] > actual:
                self.assertEqual(decision['level'], actual + 1)
                self.assertGreaterEqual(now - last_increase, 10)
                last_increase = now
                increases.append(now)
            actual = decision['level']
            if actual == 16 and decision['execution_trial']['status'] == 'confirmed':
                break
        self.assertEqual((actual, decision['execution_trial']['status']), (16, 'confirmed'))
        self.assertEqual(len(increases), 6)
        cold_at = now + 3
        exit_decision = self.step(controller, cold_at, 16)
        self.assertEqual((exit_decision['level'], exit_decision['execution_trial']['purpose']),
            (15, 'ceiling_exit'))
        failed_at = cold_at + 3
        with patch.object(controller, '_advance_fixed', side_effect=inspect):
            restored = self.step(controller, failed_at, 15,
                nodes=fleet(failed_at, cpu=80, gpu=38, board=46, load=5, power=45))
        self.assertEqual(frames[failed_at], (16, False, False, True))
        proof = controller.high_wind.proof
        self.assertIsInstance(proof, FailedDescentRestoration)
        self.assertEqual((proof.from_level, proof.destination, proof.edge, proof.original_deadline,
            proof.diagnosed_mono, proof.generation, proof.failure_class),
            (15, 16, (16, 15), failed_at + 60, failed_at, controller.thermal_view.generation, 'thermal_failure'))
        self.assertEqual(proof.source, tuple(failures[0]['source']))
        self.assertEqual((restored['level'], restored['high_wind']['exception'],
            restored['control_status']['primary_reason']), (16, 'FailedDescentRestoration', 'failed_descent_restoration'))
        self.assertTrue(controller.high_wind.permits(failed_at, 15, 16))
        self.assertFalse(controller.high_wind.permits(failed_at, 15, 14))
        high_failures = controller.export_thermal_memory()['failures']
        self.assertEqual(len(high_failures), 2 * len(HOSTS))
        self.assertTrue(all(row in high_failures for row in failures))
        new_failures = [row for row in high_failures if row['failed_level'] == 15]
        self.assertEqual(len(new_failures), len(HOSTS))
        self.assertEqual({tuple(row['source']) for row in high_failures},
            {tuple(row['source']) for row in failures})
        self.assertTrue(all((row['safe_level'], row['failed_level'], row['baseline'],
            row['automatic'], row['failure_class']) == (16, 15, [5, 5, 45], True, 'thermal_failure')
            for row in new_failures))
        self.assertTrue(restored['retry_blocked'])

    def test_snapshot_is_pure_strict_and_unchanged_on_flat_clock_ticks(self):
        controller = self.controller()
        for now in range(0, 121, 3):
            self.step(controller, now)
        first = controller.export_thermal_memory()
        self.assertEqual(first, controller.export_thermal_memory())
        json.dumps(first, allow_nan=False)
        self.step(controller, 123)
        self.assertEqual(first, controller.export_thermal_memory())
        first['schema'] = -1
        self.assertNotEqual(first, controller.export_thermal_memory())

    def test_pending_snapshot_changes_and_restores_no_permission(self):
        controller = self.controller()
        for now in range(0, 211, 3):
            before = controller.export_thermal_memory()
            decision = self.step(controller, now)
            if decision['level'] < 10:
                break
        self.assertEqual(decision['level'], 9)
        payload = controller.export_thermal_memory()
        self.assertNotEqual(before, payload)
        self.assertIsNotNone(payload['open_edge'])
        restarted = self.controller()
        restarted.restore_thermal_memory(json.loads(json.dumps(payload)), 1000)
        self.assertEqual(restarted.operating.observe_credit, 0)
        self.assertFalse(restarted.high_wind.allowed)
        self.assertFalse(restarted._pending())
        self.assertIsNone(restarted.motion.deadline)
        decision = self.step(restarted, 1000, 9)
        self.assertGreaterEqual(decision['level'], 9)

    def test_invalid_restore_is_atomic(self):
        controller = self.controller()
        payload = controller.export_thermal_memory()
        huge_anchor = dict(source=[HOSTS[0], 'DGX', 'spbm', 'acpi:0:acpitz', 'acpi'],
            anchor_c=10**1000, peak_c=10**1000)
        for bad in (None, [], {}, dict(payload, schema=True), dict(payload, unknown=1),
                    dict(payload, anchors=[{'anchor_c': float('nan')}]), dict(payload, anchors=[huge_anchor])):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                controller.restore_thermal_memory(bad, 0)
            self.assertEqual(controller.export_thermal_memory(), payload)
        for now in (None, -1, True, float('inf'), 10**1000):
            with self.subTest(now=now), self.assertRaises(ValueError):
                controller.restore_thermal_memory(payload, now)
            self.assertEqual(controller.export_thermal_memory(), payload)

    def failed_one_host(self):
        controller, actual = self.controller(), 10
        for now in range(0, 214, 3):
            decision = self.step(controller, now, actual, load=25, power=70)
            actual = decision['level']
        nodes = fleet(216, cpu=38, gpu=38, board=46, load=25, power=70)
        nodes[0]['sensors'][0]['temp'] = nodes[0]['cpu_temp'] = 58
        decision = self.step(controller, 216, actual, nodes=nodes)
        self.assertEqual(decision['level'], 10)
        self.assertTrue(decision['retry_blocked'])
        for now in range(219, 307, 3):
            self.step(controller, now, 10, load=25, power=70)
        return controller

    def test_failure_survives_restart_replacement_and_original_identity_return(self):
        payload = self.failed_one_host().export_thermal_memory()
        self.assertEqual(len(payload['failures']), 1)
        controller = self.controller()
        controller.restore_thermal_memory(payload, 1000)
        for now in range(1000, 1064, 3):
            decision = self.step(controller, now, 10, load=25, power=70)
            self.assertTrue(decision['retry_blocked'])
            self.assertGreaterEqual(decision['level'], 10)
        for now in range(1066, 1127, 3):
            nodes = fleet(now, cpu=38, gpu=38, board=46, load=25, power=70)
            nodes[0]['hostname'] = 'replacement'
            decision = self.step(controller, now, 10, nodes=nodes)
        self.assertFalse(decision['retry_blocked'])
        self.assertEqual(controller.export_thermal_memory()['failures'], payload['failures'])
        for now in range(1129, 1190, 3):
            decision = self.step(controller, now, 10, load=25, power=70)
        self.assertTrue(decision['retry_blocked'])
        self.assertEqual(decision['level'], 10)

    def test_failure_rehabilitation_uses_failed_hosts_baseline_and_changes_snapshot(self):
        controller = self.failed_one_host()
        failures = deepcopy(controller.export_thermal_memory()['failures'])
        for now in range(309, 432, 3):
            nodes = fleet(now, cpu=38, gpu=38, board=46, load=5, power=45)
            nodes[0].update(cpu_load=25, gpu_load=25, power_w=70)
            decision = self.step(controller, now, 10, nodes=nodes)
            self.assertTrue(decision['retry_blocked'])
        self.assertEqual(controller.export_thermal_memory()['failures'], failures)
        for now in range(432, 495, 3):
            nodes = fleet(now, cpu=38, gpu=38, board=46, load=25, power=70)
            nodes[0].update(cpu_load=10, gpu_load=10, power_w=56)
            before = controller.export_thermal_memory()
            decision = self.step(controller, now, 10, nodes=nodes)
        self.assertFalse(decision['retry_blocked'])
        self.assertEqual(controller.export_thermal_memory()['failures'], [])
        self.assertNotEqual(before, controller.export_thermal_memory())
        self.assertEqual(decision['operating']['observe_credit'], 0)

    def test_cross_host_maximum_swap_cannot_attribute_changed_hosts_failure(self):
        for changed_host, failure_expected in ((0, False), (1, True)):
            with self.subTest(changed_host=changed_host):
                controller, actual = self.controller(), 10
                for now in range(0, 220, 3):
                    nodes = fleet(now, cpu=38, gpu=38, board=46, load=25, power=70)
                    nodes[changed_host].update(cpu_load=5, gpu_load=5, power_w=45)
                    if now >= 216:
                        nodes[changed_host].update(cpu_load=20, gpu_load=20, power_w=60)
                        nodes[(changed_host + 1) % len(nodes)].update(cpu_load=5, gpu_load=5, power_w=45)
                    if now == 219:
                        nodes[0]['sensors'][0]['temp'] = nodes[0]['cpu_temp'] = 58
                    decision = self.step(controller, now, actual, nodes=nodes)
                    actual = decision['level']
                self.assertEqual(decision['retry_blocked'], failure_expected)
                if failure_expected:
                    self.assertEqual(decision['operating']['failed_floor']['witness']['source'][0], HOSTS[0])

    def test_restoration_intent_and_failure_are_durable_without_deadlines(self):
        controller, actual = self.controller(), 10
        for now in range(0, 217, 3):
            nodes = fleet(now, cpu=58 if now == 216 else 38, gpu=38, board=46, load=5, power=45)
            decision = self.step(controller, now, actual, nodes=nodes)
            actual = decision['level']
        payload = controller.export_thermal_memory()
        self.assertEqual(payload['intent']['purpose'], 'rollback')
        self.assertEqual(payload['intent']['to_level'], 10)
        self.assertTrue(payload['failures'])
        text = json.dumps(payload, allow_nan=False)
        self.assertNotIn('deadline', text)
        self.assertNotIn('credit', text)
        self.assertNotIn('mono', text)
        controller = self.controller()
        controller.restore_thermal_memory(payload, 1)
        self.assertFalse(controller._pending())
        self.assertFalse(controller.high_wind.allowed)
        self.assertIsNone(controller.motion.restore_level)
        self.assertTrue(controller.export_thermal_memory()['failures'])

    def test_unconfirmed_memory_gate_timeouts_do_not_create_thermal_failures(self):
        controller, deadlines = self.controller(), set()
        for now in range(0, 1000, 3):
            decision = self.step(controller, now, 10)
            trial = decision.get('execution_trial')
            if trial and trial['status'] == 'pending':
                deadlines.add(trial['original_deadline'])
            self.assertFalse(decision['retry_blocked'])
            self.assertEqual(controller.export_thermal_memory()['failures'], [])
        self.assertGreaterEqual(len(deadlines), 2)
        self.assertTrue(all(b - a >= 180 for a, b in zip(sorted(deadlines), sorted(deadlines)[1:])))

    def test_repeating_intent_is_a_new_snapshot_even_at_same_endpoints(self):
        controller, snapshots = self.controller(), []
        for now in range(0, 650, 3):
            decision = self.step(controller, now, 10)
            if decision['level'] == 9 and decision['execution_trial']['requested_at'] == now:
                snapshots.append(controller.export_thermal_memory())
        self.assertGreaterEqual(len(snapshots), 2)
        self.assertNotEqual(snapshots[0]['revision'], snapshots[1]['revision'])

    def test_overflow_keeps_all_existing_failures_and_permanently_holds_descent(self):
        memory = ThermalMemory()
        for index in range(MAX_FAILURES + 1):
            memory.add_failure((HOSTS[0], f'identity-{index}', 'spbm', 'gpu:nvidia-smi', 'gpu'),
                dict(safe_level=10, failed_level=9, baseline=[25, 25, 70], automatic=True))
        payload = memory.export()
        self.assertEqual(len(payload['failures']), MAX_FAILURES)
        self.assertTrue(payload['saturated'])
        self.assertTrue(any(r['source'][1] == 'identity-0' for r in payload['failures']))
        controller = self.controller()
        controller.restore_thermal_memory(payload, 0)
        for now in range(0, 600, 3):
            decision = self.step(controller, now, 10)
            self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['control_status']['primary_reason'], 'memory_capacity')

    def test_open_edge_nested_caps_and_injected_permission_fields_are_rejected(self):
        controller = self.controller()
        for now in range(0, 211, 3):
            self.step(controller, now)
        payload = controller.export_thermal_memory()
        for field, value in (('sources', payload['open_edge']['sources'] * (MAX_SOURCES + 1)),
                             ('from_level', True), ('automatic', 1), ('stage', 'x' * 1000), ('deadline', 99999)):
            bad = deepcopy(payload)
            bad['open_edge'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.controller().restore_thermal_memory(bad, 0)
        for field, value in (('source', ['x' * 257] * 5), ('baseline', [1] * 4),
                             ('temp_c', float('inf')), ('attributable', 1), ('credit', 180)):
            bad = deepcopy(payload)
            bad['open_edge']['sources'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.controller().restore_thermal_memory(bad, 0)

    def test_policy_spec_matches_executable_limits_and_returns_detached_values(self):
        from core import LIMITS
        spec = policy_spec()
        self.assertEqual(spec, Controller.policy_spec())
        self.assertEqual(spec['targets_c'], {group: limit[0] for group, limit in LIMITS.items()})
        self.assertEqual(spec['critical_c'], 75)
        spec['targets_c']['cpu'] = 999
        self.assertEqual(policy_spec()['targets_c']['cpu'], 60)

    def test_warming_budget_survives_restart_and_cannot_restart_observation_allowance(self):
        controller, actual = self.controller(), 10
        for now in range(0, 361, 3):
            decision = self.step(controller, now, actual, rebound=now / 120)
            actual = decision['level']
        payload = controller.export_thermal_memory()
        controller = self.controller()
        controller.restore_thermal_memory(payload, 1000)
        for age in range(0, 400, 3):
            decision = self.step(controller, 1000 + age, actual, rebound=3 + age / 120)
            self.assertGreaterEqual(decision['level'], actual)
        self.assertEqual(decision['control_status']['primary_reason'], 'warming_budget_exhausted')

    def test_all_record_and_edge_caps_fit_the_strict_snapshot_byte_limit(self):
        memory, sources = ThermalMemory(), []
        for index in range(MAX_SOURCES):
            key = (HOSTS[0], 'DGX', 'spbm', 's' * 150 + str(index), 'other')
            memory.add_anchor(key, 99.99999999999999, 149.99999999999997)
            sources.append(dict(source=list(key), baseline=[99.99999999999999] * 2 + [999.9999999999999],
                temp_c=149.99999999999997, attributable=True))
            if index < MAX_FAILURES:
                memory.add_failure(key, dict(safe_level=16, failed_level=15,
                    baseline=[99.99999999999999] * 2 + [999.9999999999999], automatic=True))
        memory.open_edge = dict(from_level=16, to_level=15, automatic=True, sources=sources, stage='pending')
        payload = memory.export()
        self.assertLess(len(json.dumps(payload, allow_nan=False).encode()), MAX_BYTES)
        self.assertEqual(ThermalMemory.restore(payload).export(), payload)

    def test_anchor_uses_completed_block_medians_not_isolated_raw_spikes(self):
        controller = self.controller()
        for now in range(0, 151, 3):
            decision = self.step(controller, now, rebound=4 if now in (30, 90, 120) else 0)
            self.assertGreaterEqual(decision['level'], 10)
            if now == 60:
                initial = controller.export_thermal_memory()['anchors']
                self.assertTrue(initial)
        self.assertEqual(initial, controller.export_thermal_memory()['anchors'])
        self.assertTrue(all(row['anchor_c'] == row['peak_c'] for row in initial))

    def test_plateau_rebase_resets_peak_to_new_median(self):
        controller, actual, exhausted, rebased = self.controller(), 10, False, False
        for now in range(0, 901, 3):
            rebound = min(4, now / 120) + (.3 if now >= 480 and now % 30 == 0 else 0)
            decision = self.step(controller, now, actual, rebound=rebound)
            actual = decision['level']
            rows = controller.export_thermal_memory()['anchors']
            exhausted |= any(row['peak_c'] - row['anchor_c'] > 2 for row in rows)
            if exhausted and rows and all(row['peak_c'] == row['anchor_c'] for row in rows):
                rebased = True
                self.assertLessEqual(now, 666)
                break
        self.assertTrue(exhausted)
        self.assertTrue(rebased)

    def test_raw_power_flags_and_demand_survive_preparation_and_settled_suppression(self):
        controller, actual, phases, edges = self.controller(), 10, set(), []
        advance = controller._advance_fixed
        frames = {}

        def inspect(frame):
            fields = ('power_warning', 'power_guard', 'demand', 'thermal_risk')
            raw = tuple(frame[k] for k in fields)
            result = advance(frame)
            self.assertEqual(raw, tuple(frame[k] for k in fields))
            frames[frame['now']] = raw
            if 6 <= frame['now'] <= 723:
                self.assertTrue(raw[1])
                self.assertGreaterEqual(raw[2], 10)
            phases.add(controller._workload_cycle.phase)
            if controller._workload_cycle.phase == 'preparing':
                self.assertGreaterEqual(controller.level, frame['feedback']['level'])
            return result

        with patch.object(controller, '_advance_fixed', side_effect=inspect):
            for now in range(0, 721, 3):
                decision = self.step(controller, now, actual, load=60, power=100)
                if decision['level'] < actual:
                    edges.append(now)
                actual = decision['level']
            hot = fleet(723, cpu=80, gpu=38, board=46, load=60, power=100)
            self.assertEqual(actual, 7)
            self.assertEqual(controller._policy['last_downward']['edge'], (8, 7))
            decision = self.step(controller, 723, actual, nodes=hot)
            self.assertEqual(frames[723], (False, True, 16, True))
            self.assertIsInstance(controller.high_wind.proof, Measured80)
            self.assertEqual(controller.high_wind.normal_ceiling_at(723), 16)
            self.assertEqual((decision['level'], controller.motion.restore_level,
                controller.motion.restore_from, controller.motion.deadline), (8, 8, 7, 783))
            self.assertEqual(decision['control_status']['phase'], 'recovering')
            self.assertEqual(decision['control_status']['primary_reason'], 'attributed_descent_failure')
            self.assertEqual((decision['execution_trial']['from'], decision['execution_trial']['to'],
                decision['execution_trial']['purpose'], decision['execution_trial']['status']),
                (7, 8, 'rollback', 'pending'))
            failures = controller.export_thermal_memory()['failures']
            self.assertEqual(len(failures), len(HOSTS))
            self.assertEqual({tuple(row['source']) for row in failures},
                {(node['host'], 'DGX', node['power_source'], 'acpi:0:acpitz', 'acpi') for node in hot})
            self.assertTrue(all((row['safe_level'], row['failed_level'], row['baseline'],
                row['automatic'], row['failure_class']) == (8, 7, [60, 60, 100], True, 'thermal_failure')
                for row in failures))
            self.assertTrue(decision['retry_blocked'])
            actual, last_increase, increases = decision['level'], 726, []
            for now in range(726, 769, 3):
                decision = self.step(controller, now, actual,
                    nodes=fleet(now, cpu=80, gpu=38, board=46, load=60, power=100))
                self.assertEqual(frames[now][2:], (16, True))
                self.assertGreaterEqual(decision['level'], actual)
                self.assertEqual(controller.export_thermal_memory()['failures'], failures)
                self.assertTrue(decision['retry_blocked'])
                if decision['level'] > actual:
                    self.assertEqual(decision['level'], actual + (1 if actual >= 10 else 2))
                    self.assertGreaterEqual(now - last_increase, 10)
                    last_increase = now
                    increases.append((actual, decision['level']))
                actual = decision['level']
            self.assertEqual(increases, [(8, 10), (10, 11), (11, 12)])
        self.assertEqual(phases, {'preparing', 'stable_loaded'})
        self.assertTrue(edges)
        self.assertLessEqual(edges[0], 243)

    def test_workload_comparability_uses_original_power_boundary(self):
        for power, expected in ((110, 'stable_loaded'), (110.01, 'preparing')):
            with self.subTest(power=power):
                controller = self.controller()
                for now in range(0, 121, 3):
                    decision = self.step(controller, now, load=60, power=100)
                self.assertEqual(decision['control_status']['workload_phase'], 'stable_loaded')
                decision = self.step(controller, 123, load=60, power=power)
                self.assertEqual(decision['control_status']['workload_phase'], expected)
                self.assertGreaterEqual(decision['level'], 10)

    def test_restart_at_origin_cannot_prove_pending_or_confirmed_edge_never_failed(self):
        original = self.controller()
        for now in range(0, 211, 3):
            decision = self.step(original, now, 8, load=25, power=70)
        self.assertEqual(decision['level'], 7)
        pending = original.export_thermal_memory()
        self.step(original, 213, 7, load=25, power=70)
        confirmed = original.export_thermal_memory()
        self.assertEqual(pending['open_edge']['stage'], 'pending')
        self.assertEqual(confirmed['open_edge']['stage'], 'confirmed')
        hot = fleet(216, cpu=58, gpu=38, board=46, load=25, power=70)
        self.assertEqual(self.step(original, 216, 7, nodes=hot)['level'], 8)
        self.assertTrue(original.export_thermal_memory()['failures'])
        unresolved = deepcopy(pending)
        unresolved['open_edge']['stage'] = 'unresolved'
        manual = deepcopy(pending)
        manual['open_edge']['automatic'] = False
        for payload in (pending, confirmed, unresolved, manual):
            with self.subTest(stage=payload['open_edge']['stage'], automatic=payload['open_edge']['automatic']):
                restarted = self.controller()
                restarted.restore_thermal_memory(payload, 1000)
                self.assertEqual(restarted.operating.observe_credit, 0)
                self.assertFalse(restarted._pending())
                for now in range(1000, 1601, 3):
                    decision = self.step(restarted, now, 8, load=25, power=70)
                    self.assertGreaterEqual(decision['level'], 8)
                self.assertEqual(decision['control_status']['primary_reason'], 'unresolved_descent')
                restrictions = restarted.export_thermal_memory()['failures']
                self.assertTrue(restrictions)
                self.assertEqual({row['failure_class'] for row in restrictions}, {'unresolved_descent'})
                first_retry = None
                for now in range(1603, 1964, 3):
                    decision = self.step(restarted, now, 8, load=10, power=56)
                    if decision['level'] < 8:
                        first_retry = now
                        break
                self.assertEqual(restarted.export_thermal_memory()['failures'], [])
                self.assertIsNotNone(first_retry)
                self.assertGreaterEqual(first_retry, 1603 + 60 + 180)

    def test_clearing_stronger_failure_does_not_erase_older_lower_gear_failure(self):
        sample = fleet(0)[0]
        sensor = sample['sensors'][0]
        source = (sample['host'], sample['hostname'], sample['power_source'], sensor['id'], sensor['group'])
        memory = ThermalMemory()
        for safe, failed, baseline in ((8, 7, [5, 5, 45]), (9, 8, [50, 50, 100])):
            memory.add_failure(source, dict(safe_level=safe, failed_level=failed,
                baseline=baseline, automatic=True))
        controller = self.controller()
        controller.restore_thermal_memory(memory.export(), 0)
        for now in range(0, 601, 3):
            decision = controller.decide(fleet(now, cpu=38, gpu=38, board=46, load=30, power=80),
                now, minimum_level=10, feedback=feedback(now, 10))
        self.assertEqual([row['failed_level'] for row in controller.export_thermal_memory()['failures']], [7])
        self.assertEqual(decision['operating']['failed_floor']['failed_level'], 7)

    def test_control_status_history_projection_is_bounded_and_preserves_unknowns(self):
        controller = self.controller()
        decision = self.step(controller, 0)
        expected = deepcopy(decision['control_status'])
        decision['control_status']['private_token'] = 'do-not-publish'
        with tempfile.TemporaryDirectory() as root, patch.object(Journal, '_sync_directory'):
            journal = Journal(root)
            journal.append(dict(ts=1, decision=decision, nodes=[]))
            projected = journal.history(0, 2)['samples'][0]['decision']['control_status']
        self.assertEqual(projected, expected)
        self.assertNotIn('do-not-publish', json.dumps(projected))


if __name__ == '__main__':
    unittest.main()
