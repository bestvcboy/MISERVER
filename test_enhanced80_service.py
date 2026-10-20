import asyncio
import threading
import json
from dataclasses import replace
from unittest.mock import patch

import service
from core import SourceFaultProtection
from test_fan_snapshot import matched_fan
from test_high_exit_service import HighExitServiceTests
from test_scenarios import feedback, fleet
from test_service import FakeFan
from test_thermal_memory_service import closure


class Ceiling80ServiceTests(HighExitServiceTests):
    async def test_poll_accepts_response_generated_source_without_renewing_request_age(self):
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
                owner.now = 100.4
                yield json.dumps(dict(hostname='DGX', timestamp=1100.2, cpu_percent=5, gpu_percent=5,
                    system_power_w=45, system_power_source='spbm', temperatures=dict(gpu=38,
                        acpi=[dict(zone='0', type='acpitz', temp=40)], mlx=[dict(label='asic', temp=46)]))).encode()
        class Session:
            def get(self, *args, **kwargs):
                return Response()
        self.now = 100
        node = await poll(Session(), service.HOSTS[0], 1100)
        self.assertEqual(node['requested_mono'], 100)
        self.assertEqual(node['sampled_at'], 1100.4)
        self.assertEqual(node['source_ts'], 1100.2)
        for label, sample, now, complete in (
                ('received', node, 100.4, True),
                ('future', dict(node, source_ts=1100.900001), 100.4, False),
                ('request_ttl', node, 106, False),
                ('source_ttl', dict(node, source_ts=1094.4), 100.4, False)):
            with self.subTest(label=label):
                controller = service.Controller([service.HOSTS[0]], rules_only=True, high_wind_gate=True)
                controller._validate_admission([sample], now)
                self.assertIs(controller.thermal_view.complete, complete)

    async def test_real_second_attempt_uses_same_deadline_and_keeps_durable_fault_debt(self):
        for actual, cpu, target in ((16, 65, 15), (10, 80, 11), (8, 40, 10)):
            with self.subTest(actual=actual, cpu=cpu):
                self.controller = service.Controller(service.HOSTS, rules_only=True, high_wind_gate=True)
                self.state.update(controller=self.controller, motor_schedule=service.MotorSchedule())
                await self.frame(100, actual, cpu)
                schedule = self.state['motor_schedule']
                schedule.budget_path = self.memory.store.path.parent / f'retry-{actual}.json'
                device = FakeFan(['on', 'favorite', actual, 1500])
                device.ignore_writes = True
                await self.apply(device)
                self.assertEqual((device.writes, schedule.attempts), ([target], 1))
                await self.frame(130, actual, cpu)
                self.assertEqual(self.snapshot().snapshot.original_deadline, 160)
                device.ignore_writes = False
                set_level = device.set_favorite_level
                def inspect_reserved_debt(level):
                    debt = json.loads(schedule.budget_path.read_text())
                    self.assertEqual((debt['attempts'], debt['expected_level'], debt['fault']), (2, target, True))
                    self.assertEqual((schedule.attempts, schedule.fault), (2, True))
                    return set_level(level)
                with patch.object(device, 'set_favorite_level', side_effect=inspect_reserved_debt):
                    result = await self.apply(device)
                self.assertEqual(device.writes, [target, target])
                self.assertTrue(result['confirmed'])
                self.assertEqual(result['level'], target)
                self.assertTrue(result['protection_interrupted'])

    async def test_explicit_compatibility_retains_storage_memory_deadline_and_debt_checks(self):
        await self.frame(100)
        original = self.snapshot().snapshot
        for revoke in ('none', 'storage', 'memory', 'deadline', 'reservation', 'reserved_level'):
            with self.subTest(revoke=revoke):
                snapshot = replace(original, proof=None)
                if revoke == 'storage':
                    snapshot = replace(snapshot, blocker='storage_protect')
                elif revoke == 'memory':
                    snapshot = replace(snapshot, acknowledged_digest=None)
                elif revoke == 'deadline':
                    snapshot = replace(snapshot, original_deadline=100)
                request = service.FanRequest(15, snapshot=snapshot)
                schedule = service.MotorSchedule()
                persist = schedule.persist_debt_then_set_budget
                def reserve(*args, **kwargs):
                    saved = persist(*args, **kwargs)
                    if revoke == 'reservation':
                        schedule.last_attempt = 99
                    elif revoke == 'reserved_level':
                        schedule.expected_level = 14
                    return saved
                device = FakeFan(['on', 'favorite', 16, 1500])
                with patch.object(schedule, 'persist_debt_then_set_budget', side_effect=reserve) as reservation:
                    service.fan_apply(device, lambda **kwargs: request, schedule, high_wind_gate=False)
                self.assertEqual(device.writes, [15] if revoke == 'none' else [])
                if revoke == 'storage':
                    reservation.assert_not_called()
                    self.assertEqual(schedule.attempts, 0)
                    self.assertEqual(schedule.last_attempt, -float('inf'))
                elif revoke != 'none':
                    self.assertEqual(schedule.attempts, 1)

    async def test_measured80_can_activate_an_off_device_without_claiming_running_rpm(self):
        self.now = 100
        d = self.controller.decide(fleet(100, cpu=80), 100, minimum_level=0,
            feedback=feedback(100, 0, power='off', rpm=0))
        observed = dict(matched_fan(0, 100, 1100), power='off', rpm=0)
        self.state.update(decision=d, fan=observed, last_poll=100, last_fan=100,
            fan_request=service.FanRequest(d['level'], high_wind=self.controller.high_wind,
                motion=self.controller.motion))
        await self.memory.flush(self.controller)
        device = FakeFan(['off', 'favorite', 0, 0])
        result = await self.apply(device)
        self.assertEqual(device.writes, ['on', 10])
        self.assertFalse(result['confirmed'])

    async def test_second_reservation_external_revocations_never_write_or_refund(self):
        for revoke in ('memory', 'settings', 'storage', 'generation'):
            with self.subTest(revoke=revoke):
                self.controller = service.Controller(service.HOSTS, rules_only=True, high_wind_gate=True)
                self.state.update(controller=self.controller, motor_schedule=service.MotorSchedule(), storage_error='')
                await self.frame(100)
                device = FakeFan(['on', 'favorite', 16, 1500])
                device.ignore_writes = True
                await self.apply(device)
                await self.frame(130)
                schedule = self.state['motor_schedule']
                reserve = schedule.persist_debt_then_set_budget
                def change(*args, **kwargs):
                    result = reserve(*args, **kwargs)
                    if revoke == 'memory':
                        self.memory.invalidate()
                    elif revoke == 'settings':
                        self.state['settings_revision'] += 1
                    elif revoke == 'storage':
                        self.state['storage_error'] = 'failed'
                    else:
                        self.controller.thermal_view = replace(self.controller.thermal_view,
                            generation=self.controller.thermal_view.generation + 1)
                    return result
                with patch.object(schedule, 'persist_debt_then_set_budget', side_effect=change):
                    await self.apply(device)
                self.assertEqual(device.writes, [15])
                self.assertEqual((schedule.attempts, schedule.fault), (2, True))
                self.now = 133
                await self.apply(device)
                self.assertEqual(device.writes, [15])

    async def test_source_fault_cannot_write_from_running_stalled_or_invalid_endpoint(self):
        for rpm, age in ((0, 0), (-1, 0), (float('nan'), 0), (1100, 6)):
            with self.subTest(rpm=rpm, age=age):
                self.controller = service.Controller(service.HOSTS, rules_only=True, high_wind_gate=True)
                self.state.update(controller=self.controller, motor_schedule=service.MotorSchedule())
                self.now = 100
                nodes = fleet(100)
                nodes[0]['hostname_verified'] = False
                d = self.controller.decide(nodes, 100, minimum_level=0, feedback=feedback(100, 10, rpm=rpm))
                self.state.update(decision=d, fan=dict(matched_fan(10, 100, 1100), rpm=rpm),
                    last_poll=100, last_fan=100, fan_request=service.FanRequest(d['level'],
                        high_wind=self.controller.high_wind, motion=self.controller.motion))
                self.state['fan']['observed_mono'] -= age
                await self.memory.flush(self.controller)
                device = FakeFan(['on', 'favorite', 10, rpm])
                await self.apply(device)
                self.assertEqual(device.writes, [])

    async def test_canceled_publication_waiter_cannot_strand_submission_lock(self):
        boundary = self.state['admission']
        boundary.lock.acquire()
        entered = []
        async def publish():
            async with boundary.publication():
                entered.append(True)
        waiting = asyncio.create_task(publish())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        boundary.lock.release()
        await asyncio.wait_for(publish(), 2)
        self.assertEqual(entered, [True])
        self.assertFalse(boundary.lock.locked())

    async def frame(self, now, actual=16, cpu=65):
        self.now = now
        d = self.controller.decide(fleet(now, cpu=cpu, load=60, power=100), now,
            minimum_level=0, feedback=feedback(now, actual))
        self.state.update(decision=d, fan=matched_fan(actual, now, 1000 + now),
            last_poll=now, last_fan=now, fan_request=service.FanRequest(d['level'],
                high_wind=self.controller.high_wind, motion=self.controller.motion))
        await self.memory.flush(self.controller)

    async def test_exact_eight_thirty_and_two_attempt_boundaries(self):
        for attempt_age, descent_age, attempts, writes in (
                (7.999, 40, 0, []), (8, 40, 0, [15]),
                (40, 29.999, 0, []), (40, 30, 0, [15]), (40, 40, 2, [])):
            with self.subTest(attempt_age=attempt_age, descent_age=descent_age, attempts=attempts):
                self.controller = service.Controller(service.HOSTS, rules_only=True, high_wind_gate=True)
                self.state['controller'] = self.controller
                await self.frame(100)
                schedule = service.MotorSchedule()
                schedule.last_attempt, schedule.last_decrease = 100 - attempt_age, 100 - descent_age
                schedule.attempts, schedule.expected_level = attempts, 15 if attempts else None
                self.state['motor_schedule'] = schedule
                device = FakeFan(['on', 'favorite', 16, 2096])
                await self.apply(device)
                self.assertEqual(device.writes, writes)
                self.assertLessEqual(schedule.attempts, 2)

    async def test_budget_persistence_is_outside_admission_lock(self):
        await self.frame(100)
        schedule = self.state['motor_schedule']
        reserve = schedule.persist_debt_then_set_budget
        lock = self.state['admission'].lock
        def checked(*args, **kwargs):
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()
            return reserve(*args, **kwargs)
        device = FakeFan(['on', 'favorite', 16, 2096])
        with patch.object(schedule, 'persist_debt_then_set_budget', side_effect=checked):
            await self.apply(device)
        self.assertEqual(device.writes, [15])

    async def test_original_deadline_at_sdk_entry_cannot_be_renewed(self):
        self.qualify()
        await self.frame(57)
        owner = self
        class DeadlineFan(FakeFan):
            @property
            def set_favorite_level(self):
                owner.now = 60
                return super().set_favorite_level
        device = DeadlineFan(['on', 'favorite', 16, 2096])
        result = await self.apply(device)
        self.assertEqual(device.writes, [])
        self.assertEqual(result['submission']['reason'], 'original_deadline_expired')
        self.assertEqual(self.state['motor_schedule'].attempts, 1)

    async def test_old_ack_cannot_authorize_new_intent_even_if_gate_claims_ready(self):
        await self.frame(100)
        old_digest = self.memory.acknowledged_digest
        self.controller._thermal_memory.revision += 1
        self.memory.desired_digest = old_digest
        self.assertTrue(self.memory.ready)
        self.assertEqual(self.snapshot().snapshot.blocker, 'current_memory_not_acknowledged')
        device = FakeFan(['on', 'favorite', 16, 2096])
        await self.apply(device)
        self.assertEqual(device.writes, [])

    async def test_current_failed_descent_restoration_is_exact_and_expires(self):
        await self.frame(0, cpu=40)
        await self.frame(3, actual=15, cpu=40)
        await self.frame(6, actual=15, cpu=80)
        authority = self.controller.high_wind
        self.assertEqual(self.snapshot().level, 16)
        device = FakeFan(['on', 'favorite', 15, 2096])
        result = await self.apply(device)
        self.assertEqual(device.writes, [16])
        self.assertEqual(result['submission']['state'], 'Submitted')
        self.assertFalse(authority.permits(6, 14, 16))
        self.assertFalse(authority.valid_at(12))

    async def test_final_budget_revocations_keep_reservation_without_any_write(self):
        for revoke in ('memory', 'settings', 'storage', 'endpoint', 'generation'):
            with self.subTest(revoke=revoke):
                self.now += 100
                self.controller = service.Controller(service.HOSTS, rules_only=True, high_wind_gate=True)
                self.state['controller'] = self.controller
                self.state['storage_error'] = ''
                self.state['motor_schedule'] = service.MotorSchedule()
                self.qualify()
                await self.memory.flush(self.controller)
                schedule = self.state['motor_schedule']
                reserve = schedule.persist_debt_then_set_budget
                def change(*args, **kwargs):
                    result = reserve(*args, **kwargs)
                    if revoke == 'memory':
                        self.memory.invalidate()
                    elif revoke == 'settings':
                        self.state['settings_revision'] += 1
                    elif revoke == 'storage':
                        self.state['storage_error'] = 'failed'
                    elif revoke == 'endpoint':
                        self.state['fan'] = matched_fan(14, self.now, 1000 + self.now)
                    else:
                        self.controller.thermal_view = replace(self.controller.thermal_view,
                            generation=self.controller.thermal_view.generation + 1)
                    return result
                device = FakeFan(['on', 'favorite', 16, 2096])
                with patch.object(schedule, 'persist_debt_then_set_budget', side_effect=change):
                    await self.apply(device)
                self.assertEqual(device.writes, [])
                self.assertEqual((schedule.attempts, schedule.expected_level), (1, 15))

    async def test_revocation_after_final_loop_equality_before_method_lookup(self):
        self.qualify()
        await self.memory.flush(self.controller)
        memory = self.memory
        class EntryFan(FakeFan):
            @property
            def set_favorite_level(self):
                memory.invalidate()
                return super().set_favorite_level
        device = EntryFan(['on', 'favorite', 16, 2096])
        result = await self.apply(device)
        self.assertEqual(device.writes, [])
        self.assertEqual(result['submission']['state'], 'Refused')
        self.assertEqual(self.state['motor_schedule'].attempts, 1)

    async def test_proof_expiry_at_actual_sdk_entry(self):
        self.qualify()
        await self.memory.flush(self.controller)
        owner = self
        class ExpiringFan(FakeFan):
            @property
            def set_favorite_level(self):
                owner.now += 6
                return super().set_favorite_level
        device = ExpiringFan(['on', 'favorite', 16, 2096])
        await self.apply(device)
        self.assertEqual(device.writes, [])
        self.assertEqual(self.state['motor_schedule'].attempts, 1)

    async def test_publication_during_sdk_is_ordered_after_submit_without_blocking_loop(self):
        self.now = 100
        d = self.controller.decide(fleet(self.now, cpu=80), self.now,
            minimum_level=0, feedback=feedback(self.now, 10))
        self.state.update(decision=d, fan=matched_fan(10, self.now, 1100),
            last_poll=self.now, last_fan=self.now,
            fan_request=service.FanRequest(d['level'], high_wind=self.controller.high_wind,
                motion=self.controller.motion))
        await self.memory.flush(self.controller)
        entered, finish = threading.Event(), threading.Event()
        class BlockingFan(FakeFan):
            def set_favorite_level(self, value):
                entered.set()
                if not finish.wait(2):
                    raise TimeoutError()
                return super().set_favorite_level(value)
        device = BlockingFan(['on', 'favorite', 10, 900])
        applying = asyncio.create_task(self.apply(device))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        published = []
        async def cold():
            async with self.state['admission'].publication():
                published.append(True)
                self.now = 103
                self.controller.decide(fleet(self.now, cpu=79), self.now,
                    minimum_level=0, feedback=feedback(self.now, 10))
        publishing = asyncio.create_task(cold())
        try:
            await asyncio.sleep(0)
            self.assertFalse(published)
            self.assertFalse(publishing.done())
        finally:
            finish.set()
        result = await applying
        await publishing
        self.assertEqual(device.writes, [11])
        self.assertEqual(result['submission']['state'], 'Submitted')
        self.assertEqual(self.controller.effective_limit(self.now), 10)
        self.assertEqual(len(self.state['admission'].dispatches), 1)

    async def test_sdk_timeout_is_uncertain_and_does_not_refund_debt(self):
        self.qualify()
        await self.memory.flush(self.controller)
        class UncertainFan(FakeFan):
            def set_favorite_level(self, level):
                super().set_favorite_level(level)
                raise TimeoutError()
        device = UncertainFan(['on', 'favorite', 16, 2096])
        result = await self.apply(device)
        self.assertEqual(device.writes, [15])
        self.assertEqual(result['submission']['state'], 'SubmissionUncertain')
        self.assertFalse(result['confirmed'])
        self.assertEqual(self.state['motor_schedule'].attempts, 1)

    async def test_full_exit_manual_and_automatic_uses_real_feedback(self):
        for manual in (None, 16):
            with self.subTest(manual=manual):
                self.controller = service.Controller(service.HOSTS, rules_only=True, high_wind_gate=True)
                self.state['controller'] = self.controller
                self.state['settings']['manual_level'] = manual
                self.state['motor_schedule'] = service.MotorSchedule()
                device = FakeFan(['on', 'favorite', 16, 2096])
                actual = 16
                for self.now in range(0, 211, 3):
                    d = self.controller.decide(fleet(self.now, cpu=79.999, gpu=65, board=65,
                        load=50 if self.now % 2 else 90, power=100 if self.now % 2 else 140),
                        self.now, minimum_level=manual or 0, manual_level=manual,
                        feedback=feedback(self.now, actual))
                    self.state.update(decision=d, fan=matched_fan(actual, self.now, 1000 + self.now),
                        last_poll=self.now, last_fan=self.now, fan_request=service.FanRequest(
                            d['level'], high_wind=self.controller.high_wind, motion=self.controller.motion))
                    await self.memory.flush(self.controller)
                    result = await self.apply(device)
                    actual = result['level']
                    if actual == 10:
                        break
                self.assertEqual(device.writes, [15, 14, 13, 12, 11, 10])
                self.assertIsNone(self.controller._thermal_memory.open_edge)
