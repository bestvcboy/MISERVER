import json
import math
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import service
import thermal_policy
from core import Controller, HighWindAuthority, Measured80, NoAscent, SourceFaultProtection
from test_fan_snapshot import matched_fan
import test_high_exit_service
from test_scenarios import HOSTS, feedback, fleet
from test_service import FakeFan
from test_thermal_memory_service import closure


class SourceTimingTests(unittest.TestCase):
    def sample(self, offset=0):
        return dict(requested_mono=100, sampled_at=1100, source_ts=1100 + offset)

    def test_signed_uncertainty_edges_and_raw_clocks(self):
        for offset, observed in ((-.5, 99), (-.163, 99.337), (0, 99.5),
                                 (.163, 99.663), (.5, 100)):
            with self.subTest(offset=offset):
                sample = self.sample(offset)
                original = deepcopy(sample)
                timing = thermal_policy.source_timing(sample, 100)
                self.assertIsNotNone(timing)
                self.assertAlmostEqual(timing.observed_mono, observed)
                self.assertAlmostEqual(timing.expires_mono, observed + 6)
                self.assertEqual(sample, original)
        self.assertIsNone(thermal_policy.source_timing(self.sample(.500001), 100))

    def test_fixed_anchor_exact_expiry_and_no_double_elapsed_deduction(self):
        sample = self.sample()
        first = thermal_policy.source_timing(sample, 100)
        later = thermal_policy.source_timing(sample, 103)
        self.assertEqual(first, later)
        self.assertEqual((first.observed_mono, first.expires_mono), (99.5, 105.5))
        with self.assertRaises(FrozenInstanceError):
            first.observed_mono = 103
        self.assertTrue(first.valid_at(105.499999))
        self.assertFalse(first.valid_at(105.5))
        self.assertFalse(first.valid_at(99.499999))
        self.assertIsNotNone(thermal_policy.source_timing(sample, 105.499999))
        self.assertIsNone(thermal_policy.source_timing(sample, 105.5))

    def test_invalid_and_missing_clocks_fail_closed(self):
        for field in ('requested_mono', 'sampled_at', 'source_ts', 'now'):
            for value in (None, True, False, '100', math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value):
                    sample = self.sample()
                    now = 100
                    if field == 'now':
                        now = value
                    else:
                        sample[field] = value
                    self.assertIsNone(thermal_policy.source_timing(sample, now))
            if field != 'now':
                sample = self.sample()
                del sample[field]
                self.assertIsNone(thermal_policy.source_timing(sample, 100))

    def test_request_timeout_and_old_source_spend_the_same_budget(self):
        self.assertIsNone(thermal_policy.source_timing(self.sample(.5), 99.999999))
        self.assertIsNotNone(thermal_policy.source_timing(self.sample(.5), 105.999999))
        self.assertIsNone(thermal_policy.source_timing(self.sample(.5), 106))
        sample = self.sample(-5)
        self.assertIsNotNone(thermal_policy.source_timing(sample, 100.499999))
        self.assertIsNone(thermal_policy.source_timing(sample, 100.5))


