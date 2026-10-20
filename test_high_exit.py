import json
import tempfile
import unittest
from copy import deepcopy

from core import Controller, FailedDescentRestoration, Journal, Measured80, NoAscent, SourceFaultProtection
from test_core import HOSTS
from test_scenarios import feedback, fleet
from thermal_policy import HIGH_EXIT_TAG, ThermalMemory, source_key


class HighExitBoundaryTests(unittest.TestCase):
    def controller(self):
        return Controller(HOSTS, rules_only=True, high_wind_gate=True)

    def tick(self, controller, now, actual=16, *, cpu=65, gpu=52, board=64,
             load=60, power=100, readback=None, mutate=None, **options):
        nodes = fleet(now, cpu=cpu, gpu=gpu, board=board, load=load, power=power)
        if mutate:
            mutate(nodes)
        return controller.decide(nodes, now, minimum_level=options.pop('minimum_level', 0),
            feedback=feedback(now, actual, **(readback or {})), **options)

    def requested(self, *, cpu=65, actual=16, **options):
        controller = self.controller()
        decision = self.tick(controller, 0, actual, cpu=cpu, **options)
        self.assertEqual(decision['execution_trial']['purpose'], 'ceiling_exit')
        self.assertEqual(decision['level'], actual - 1)
        return controller, 0, decision

    def confirmed(self, **options):
        controller, now, _ = self.requested(**options)
        self.tick(controller, 3, 15, **options)
        self.assertEqual(controller.operating.confirmed_level, 15)
        return controller, 3

    def legacy_payload(self):
        nodes = fleet(0, cpu=50)
        key = source_key(nodes[0], (HOSTS[0], nodes[0]['sensors'][0]['id'], 'acpi'))
        memory = ThermalMemory()
        memory.add_anchor(key, 50, 53)
        memory.open_edge = dict(from_level=16, to_level=15, automatic=True, stage='confirmed',
            release_basis=HIGH_EXIT_TAG, sources=[dict(source=list(key), baseline=[0, 0, 30],
                temp_c=50, attributable=True, exit_origin_c=50)])
        memory.intent = dict(from_level=16, to_level=15, purpose='release', state='confirmed')
        return memory.export()

    def test_every_adjacent_endpoint_to_ten_obeys_thirty_actual_seconds(self):
        c, actual, edges, last_confirm = self.controller(), 16, [], None
        for now in range(0, 211, 3):
            d = self.tick(c, now, actual)
            if c._pending() and d['level'] < actual:
                self.assertEqual(d['level'], actual - 1)
                if last_confirm is not None:
                    self.assertGreaterEqual(now - last_confirm, 30)
                edges.append((actual, d['level']))
                actual = d['level']
                last_confirm = now + 3
            self.assertEqual(c.operating.observe_credit, 0)
            self.assertIsNone(c._thermal_memory.open_edge)
            if c.operating.confirmed_level == 10:
                break
        self.assertEqual(edges, list(zip(range(16, 10, -1), range(15, 9, -1))))

    def test_low_two_degree_oscillations_follow_every_requested_step_to_ten(self):
        c, actual, edges, last_confirm = self.controller(), 16, [], None
        memory = ThermalMemory.restore(self.legacy_payload())
        memory.open_edge = memory.intent = None
        key = next(iter(memory.anchors))
        memory.add_failure(key, dict(safe_level=10, failed_level=9, baseline=[5, 5, 45],
            automatic=True, failure_class='thermal_failure'))
        c.restore_thermal_memory(memory.export(), 0)
        before = c.export_thermal_memory()
        for now in range(0, 211, 3):
            d = self.tick(c, now, actual, cpu=50 + 2 * ((now // 3) % 2),
                gpu=38, board=46, load=5, power=45)
            self.assertLessEqual(d['next_command'], actual)
            self.assertFalse(d['learning_eligible'])
            if d['next_command'] < actual:
                self.assertEqual(d['next_command'], actual - 1)
                self.assertEqual(d['execution_trial']['purpose'], 'ceiling_exit')
                if last_confirm is not None:
                    self.assertGreaterEqual(now - last_confirm, 30)
                edges.append((actual, d['next_command']))
                actual = d['next_command']
                last_confirm = now + 3
            after = c.export_thermal_memory()
            self.assertEqual(after['anchors'], before['anchors'])
            self.assertEqual(after['failures'], before['failures'])
            self.assertIsNone(after['open_edge'])
            self.assertEqual(c.operating.observe_credit, 0)
            if c.operating.confirmed_level == 10:
                break
        self.assertEqual(edges, [(16, 15), (15, 14), (14, 13), (13, 12), (12, 11), (11, 10)])
        self.assertEqual(c.operating.confirmed_level, 10)

    def test_low_rapid_rise_keeps_pending_eleven_to_ten_and_original_deadline(self):
        c, _, d = self.requested(actual=11, cpu=50)
        episode = c.high_wind.proof.operation
        for now, cpu in ((3, 52), (6, 54), (9, 56)):
            d = self.tick(c, now, 11, cpu=cpu)
            self.assertEqual(d['level'], 10)
            self.assertEqual(d['execution_trial']['original_deadline'], 60)
            self.assertIsInstance(c.high_wind.proof, NoAscent)
            self.assertIs(c.high_wind.proof.operation, episode)
            self.assertTrue(c.high_wind.permits(now, 11, 10))
            self.assertFalse(c.high_wind.permits(now, 11, 12))
            self.assertEqual(c.export_thermal_memory()['failures'], [])

    def test_same_source_step_and_rapid_hold_start_at_73_inclusive(self):
        for kind in ('step', 'rapid'):
            for endpoint in (72.999, 73):
                with self.subTest(kind=kind, endpoint=endpoint):
                    start = endpoint - (2 if kind == 'step' else 1.9)
                    c, _, _ = self.requested(actual=11, cpu=start)
                    episode = c.high_wind.proof.operation
                    samples = ((3, endpoint),) if kind == 'step' else ((3, endpoint - .95), (6, endpoint))
                    for now, cpu in samples:
                        d = self.tick(c, now, 11, cpu=cpu)
                    self.assertEqual(d['level'], 11 if endpoint == 73 else 10)
                    self.assertEqual(d['control_status']['primary_reason'],
                        'ceiling_exit_current_rapid_risk' if endpoint == 73 else 'ceiling_exit_pending')
                    self.assertEqual(d['execution_trial']['original_deadline'], 60)
                    self.assertFalse(c.high_wind.permits(now, 11, 12))
                    if endpoint < 73:
                        self.assertIs(c.high_wind.proof.operation, episode)
                        self.assertTrue(c.high_wind.permits(now, 11, 10))
                    self.assertEqual(c.export_thermal_memory()['failures'], [])

    def test_unrelated_hot_flat_sensor_cannot_combine_with_cool_source_rise(self):
        for kind in ('step', 'rapid'):
            with self.subTest(kind=kind):
                c, _, _ = self.requested(actual=11, cpu=50, board=79.999)
                samples = ((3, 52),) if kind == 'step' else ((3, 50.95), (6, 51.9))
                for now, cpu in samples:
                    d = self.tick(c, now, 11, cpu=cpu, board=79.999)
                    self.assertEqual(d['level'], 10)
                    self.assertEqual(d['control_status']['primary_reason'], 'ceiling_exit_pending')
                    self.assertTrue(c.high_wind.permits(now, 11, 10))
                    self.assertFalse(c.high_wind.permits(now, 11, 12))

    def test_near80_rise_pauses_then_resumes_exact_episode_before_original_deadline(self):
        c, _, d = self.requested(actual=11, cpu=77)
        episode = c.high_wind.proof.operation
        operation = c._policy['operation']
        sources = operation['ceiling_sources']
        baseline = operation['baseline']
        intent = deepcopy(c.export_thermal_memory()['intent'])
        for now in (3, 6, 8.999):
            d = self.tick(c, now, 11, cpu=79)
            self.assertEqual(d['level'], 11)
            self.assertEqual(d['control_status']['primary_reason'], 'ceiling_exit_current_rapid_risk')
            self.assertEqual(d['control_status']['risk_evidence_expires_mono'], now + 5.5)
            self.assertEqual(d['execution_trial']['original_deadline'], 60)
            self.assertFalse(c.high_wind.permits(now, 11, 10))
            self.assertFalse(c.high_wind.permits(now, 11, 12))
            self.assertEqual(c.export_thermal_memory()['intent'], intent)
            self.assertEqual(c.export_thermal_memory()['failures'], [])
            self.assertIs(c._policy['operation'], operation)
            self.assertIs(operation['episode'], episode)
            self.assertIs(operation['ceiling_sources'], sources)
            self.assertIs(operation['baseline'], baseline)
            self.assertEqual((operation['requested_at'], operation['destination_requested_at'],
                operation['destination'], operation['attribution']), (0, 0, 10, 'comparable'))
        d = self.tick(c, 9, 11, cpu=79)
        self.assertEqual(d['level'], 10)
        self.assertIsInstance(c.high_wind.proof, NoAscent)
        self.assertIs(c.high_wind.proof.operation, episode)
        self.assertEqual(c.high_wind.proof.original_deadline, 60)
        self.assertEqual(c.export_thermal_memory()['intent'], intent)
        self.assertIs(c._policy['operation'], operation)
        self.assertIs(operation['ceiling_sources'], sources)
        self.assertIs(operation['baseline'], baseline)
        self.assertEqual((operation['requested_at'], operation['destination_requested_at'],
            operation['destination'], operation['attribution']), (0, 0, 10, 'comparable'))
        self.assertTrue(c.high_wind.permits(9, 11, 10))
        self.assertFalse(c.high_wind.permits(9, 11, 12))

    def test_hardware_throttle_independently_holds_cool_rising_pending_exit(self):
        c, _, _ = self.requested(actual=11, cpu=50)
        episode = c.high_wind.proof.operation
        d = self.tick(c, 3, 11, cpu=52,
            mutate=lambda nodes: nodes[0].update(thermal_throttle=True))
        self.assertEqual(d['level'], 11)
        self.assertEqual(d['control_status']['primary_reason'], 'ceiling_exit_hardware_alarm')
        self.assertEqual(d['execution_trial']['original_deadline'], 60)
        self.assertFalse(c.high_wind.permits(3, 11, 10))
        self.assertFalse(c.high_wind.permits(3, 11, 12))
        d = self.tick(c, 6, 11, cpu=54)
        self.assertEqual(d['level'], 10)
        self.assertIs(c.high_wind.proof.operation, episode)
        self.assertEqual(c.high_wind.proof.original_deadline, 60)
        self.assertEqual(c.export_thermal_memory()['failures'], [])

    def test_arrival_during_near_risk_rise_is_recorded_before_hold(self):
        for prior in (11, 16):
            with self.subTest(prior=prior):
                c, _, _ = self.requested(actual=prior, cpu=77)
                d = self.tick(c, 3, prior - 1, cpu=79)
                self.assertEqual(c.operating.confirmed_level, prior - 1)
                self.assertEqual(d['execution_trial']['status'], 'confirmed')
                self.assertEqual(d['level'], prior - 1)
                self.assertEqual(d['control_status']['primary_reason'],
                    'ceiling_exit_complete' if prior == 11 else 'ceiling_exit_current_rapid_risk')
                self.assertFalse(c.high_wind.permits(3, prior - 1, prior))
                self.assertEqual(c.export_thermal_memory()['failures'], [])

    def test_near_risk_arrival_at_original_deadline_times_out_first(self):
        for arrival, expected in ((59.999, 'confirmed'), (60, 'execution_timeout')):
            with self.subTest(arrival=arrival):
                c, _, _ = self.requested(actual=11, cpu=77)
                for now in range(3, 58, 3):
                    self.tick(c, now, 11, cpu=77)
                d = self.tick(c, arrival, 10, cpu=79)
                self.assertEqual(d['execution_trial']['status'], expected)
                self.assertEqual(d['execution_trial']['original_deadline'], 60)

    def test_backward_clock_cannot_requalify_descent_using_a_future_step(self):
        c, _, _ = self.requested(actual=11, cpu=77)
        self.tick(c, 3, 11, cpu=79)
        d = self.tick(c, 2, 11, cpu=79)
        self.assertFalse(c.high_wind.permits(2, 11, 10))
        self.assertIsInstance(c.high_wind.proof, SourceFaultProtection)
        self.assertEqual(d['execution_trial']['original_deadline'], 60)

    def test_loaded_flat_65_through_79_999_bypasses_old_qualification(self):
        for cpu in (65, 67, 69, 70, 71, 74.999, 75, 79.999):
            with self.subTest(cpu=cpu):
                c, now, d = self.requested(cpu=cpu)
                self.assertEqual(now, 0)
                self.assertEqual(d['high_wind_limit'], 10)
                self.assertTrue(c.high_wind.permits(0, 16, 15))
                self.assertFalse(c.high_wind.permits(0, 16, 10))
                self.assertEqual(d['control_status']['required_seconds'], 0)

    def test_manual_minimum_power_and_workload_cannot_recreate_high_normal_ceiling(self):
        for manual in (None, 16):
            c = self.controller()
            for now in (0, 3, 6, 9):
                d = self.tick(c, now, minimum_level=16, manual_level=manual,
                    load=10 if now % 2 else 95, power=50 if now % 2 else 150)
                self.assertEqual((d['demand'], d['high_wind_limit'], d['level']), (10, 10, 15))

    def test_pending_deadline_is_fixed_and_expiry_precedes_matching_arrival(self):
        for offset, expected in ((59.999, 'confirmed'), (60, 'execution_timeout')):
            c, _, _ = self.requested()
            for now in range(3, 58, 3):
                d = self.tick(c, now, manual_level=16)
                self.assertEqual(d['execution_trial']['original_deadline'], 60)
            d = self.tick(c, offset, 15, manual_level=16)
            self.assertEqual(d['execution_trial']['status'], expected)
            self.assertEqual(d['execution_trial']['original_deadline'], 60)

    def test_duplicate_feedback_never_confirms_or_earns_credit(self):
        c, _, _ = self.requested()
        d = self.tick(c, 3, 15, readback=dict(observed_mono=0))
        self.assertNotEqual(d['execution_trial']['status'], 'confirmed')
        self.assertEqual(c.operating.confirmed_level, 16)
        self.assertEqual(c.operating.observe_credit, 0)

    def test_zero_rpm_unknown_and_nonfavorite_do_not_authorize_descent_or_activation(self):
        for values in (dict(rpm=0), dict(level=None), dict(power='off'), dict(mode='auto')):
            with self.subTest(values=values):
                c = self.controller()
                self.tick(c, 0, readback=values)
                self.assertFalse(c.high_wind.permits(0, 16, 15))
                self.assertFalse(c.high_wind.permits_activation(0, 16, 15))

    def test_source_loss_revokes_only_the_descent_not_existing_inflight_history(self):
        for kind in ('sensor', 'host', 'stale', 'missing', 'uncalibrated'):
            with self.subTest(kind=kind):
                c, _, _ = self.requested()
                def change(nodes):
                    if kind == 'sensor': nodes[0]['sensors'][0]['id'] = 'replacement'
                    elif kind == 'host': nodes[0]['hostname'] = 'replacement'
                    elif kind == 'stale': nodes[0]['source_ts'] = 0
                    elif kind == 'missing': nodes[0]['online'] = False
                    else: nodes[0]['sensors'][0]['calibrated'] = False
                d = self.tick(c, 3, mutate=change)
                self.assertFalse(c.high_wind.permits(3, 16, 15))
                self.assertIsInstance(c.high_wind.proof, SourceFaultProtection)
                self.assertEqual(d['execution_trial']['original_deadline'], 60)
                self.assertEqual(c._thermal_memory.failures, {})

    def test_exact_edge_authority_uses_oldest_source_time_and_six_second_ttl(self):
        c = self.controller()
        def aged(nodes): nodes[0]['source_ts'] -= 3
        d = self.tick(c, 100, mutate=aged)
        self.assertEqual(d['level'], 15)
        self.assertEqual(c.high_wind.observed_mono, 96.5)
        self.assertTrue(c.high_wind.permits(102.499, 16, 15))
        self.assertFalse(c.high_wind.permits(102.5, 16, 15))
        self.assertFalse(c.high_wind.permits(103, 16, 15))
        self.assertFalse(c.high_wind.permits_activation(100, 0, 15))

    def test_cool_step_after_confirmation_waits_for_motor_interval(self):
        c, now = self.confirmed()
        d = self.tick(c, now + 3, 15, cpu=67)
        self.assertEqual(d['level'], 15)
        self.assertEqual(d['control_status']['primary_reason'], 'ceiling_exit_motor_interval')
        self.assertEqual(d['control_status']['next_eligible_at'], 33)
        self.assertFalse(c.high_wind.permits(6, 15, 16))
        self.assertEqual(c._thermal_memory.failures, {})

    def test_forecast_and_old_margin_71_are_not_below80_restoration_proofs(self):
        c, now = self.confirmed(cpu=69)
        for age, cpu in ((3, 69.6), (6, 71)):
            d = self.tick(c, now + age, 15, cpu=cpu)
            self.assertEqual(d['high_wind_limit'], 10)
            self.assertFalse(c.high_wind.permits(now + age, 15, 16))
            self.assertNotIsInstance(c.high_wind.proof, FailedDescentRestoration)
            self.assertEqual(c._thermal_memory.failures, {})

    def test_manual_switch_retains_real_failure_restoration_and_automatic_provenance(self):
        for prior in (16, 11):
            for timing in ('before_arrival', 'on_arrival', 'after_confirmation'):
                with self.subTest(prior=prior, timing=timing):
                    c, _, _ = self.requested(actual=prior, cpu=40, gpu=38, board=46, load=5, power=45)
                    if timing == 'before_arrival':
                        self.tick(c, 3, prior, cpu=40, gpu=38, board=46, load=5, power=45, manual_level=prior - 1)
                    elif timing == 'after_confirmation':
                        self.tick(c, 3, prior - 1, cpu=40, gpu=38, board=46, load=5, power=45)
                    d = self.tick(c, 6, prior - 1, cpu=80, gpu=38, board=46, load=5, power=45,
                        manual_level=prior - 1)
                    self.assertEqual(d['level'], prior)
                    self.assertIsInstance(c.high_wind.proof, FailedDescentRestoration)
                    self.assertTrue(c.high_wind.permits(6, prior - 1, prior))
                    self.assertFalse(c.high_wind.permits(6, prior - 2, prior))
                    self.assertIsNone(c._thermal_memory.open_edge)
                    self.assertTrue(all(row['automatic'] for row in c._thermal_memory.failures.values()))

    def test_real_failure_is_not_attributed_to_increased_load_power_or_replacement(self):
        for kind in ('load', 'power', 'identity'):
            with self.subTest(kind=kind):
                c, _, _ = self.requested(cpu=40, gpu=38, board=46, load=5, power=45)
                self.tick(c, 3, 15, cpu=40, gpu=38, board=46, load=5, power=45)
                d = self.tick(c, 6, 15, cpu=58, gpu=38, board=46,
                    load=30 if kind == 'load' else 5, power=80 if kind == 'power' else 45,
                    mutate=(lambda nodes: nodes[0].update(hostname='replacement')) if kind == 'identity' else None)
                if kind == 'identity':
                    self.assertIsInstance(c.high_wind.proof, SourceFaultProtection)
                else:
                    self.assertFalse(c.high_wind.permits(6, 15, 16))
                self.assertEqual(c._thermal_memory.failures, {})
                self.assertEqual(d['high_wind_limit'], 10)

    def test_cold_publication_revokes_measured80_without_claiming_old_call_cancellation(self):
        c = self.controller()
        self.tick(c, 100, 10, cpu=80)
        old = c.high_wind
        self.assertIsInstance(old.proof, Measured80)
        d = self.tick(c, 103, 10, cpu=79.999)
        self.assertEqual(d['high_wind_limit'], 10)
        self.assertFalse(c.high_wind.permits(103, 10, 16))
        self.assertTrue(old.valid_at(103))

    def test_unresolved_legacy_tag_alone_cannot_stall_below80_exit(self):
        for stage in ('pending', 'confirmed', 'unresolved'):
            c = self.controller()
            payload = self.legacy_payload()
            payload['open_edge']['stage'] = stage
            c.restore_thermal_memory(payload, 1000)
            before = c.export_thermal_memory()
            d = self.tick(c, 1000)
            after = c.export_thermal_memory()
            self.assertEqual(d['level'], 15)
            for field in ('anchors', 'failures', 'open_edge'):
                self.assertEqual(after[field], before[field])
            self.assertEqual(ThermalMemory.restore(after).export(), after)

    def test_real_failed_floor_remains_an_explicit_safety_conflict(self):
        payload = self.legacy_payload()
        key = tuple(payload['open_edge']['sources'][0]['source'])
        m = ThermalMemory.restore(payload)
        m.open_edge = None
        m.add_failure(key, dict(safe_level=16, failed_level=15, baseline=[60, 60, 100],
            automatic=True, failure_class='thermal_failure'))
        c = self.controller()
        c.restore_thermal_memory(m.export(), 100)
        d = self.tick(c, 100)
        self.assertEqual(d['level'], 16)
        self.assertEqual(d['high_wind_limit'], 10)
        self.assertEqual(d['control_status']['primary_reason'], 'ceiling_exit_requalifying')

    def test_interruptions_do_not_overwrite_old_evidence_or_clear_motor_debt(self):
        for method in ('pause_release', 'protect'):
            c = self.controller()
            c.restore_thermal_memory(self.legacy_payload(), 100)
            self.tick(c, 100)
            before = c.export_thermal_memory()
            getattr(c, method)('storage', 101)
            after = c.export_thermal_memory()
            self.assertEqual(after['anchors'], before['anchors'])
            self.assertEqual(after['failures'], before['failures'])
            self.assertEqual(after['open_edge'], before['open_edge'])

    def test_saved_warming_debt_is_preserved_without_paying_for_exit(self):
        payload = self.legacy_payload()
        c = self.controller()
        c.restore_thermal_memory(payload, 100)
        before = c.export_thermal_memory()
        d = self.tick(c, 100)
        self.assertEqual(d['level'], 15)
        self.assertEqual(c.export_thermal_memory()['anchors'], before['anchors'])
        self.assertEqual(c.operating.observe_credit, 0)

    def test_ordinary_edge_retains_target_based_restoration(self):
        controller = self.controller()
        for now in range(0, 244, 3):
            decision = self.tick(controller, now, 10, cpu=40, gpu=38, board=46, load=5, power=45)
            if controller._pending(): break
        self.assertEqual(decision['level'], 9)
        self.tick(controller, now + 3, 9, cpu=40, gpu=38, board=46, load=5, power=45)
        decision = self.tick(controller, now + 6, 9, cpu=58, gpu=38, board=46, load=5, power=45)
        self.assertEqual(decision['level'], 10)
        self.assertTrue(controller.export_thermal_memory()['failures'])

    def test_history_keeps_bounded_basis_specific_fields(self):
        controller, now, decision = self.requested()
        expected = deepcopy(decision['control_status'])
        decision['control_status']['private_field'] = 'not-public'
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append(dict(ts=now, nodes=[], decision=decision))
            public = journal.history(now, now + 1)['samples'][0]['decision']['control_status']
        self.assertEqual(public, expected)

    def test_strict_tagged_and_legacy_decoder_preserve_memory_and_reject_partial_shapes(self):
        payload = self.legacy_payload()
        self.assertEqual(ThermalMemory.restore(payload).export(), payload)
        legacy = deepcopy(payload)
        del legacy['open_edge']['release_basis']
        for row in legacy['open_edge']['sources']: del row['exit_origin_c']
        self.assertEqual(ThermalMemory.restore(legacy).export(), legacy)
        bad_cases = []
        for tag in ('ordinary', 'high_exit_v2', None):
            bad = deepcopy(payload)
            bad['open_edge']['release_basis'] = tag
            bad_cases.append(bad)
        for field, value in (('exit_origin_c', float('nan')), ('exit_origin_c', float('inf')),
                ('exit_origin_c', True), ('source', ['wrong', 'host', 'power', 'sensor', 'cpu'])):
            bad = deepcopy(payload)
            bad['open_edge']['sources'][0][field] = value
            bad_cases.append(bad)
        bad = deepcopy(payload)
        del bad['open_edge']['sources'][0]['exit_origin_c']
        bad_cases.append(bad)
        bad = deepcopy(payload)
        bad['open_edge']['sources'].append(deepcopy(bad['open_edge']['sources'][0]))
        bad_cases.append(bad)
        bad = deepcopy(payload)
        bad['open_edge']['to_level'] = 10
        bad_cases.append(bad)
        bad = deepcopy(payload)
        bad['intent']['to_level'] = 14
        bad_cases.append(bad)
        for bad in bad_cases:
            with self.subTest(edge=bad['open_edge']), self.assertRaises(ValueError):
                ThermalMemory.restore(bad)
        json.dumps(payload, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