class CrossHostTimeRegressionTests(unittest.TestCase):
    def test_live_sized_clock_skew_does_not_block_adjacent_ceiling_exit(self):
        nodes = fleet(100, cpu=50, gpu=45, board=54, load=5, power=45)
        nodes[2]['source_ts'] += .163
        raw_times = [(n['source_ts'], n['sampled_at'], n['requested_mono']) for n in nodes]
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        decision = controller.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 16))
        self.assertTrue(controller.thermal_view.complete)
        self.assertNotIsInstance(controller.high_wind.proof, SourceFaultProtection)
        self.assertEqual(decision['normal_ceiling'], 10)
        self.assertLessEqual(decision['demand'], 10)
        self.assertEqual(decision['level'], 15)
        self.assertEqual(raw_times, [(n['source_ts'], n['sampled_at'], n['requested_mono']) for n in nodes])

    def test_live_sized_clock_skew_does_not_erase_actual_hot_evidence(self):
        nodes = fleet(100, cpu=50, gpu=45, board=54, load=5, power=45)
        nodes[2]['source_ts'] += .163
        nodes[2]['sensors'][0]['temp'] = 80
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        decision = controller.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
        self.assertTrue(controller.thermal_view.complete)
        self.assertIsInstance(controller.high_wind.proof, Measured80)
        self.assertEqual(decision['level'], 11)
        self.assertLessEqual(controller.high_wind.observed_mono, 100)
        self.assertFalse(controller.high_wind.valid_at(106))

    def test_ordinary_and_enhanced_admission_use_identical_edges(self):
        for gate in (False, True):
            for offset, valid in ((-.5, True), (.163, True), (.5, True),
                                  (.500001, False), (-5.5, False)):
                with self.subTest(gate=gate, offset=offset):
                    nodes = fleet(100, cpu=80)
                    for node in nodes:
                        node['source_ts'] += offset
                    controller = Controller(HOSTS, rules_only=True, high_wind_gate=gate)
                    controller.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
                    self.assertEqual(bool(controller.sources), valid)
                    if gate:
                        self.assertIs(controller.thermal_view.complete, valid)

    def test_backward_and_repeat_never_lower_watermarks_then_recover(self):
        for gate in (False, True):
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=gate)
            for now, source, accepted in ((100, 100, True), (103, 102, True),
                    (104, 101, False), (105, 102, False), (106, 106, True)):
                with self.subTest(gate=gate, now=now):
                    nodes = fleet(now, cpu=80)
                    for node in nodes:
                        node['source_ts'] = source
                    decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 10))
                    watermark = source if accepted else 102
                    self.assertEqual(set(controller.sources.values()), {watermark})
                    if gate:
                        self.assertEqual(set(controller._admission_sources.values()), {watermark})
                        self.assertIs(controller.thermal_view.complete, accepted)
                        if not accepted:
                            self.assertIsInstance(controller.high_wind.proof, SourceFaultProtection)
                            self.assertEqual(decision['normal_ceiling'], 10)

    def test_repeated_fault_does_not_renew_diagnosis(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in (100, 103, 106):
            nodes = fleet(now)
            for node in nodes:
                node['source_ts'] = 110
            controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 16))
            self.assertEqual({stamp for key, fault, stamp in controller.thermal_view.faults}, {100})
        self.assertFalse(controller.high_wind.valid_at(106))

    def test_invalid_clocks_refuse_both_controller_paths_without_substitution(self):
        for gate in (False, True):
            for field in ('requested_mono', 'sampled_at', 'source_ts'):
                for value in (None, True, '100', math.nan, math.inf):
                    with self.subTest(gate=gate, field=field, value=value):
                        nodes = fleet(100, cpu=80)
                        for node in nodes:
                            node[field] = value
                        controller = Controller(HOSTS, rules_only=True, high_wind_gate=gate)
                        controller.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
                        self.assertEqual(controller.sources, {})
                        if gate:
                            self.assertFalse(controller.thermal_view.complete)
                            self.assertIsInstance(controller.high_wind.proof, SourceFaultProtection)
                            self.assertIsNone(controller.high_wind.actual_c)

    def test_safe_exit_builder_keeps_fixed_anchor_and_refuses_expired_evidence(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        controller.operating.confirmed_level = 16
        nodes = fleet(100, cpu=40)
        for node in nodes:
            node['source_ts'] -= 1
        evidence = {(n['host'], s['id'], s['group']): dict(temp=s['temp'], target=60)
            for n in nodes for s in n['sensors'] if s['calibrated']}
        for now in (100, 103, 104.499999, 104.5):
            with self.subTest(now=now):
                controller.high_wind = HighWindAuthority()
                frame = dict(nodes=nodes, now=now, evidence=evidence, feedback=feedback(now, 16),
                    telemetry_fault=False, wifi_gap=False, uncertain=False)
                controller._safe_exit_authority(frame, 16, destination=15, deadline=160)
                if now < 104.5:
                    self.assertIsInstance(controller.high_wind.proof, NoAscent)
                    self.assertEqual(controller.high_wind.observed_mono, 98.5)
                    self.assertEqual(controller.high_wind.expires_mono, 104.5)
                    self.assertTrue(controller.high_wind.valid_at(now))
                else:
                    self.assertFalse(controller.high_wind.allowed)

    def test_hot_identity_is_independent_of_other_faults_under_skew(self):
        for reverse in (False, True):
            nodes = fleet(100, cpu=50)
            nodes[0]['source_ts'] += .163
            nodes[0]['sensors'][0]['temp'] = 80
            nodes[1]['source_ts'] += .500001
            if reverse:
                nodes.reverse()
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            controller.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
            self.assertFalse(controller.thermal_view.complete)
            self.assertIsInstance(controller.high_wind.proof, Measured80)
            self.assertEqual(controller.high_wind.witness[0], HOSTS[0])
            self.assertAlmostEqual(controller.high_wind.observed_mono, 99.663)

    def test_bounded_skew_preserves_threshold_and_original_deadline(self):
        for cpu, target in ((79.999, 10), (80, 11)):
            nodes = fleet(100, cpu=cpu)
            for node in nodes:
                node['source_ts'] += .163
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            decision = controller.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
            self.assertEqual(decision['level'], target)
            if cpu == 80:
                deadline = decision['execution_trial']['original_deadline']
                for node in nodes:
                    node['source_ts'] += 3
                    node['sampled_at'] += 3
                    node['requested_mono'] += 3
                again = controller.decide(nodes, 103, minimum_level=0, feedback=feedback(103, 10))
                self.assertEqual(again['execution_trial']['original_deadline'], deadline)


class CrossHostTimeServiceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_high_exit_service.HighExitServiceTests.asyncSetUp
    asyncTearDown = test_high_exit_service.HighExitServiceTests.asyncTearDown

    async def poll_sample(self, duration, offset):
        owner = self
        poll = closure(closure(self.app.on_startup[-1])['monitor'])['poll']
        class Response:
            status = 200
            async def __aenter__(self):
                self.content = self
                return self
            async def __aexit__(self, *args):
                pass
            async def iter_chunked(self, size):
                owner.now = 100 + duration / 2
                yield b' '
                owner.now = 100 + duration
                yield json.dumps(dict(hostname='DGX', timestamp=1100 + duration + offset,
                    cpu_percent=5, gpu_percent=5, system_power_w=45, system_power_source='spbm',
                    temperatures=dict(gpu=38, acpi=[dict(zone='0', type='acpitz', temp=40)],
                        mlx=[dict(label='asic', temp=46)]))).encode()
        class Session:
            def get(self, *args, **kwargs):
                return Response()
        self.now = 100
        return await poll(Session(), service.HOSTS[0], 1100)

    async def test_body_completion_uses_receipt_wall_but_original_request_deadline(self):
        for duration, offset, valid in ((.4, .163, True), (.4, -.5, True),
                (.4, .5, True), (.4, .500001, False), (6, .163, False), (7, .5, False)):
            with self.subTest(duration=duration, offset=offset):
                sample = await self.poll_sample(duration, offset)
                self.assertEqual(sample['requested_mono'], 100)
                self.assertEqual(sample['sampled_at'], 1100 + duration)
                self.assertEqual(sample['source_ts'], 1100 + duration + offset)
                controller = Controller([service.HOSTS[0]], rules_only=True, high_wind_gate=True)
                controller.decide([sample], self.now, minimum_level=0, feedback=feedback(self.now, 16))
                self.assertIs(controller.thermal_view.complete, valid)
                self.assertEqual(thermal_policy.source_timing(sample, self.now) is not None, valid)

    async def test_uncertainty_expiry_at_motor_reservation_refuses_write(self):
        nodes = fleet(100, cpu=80)
        for node in nodes:
            node['source_ts'] += .163
        self.now = 100
        decision = self.controller.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10))
        self.assertIsInstance(self.controller.high_wind.proof, Measured80)
        expiry = self.controller.high_wind.expires_mono
        self.assertAlmostEqual(expiry, 105.663)
        self.state.update(decision=decision, nodes=nodes, fan=matched_fan(10, 100, 1100),
            last_poll=100, last_fan=100, fan_request=service.FanRequest(decision['level'],
                high_wind=self.controller.high_wind, motion=self.controller.motion))
        await self.memory.flush(self.controller)
        schedule = self.state['motor_schedule']
        reserve = schedule.persist_debt_then_set_budget
        def expire(*args, **kwargs):
            result = reserve(*args, **kwargs)
            self.now = expiry
            return result
        device = FakeFan(['on', 'favorite', 10, 1500])
        with patch.object(schedule, 'persist_debt_then_set_budget', side_effect=expire):
            result = await self.apply(device)
        self.assertEqual(device.writes, [])
        self.assertEqual(schedule.attempts, 1)
        self.assertEqual(result['error'], 'actuation_snapshot_changed_after_budget')


if __name__ == '__main__':
    unittest.main()
