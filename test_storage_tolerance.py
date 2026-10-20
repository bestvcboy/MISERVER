import asyncio
import errno
from contextlib import ExitStack, asynccontextmanager
from copy import deepcopy
from pathlib import Path
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import service


class StorageHealthTests(unittest.TestCase):
    def episode(self):
        health = service.StorageHealth()
        health.failure(100, 1000, TimeoutError('private/path/token'), 'prune', False, hold_eligible=True)
        return health

    def test_soft_deadline_never_renews(self):
        health = self.episode()
        self.assertEqual(health.refresh(108.999, hold_eligible=True), 'holding')
        health.success(103, hold_eligible=True, verified_revision=0)
        health.failure(106, 1006, OSError(errno.EAGAIN, 'private'), 'prune', False, hold_eligible=True)
        self.assertEqual(health.first_failure, 100)
        self.assertEqual(health.refresh(109, hold_eligible=True), 'protect')
        self.assertNotIn('private', str(health.last_error))

    def test_third_failure_hard_errors_and_unknown_actual(self):
        health = self.episode()
        health.failure(103, 1003, TimeoutError(), 'prune', False, hold_eligible=True)
        self.assertEqual(health.phase, 'holding')
        health.failure(106, 1006, TimeoutError(), 'prune', False, hold_eligible=True)
        self.assertEqual(health.phase, 'protect')
        for error in (OSError(errno.ENOSPC, 'disk'), PermissionError(), ValueError(), RuntimeError()):
            health = service.StorageHealth()
            health.failure(100, 1000, error, 'prune', False, hold_eligible=True)
            self.assertEqual(health.phase, 'protect')
        self.assertEqual(self.episode().refresh(101, hold_eligible=False), 'protect')

    def test_hard_fault_requires_two_new_qualified_successes(self):
        health = service.StorageHealth()
        health.failure(100, 1000, OSError(errno.ENOSPC, 'disk'), 'prune', False, hold_eligible=True)
        health.success(103, hold_eligible=True, verified_revision=0)
        self.assertEqual(health.phase, 'protect')
        health.success(106, hold_eligible=True, verified_revision=0)
        self.assertEqual(health.phase, 'healthy')
        self.assertEqual(health.last_error['errno'], errno.ENOSPC)

    def test_explicit_temporary_errnos_before_append_hold(self):
        for code in (errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ETIMEDOUT):
            health = service.StorageHealth()
            health.failure(100, 1000, OSError(code, 'private'), 'prune', False, hold_eligible=True)
            self.assertEqual(health.phase, 'holding')

    def test_unknown_append_and_postappend_are_hard(self):
        for phase, committed in (("append", None), ("accounting", True)):
            health = service.StorageHealth()
            health.failure(100, 1000, TimeoutError(), phase, committed, hold_eligible=True)
            self.assertEqual(health.phase, 'protect')
            self.assertEqual(health.last_error['committed'], committed)

    def test_pause_release_does_not_cancel_critical_ascent(self):
        from core import Controller
        from test_core import HOSTS
        from test_scenarios import fleet, feedback
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        decision = controller.decide(fleet(100, cpu=75), 100, minimum_level=0, feedback=feedback(100, 7))
        self.assertEqual((decision['level'], decision['normal_ceiling']), (10, 10))
        self.assertEqual(controller.thermal_view.maximum_c, 75)
        self.assertFalse(controller.high_wind.valid_at(100))
        self.assertEqual((decision['execution_trial']['from'], decision['execution_trial']['to'],
            decision['execution_trial']['status']), (7, 10, 'pending'))
        authority = controller.motion
        paused = controller.pause_release('storage hold', 100)
        self.assertEqual(paused['level'], decision['level'])
        self.assertEqual(paused['execution_trial'], decision['execution_trial'])
        self.assertEqual(paused['execution_trial']['status'], 'pending')
        self.assertEqual(controller.motion, authority)
        self.assertIs(controller.motion, authority)

    def test_two_qualified_successes_recover_without_erasing_diagnostic(self):
        health = self.episode()
        health.success(103, hold_eligible=True, verified_revision=0)
        self.assertEqual(health.phase, 'holding')
        health.success(105.999, hold_eligible=True, verified_revision=0)
        self.assertEqual(health.phase, 'holding')
        health.success(106, hold_eligible=True, verified_revision=0)
        self.assertEqual(health.phase, 'healthy')
        self.assertEqual(health.last_error['category'], 'TimeoutError')

    def test_recovery_is_independent_of_hold_and_revision_zero_is_valid(self):
        health = self.episode()
        health.success(103, hold_eligible=False, verified_revision=0)
        self.assertEqual((health.phase, health.recovery_successes, health.recovery_revision), ('protect', 1, 0))
        health.success(105.999, hold_eligible=False, verified_revision=0)
        self.assertEqual((health.first_success, health.recovery_successes), (103, 1))
        health.success(106, hold_eligible=False, verified_revision=0)
        self.assertEqual((health.phase, health.recovery_successes, health.failure_epoch), ('healthy', 2, 1))
        self.assertIsNone(health.recovery_revision)

    def test_unverified_cycle_and_revision_change_break_the_pair(self):
        health = self.episode()
        health.success(103, hold_eligible=False, verified_revision=0)
        health.success(106, hold_eligible=False, verified_revision=None)
        self.assertEqual((health.first_success, health.recovery_successes, health.recovery_revision), (None, 0, None))
        health.success(109, hold_eligible=False, verified_revision=0)
        health.success(112, hold_eligible=False, verified_revision=1)
        self.assertEqual((health.phase, health.first_success, health.recovery_successes), ('protect', 112, 1))
        health.success(115, hold_eligible=False, verified_revision=1)
        self.assertEqual(health.phase, 'healthy')

    def test_every_failure_advances_epoch_and_resets_credit(self):
        health = self.episode()
        for now in (103, 106, 109):
            health.success(now, hold_eligible=False, verified_revision=0)
            health.failure(now + 1, 1000 + now, RuntimeError('private'), 'queue', False, hold_eligible=False)
            self.assertEqual(health.failure_epoch, 1 + (now - 100) // 3)
            self.assertEqual((health.first_success, health.recovery_successes, health.recovery_revision), (None, 0, None))
            self.assertEqual(health.first_failure, 100)
        health.failure(113, 1113, RuntimeError(), 'settings_maintenance', None, hold_eligible=False)
        self.assertEqual((health.failure_epoch, health.consecutive_failures), (5, 2))


class StorageServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_storage_cycles_recover_with_known_high_endpoint_and_unmatched_target(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from test_scenarios import fleet, feedback
        from test_fan_snapshot import matched_fan
        clock = [54]
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'time',
                SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0])), \
                patch.object(service, 'fsync_directory', return_value=None), \
                patch.object(service.MotorSchedule, 'load', return_value=service.MotorSchedule()):
            app = await service.create_app(Path(directory), high_wind_gate=True)
            state = app[service.STATE]
            state['settings']['manual_level'] = None
            try:
                controller = state['controller']
                for now in range(0, 55, 3):
                    decision = controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=5),
                        now, minimum_level=0, feedback=feedback(now, 16, confirmed=False, execution='pending'))
                state['decision'] = decision
                startup = app.on_startup[-1]
                loops = dict(zip(startup.__code__.co_freevars, (c.cell_contents for c in startup.__closure__)))
                async def row(now, error=None):
                    clock[0] = now
                    actual = matched_fan(16, now, 1000 + now)
                    actual.update(confirmed=False, execution='pending')
                    state.update(last_poll=now, last_fan=now, fan=actual, fan_request=service.FanRequest(10))
                    if now > 54:
                        controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=5),
                            now, minimum_level=0, feedback=feedback(now, 16, confirmed=False, execution='pending'))
                    state['queue'].put_nowait(dict(ts=1000 + now, nodes=[]))
                    state['stop'] = True
                    if error:
                        with patch.object(state['journal'], 'prune', side_effect=error):
                            await loops['storage']()
                    else:
                        await loops['storage']()
                await row(54, TimeoutError('prune'))
                self.assertEqual(state['storage_health'].phase, 'protect')
                await row(57)
                self.assertEqual(state['storage_health'].phase, 'protect')
                self.assertEqual(state['storage_health'].recovery_successes, 1)
                await row(60)
                self.assertEqual(state['storage_health'].phase, 'healthy')
                self.assertFalse(state['fan']['confirmed'])
                self.assertEqual(state['fan']['level'], 16)
                self.assertEqual(state['fan']['execution'], 'pending')
                self.assertEqual(state['motor_schedule'].attempts, 0)
                self.assertIsNone(state['device'])
                self.assertEqual((state['history_recording']['writes'], state['history_recording']['failed_writes']), (2, 1))
            finally:
                state['stop'] = True
                try:
                    await app.on_cleanup[-1](app)
                finally:
                    state['disk'].shutdown(wait=True)
                    state['history_disk'].shutdown(wait=True)

    @staticmethod
    def episode_for_test():
        health = service.StorageHealth()
        health.failure(54, 1054, TimeoutError(), 'prune', False, hold_eligible=True)
        return health

    async def test_protection_retires_only_completed_upward_association(self):
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from test_scenarios import fleet, feedback
        from test_fan_snapshot import matched_fan
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'time',
                SimpleNamespace(monotonic=lambda: 12, time=lambda: 1012)), \
                patch.object(service.MotorSchedule, 'load', return_value=service.MotorSchedule()):
            app = await service.create_app(Path(directory), high_wind_gate=True)
            state = app[service.STATE]
            state['settings']['manual_level'] = None
            try:
                controller = state['controller']
                actual = 2
                for now in (0, 3, 6, 9):
                    decision = controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=35),
                        now, minimum_level=0, feedback=feedback(now, actual))
                    actual = decision['level']
                self.assertEqual(actual, 4)
                decision = controller.protect('hard protection', 10)
                state.update(decision=decision, fan_request=service.FanRequest(6), fan=matched_fan(6, 12, 1012),
                    last_poll=12, last_fan=12)
                handler = next(r.handler for r in app.router.routes() if r.resource.canonical == '/api/status')
                public = json.loads((await handler(SimpleNamespace())).body)
                self.assertTrue(public['fan']['confirmed'])
                self.assertEqual(public['fan']['level'], 6)
                self.assertEqual(public['decision']['execution_trial']['to'], 4)
                self.assertEqual(public['decision']['execution_trial']['outcome'], 'invalidated')
            finally:
                try:
                    await app.on_cleanup[-1](app)
                    self.assertIsNone(state['thermal_memory'].task)
                finally:
                    state['disk'].shutdown(wait=True)
                    state['history_disk'].shutdown(wait=True)

    async def test_transient_row_is_not_retried_and_recovery_is_qualified(self):
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from test_scenarios import fleet, feedback
        from test_fan_snapshot import matched_fan
        clock = [54]
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'time',
                SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0])), \
                patch.object(service.MotorSchedule, 'load', return_value=service.MotorSchedule()):
            app = await service.create_app(Path(directory), high_wind_gate=True)
            state = app[service.STATE]
            state['settings']['manual_level'] = None
            try:
                controller = state['controller']
                for now in range(0, 55, 3):
                    decision = controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=5),
                        now, minimum_level=0, feedback=feedback(now, 7))
                state.update(decision=decision, fan_request=service.FanRequest(7),
                    fan=matched_fan(7, 54, 1054), last_poll=54, last_fan=54)
                startup = app.on_startup[-1]
                loops = dict(zip(startup.__code__.co_freevars, (c.cell_contents for c in startup.__closure__)))
                storage = loops['storage']
                handler = next(r.handler for r in app.router.routes() if r.resource.canonical == '/api/status')
                async def row(now, error=None, after_append=False):
                    clock[0] = now
                    state.update(last_poll=now, last_fan=now, fan=matched_fan(7, now, 1000 + now))
                    if now > 54:
                        controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=5),
                            now, minimum_level=0, feedback=feedback(now, 7))
                    state['queue'].put_nowait(dict(ts=1000 + now, nodes=[]))
                    state['stop'] = True
                    target = 'size' if after_append else 'prune'
                    with patch.object(state['journal'], target, side_effect=error) if error else patch.object(state['journal'], 'size', return_value=100):
                        await storage()
                await row(54, TimeoutError('private token/path'))
                public = json.loads((await handler(SimpleNamespace())).body)
                self.assertEqual(public['requested_level'], 7)
                self.assertTrue(public['fan']['confirmed'])
                self.assertEqual(public['storage_health']['phase'], 'holding')
                self.assertEqual((state['history_recording']['writes'], state['history_recording']['failed_writes'], state['dropped_records']), (0, 1, 1))
                self.assertNotIn('private', json.dumps(public))
                loopvars = dict(zip(loops['fan'].__code__.co_freevars, (c.cell_contents for c in loops['fan'].__closure__)))
                apply = loopvars['apply_fan']
                from test_service import FakeFan
                device = FakeFan(['on', 'favorite', 7, 900])
                state['fan_request'] = service.FanRequest(5)
                await apply(device)
                self.assertEqual(device.writes, [])
                self.assertEqual(state['motor_schedule'].attempts, 0)
                state['fan_request'] = service.FanRequest(7)
                snapshot = dict(zip(apply.__code__.co_freevars, (c.cell_contents for c in apply.__closure__)))['request_snapshot']
                clock[0] = 62.999
                state.update(last_poll=clock[0], last_fan=clock[0], fan=matched_fan(7, clock[0], 1000 + clock[0]))
                with patch.object(controller, 'storage_safe', return_value=True):
                    self.assertFalse(snapshot().protected)
                    clock[0] = 63
                    self.assertTrue(snapshot().protected)
                state['storage_health'] = self.episode_for_test()
                await row(57)
                self.assertEqual(state['storage_health'].phase, 'holding')
                await row(60)
                self.assertEqual(state['storage_health'].phase, 'healthy')
                self.assertEqual(state['history_recording']['writes'], 2)
                clock[0] = 63
                nodes = fleet(63, cpu=49, gpu=47, board=54, power=60, load=5)
                nodes[1].update(online=False, failure_kind='transport', sensors=[])
                controller.decide(nodes, 63, minimum_level=0, feedback=feedback(63, 7))
                state.update(last_poll=63, last_fan=63, fan=matched_fan(7, 63, 1063))
                state['queue'].put_nowait(dict(ts=1063, nodes=[]))
                with patch.object(state['journal'], 'prune', side_effect=TimeoutError('node missing')):
                    await storage()
                self.assertEqual(state['storage_health'].phase, 'protect')
                self.assertEqual(state['history_recording']['failed_writes'], 2)
                await row(66, TimeoutError('after append'), after_append=True)
                self.assertEqual(state['storage_health'].phase, 'protect')
                self.assertTrue(state['storage_health'].last_error['committed'])
                self.assertEqual((state['history_recording']['writes'], state['history_recording']['failed_writes'], state['dropped_records']), (3, 2, 2))
            finally:
                try:
                    await app.on_cleanup[-1](app)
                    self.assertIsNone(state['thermal_memory'].task)
                finally:
                    state['disk'].shutdown(wait=True)
                    state['history_disk'].shutdown(wait=True)


class StorageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_fan_snapshot import matched_fan
        from test_scenarios import fleet, feedback
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 54
        for target, value in (('time', SimpleNamespace(monotonic=lambda: self.now,
                time=lambda: 1000 + self.now)), ('fsync_directory', lambda path: None)):
            patcher = patch.object(service, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(service.MotorSchedule, 'load', return_value=service.MotorSchedule())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = await service.create_app(Path(self.directory.name))
        self.state = self.app[service.STATE]
        self.state['settings']['manual_level'] = None
        self.controller = self.state['controller']
        for now in range(0, 55, 3):
            decision = self.controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=5),
                now, minimum_level=0, feedback=feedback(now, 16, confirmed=False, execution='pending'))
        self.state.update(decision=decision, fan_request=service.FanRequest(10))
        self.matched_fan = matched_fan
        startup = self.app.on_startup[-1]
        self.loops = dict(zip(startup.__code__.co_freevars, (c.cell_contents for c in startup.__closure__)))
        self.apply = dict(zip(self.loops['fan'].__code__.co_freevars,
            (c.cell_contents for c in self.loops['fan'].__closure__)))['apply_fan']
        self.snapshot = dict(zip(self.apply.__code__.co_freevars,
            (c.cell_contents for c in self.apply.__closure__)))['request_snapshot']
        class OverflowOnce(asyncio.Queue):
            overflow = False
            def put_nowait(self, row):
                if self.overflow:
                    self.overflow = False
                    raise asyncio.QueueFull
                return super().put_nowait(row)
        self.state['queue'] = OverflowOnce(maxsize=2)

    async def asyncTearDown(self):
        self.state['settings_task'] = None
        self.state['stop'] = True
        try:
            await self.app.on_cleanup[-1](self.app)
        finally:
            self.state['disk'].shutdown(wait=True)
            self.state['history_disk'].shutdown(wait=True)

    async def row(self, now, error=False, **fan):
        self.now = now
        actual = self.matched_fan(16, now, 1000 + now)
        actual.update(confirmed=False, execution='pending')
        actual.update(fan)
        self.state.update(last_poll=now, last_fan=now, fan=actual, stop=True)
        self.state['queue'].put_nowait(dict(ts=1000 + now, nodes=[]))
        with patch.object(self.state['journal'], 'prune', side_effect=TimeoutError('private/path')) if error else ExitStack():
            await self.loops['storage']()

    @asynccontextmanager
    async def paused_row(self, now, stage='return'):
        entered, release = asyncio.Event(), asyncio.Event()
        thread_release = Event()
        writer = None
        drain = service.drain_owned
        publication = self.state['admission'].publication
        prune = self.state['journal'].prune
        loop = asyncio.get_running_loop()
        async def delayed(owned):
            result = await drain(owned)
            if asyncio.current_task() is writer:
                entered.set()
                await asyncio.wait_for(release.wait(), 3)
            return result
        @asynccontextmanager
        async def publishing():
            if asyncio.current_task() is writer:
                entered.set()
                await asyncio.wait_for(release.wait(), 3)
            async with publication():
                yield
        def pruning(*args):
            loop.call_soon_threadsafe(entered.set)
            if not thread_release.wait(3):
                raise TimeoutError('test writer blocked')
            return prune(*args)
        target = (patch.object(service, 'drain_owned', delayed) if stage == 'return' else
            patch.object(self.state['admission'], 'publication', publishing) if stage == 'publication' else
            patch.object(self.state['journal'], 'prune', pruning))
        with target:
            writer = asyncio.create_task(self.row(now))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                yield
            finally:
                release.set()
                thread_release.set()
                await asyncio.wait_for(writer, 3)

    async def configure(self, error=False):
        import json
        handler = next(r.handler for r in self.app.router.routes()
            if r.method == 'POST' and r.resource.canonical == '/api/settings')
        self.state['stop'] = False
        try:
            with patch.object(self.state['journal'], 'prune', side_effect=TimeoutError('private maintenance')) if error else ExitStack():
                response = await handler(SimpleNamespace(json=AsyncMock(return_value=dict(self.state['settings']))))
            body = json.loads(response.body)
            self.assertEqual(bool(body['maintenance_error']), error)
            self.assertIsNone(self.state['settings_task'])
        finally:
            self.state['stop'] = True

    async def queue_fault(self):
        from test_service import OfflineSession
        async def stop(delay):
            self.state['stop'] = True
        self.state['stop'] = False
        self.state['queue'].overflow = True
        with patch.object(service.aiohttp, 'ClientSession', OfflineSession), patch.object(service.asyncio, 'sleep', stop):
            await self.loops['monitor']()
        if self.state['thermal_memory'].task is not None:
            await self.state['thermal_memory'].task

    async def fault_race(self, kind, stage):
        await self.row(54, error=True)
        await self.row(57)
        health = self.state['storage_health']
        self.assertEqual(health.recovery_successes, 1)
        writes = self.state['history_recording']['writes']
        async with self.paused_row(58, stage):
            self.now = 59
            for _ in range(2):
                if kind == 'queue':
                    await self.queue_fault()
                else:
                    await self.configure(error=True)
            self.assertEqual((health.failure_epoch, health.consecutive_failures), (3, 2))
            self.assertEqual(health.last_error['write_phase'], kind)
            before, message = deepcopy(vars(health)), self.state['storage_error']
        self.assertEqual(vars(health), before)
        self.assertEqual(self.state['storage_error'], message)
        self.assertEqual(self.state['history_recording']['writes'], writes + 1)
        self.assertGreater(self.state['log_bytes'], 0)
        await self.row(62)
        self.assertEqual((health.phase, health.recovery_successes), ('protect', 1))
        await self.row(65)
        self.assertEqual(health.phase, 'healthy')
        self.assertEqual(health.failure_epoch, 3)

    async def test_queue_fault_while_worker_runs_preserves_current_fault(self):
        await self.fault_race('queue', 'worker')

    async def test_queue_fault_after_worker_returns_preserves_current_fault(self):
        await self.fault_race('queue', 'return')

    async def test_queue_fault_while_publication_waits_preserves_current_fault(self):
        await self.fault_race('queue', 'publication')

    async def test_settings_maintenance_fault_during_completion_await_preserves_current_fault(self):
        await self.fault_race('settings_maintenance', 'return')

    async def test_settings_maintenance_fault_while_publication_waits_preserves_current_fault(self):
        await self.fault_race('settings_maintenance', 'publication')

    async def test_full_write_failures_reset_credit_and_preserve_commit_certainty(self):
        journal = self.state['journal']
        cases = ((journal, 'prune', 'prune', False), (journal, 'append', 'append', None),
            (service.os, 'fsync', 'append', None), (journal, '_sync_directory', 'append', None),
            (journal, '_publish', 'append', None), (journal, 'size', 'accounting', True),
            (service, 'learning_archive_usage', 'accounting', True))
        for index, (target, name, phase, committed) in enumerate(cases):
            with self.subTest(failure=name):
                self.state['storage_health'] = service.StorageHealth()
                now = 100 + index * 20
                await self.row(now, error=True)
                await self.row(now + 3)
                health = self.state['storage_health']
                self.assertEqual(health.recovery_successes, 1)
                before = dict(self.state['history_recording'])
                dropped = self.state['dropped_records']
                archive_usage = service.learning_archive_usage
                calls = []
                def fail_accounting(root):
                    calls.append(root)
                    if len(calls) == 1:
                        return archive_usage(root)
                    raise TimeoutError('private/path/token')
                failure = fail_accounting if name == 'learning_archive_usage' else TimeoutError('private/path/token')
                with patch.object(target, name, side_effect=failure) as failed:
                    await self.row(now + 6)
                self.assertEqual(failed.call_count, 2 if name == 'learning_archive_usage' else 1)
                self.assertEqual((health.phase, health.recovery_successes, health.failure_epoch), ('protect', 0, 2))
                self.assertEqual((health.last_error['write_phase'], health.last_error['committed']), (phase, committed))
                self.assertEqual(self.state['history_recording']['writes'], before['writes'] + (committed is True))
                self.assertEqual(self.state['history_recording']['failed_writes'], before['failed_writes'] + (committed is not True))
                self.assertEqual(self.state['dropped_records'], dropped + (committed is False))
                self.assertNotIn('private', str(health.status()) + self.state['storage_error'])

    async def test_stale_configuration_and_pending_or_failed_settings_break_recovery(self):
        for index, case in enumerate(('revision', 'away_back', 'values', 'pending', 'error')):
            with self.subTest(settings=case):
                self.state['storage_health'] = service.StorageHealth()
                now = 100 + index * 20
                await self.row(now, error=True)
                await self.row(now + 3)
                async with self.paused_row(now + 6):
                    if case == 'revision':
                        self.state['settings_revision'] += 1
                    elif case == 'away_back':
                        original = dict(self.state['settings'])
                        self.state['settings']['retention_days'] += 1
                        self.state['settings_revision'] += 1
                        self.state['settings'] = original
                        self.state['settings_revision'] += 1
                    elif case == 'values':
                        self.state['settings']['retention_days'] += 1
                    elif case == 'pending':
                        self.state['settings_task'] = asyncio.get_running_loop().create_future()
                    else:
                        self.state['settings_error'] = 'settings unverified'
                health = self.state['storage_health']
                self.assertEqual((health.recovery_successes, health.first_success, health.recovery_revision), (0, None, None))
                self.assertEqual(health.phase, 'protect')
                self.assertEqual(health.consecutive_failures, 0)
                if self.state['settings_task'] is not None:
                    self.state['settings_task'].cancel()
                self.state.update(settings_task=None, settings_error='')
                await self.row(now + 9)
                self.assertEqual((health.phase, health.recovery_successes), ('protect', 1))
                await self.row(now + 12)
                self.assertEqual(health.phase, 'healthy')

    async def test_completed_writes_from_different_revisions_cannot_form_a_pair(self):
        await self.row(54, error=True)
        await self.row(57)
        self.state['settings_revision'] += 1
        await self.row(60)
        health = self.state['storage_health']
        self.assertEqual((health.phase, health.recovery_successes, health.first_success), ('protect', 1, 60))
        await self.row(62.999)
        self.assertEqual(health.phase, 'protect')
        await self.row(63)
        self.assertEqual(health.phase, 'healthy')

    async def test_settings_maintenance_and_status_supply_no_recovery_credit(self):
        await self.row(54, error=True)
        health = self.state['storage_health']
        await self.configure()
        before = deepcopy(vars(health))
        handler = next(r.handler for r in self.app.router.routes() if r.resource.canonical == '/api/status')
        for _ in range(3):
            await handler(SimpleNamespace())
        self.assertEqual(vars(health), before)
        self.assertEqual((health.recovery_successes, health.failure_epoch), (0, 1))

    async def test_recovery_preserves_independent_gates_and_does_not_command_or_confirm(self):
        from test_service import FakeFan
        from test_scenarios import fleet, feedback
        for index, gate in enumerate(('memory', 'memory_error', 'motor_debt', 'motor_fault',
                'motor_persistence', 'thermal', 'source', 'readback', 'intent')):
            with self.subTest(gate=gate):
                self.state['storage_health'] = service.StorageHealth()
                now = 100 + index * 20
                await self.row(now, error=True)
                schedule = self.state['motor_schedule'] = service.MotorSchedule()
                memory = self.state['thermal_memory']
                memory.invalidate()
                memory.error = gate == 'memory_error'
                if gate == 'motor_debt':
                    schedule.attempts, schedule.expected_level = 1, 15
                    schedule.last_attempt = now
                    schedule.last_decrease = now
                elif gate == 'motor_fault':
                    schedule.fault = True
                elif gate == 'motor_persistence':
                    schedule.persistence_error = 'not durable'
                elif gate in ('thermal', 'source'):
                    nodes = fleet(now, cpu=90 if gate == 'thermal' else 49, gpu=47, board=54, power=60, load=5)
                    if gate == 'source':
                        nodes[1].update(online=False, failure_kind='transport', sensors=[])
                    self.state['decision'] = self.controller.decide(nodes, now, minimum_level=0,
                        feedback=feedback(now, 16, confirmed=False, execution='pending'))
                if gate not in ('memory', 'memory_error'):
                    await memory.flush(self.controller)
                    self.assertTrue(memory.ready)
                memory_before = memory.status()
                thermal_before = self.controller.export_thermal_memory()
                motor_before = deepcopy(vars(schedule))
                fan = dict(rpm=None) if gate == 'readback' else {}
                await self.row(now + 3, **fan)
                await self.row(now + 6, **fan)
                self.assertEqual(self.state['storage_health'].phase, 'healthy')
                self.assertEqual(memory.status(), memory_before)
                self.assertEqual(self.controller.export_thermal_memory(), thermal_before)
                self.assertEqual(vars(schedule), motor_before)
                self.assertEqual((self.state['fan']['level'], self.state['fan']['confirmed'],
                    self.state['fan']['execution']), (16, False, 'pending'))
                self.state['fan_request'] = service.FanRequest(10)
                device = FakeFan(['on', 'favorite', 16, 2096])
                await self.apply(device)
                self.assertEqual(device.writes, [])
                self.assertEqual(vars(schedule), motor_before)

    async def test_recovery_does_not_renew_expired_exact_edge_authority(self):
        from test_service import FakeFan
        from test_scenarios import fleet, feedback
        await self.row(54, error=True)
        for now in range(57, 658, 3):
            decision = self.controller.decide(fleet(now, cpu=65, gpu=52, board=64, load=60, power=100),
                now, minimum_level=0, feedback=feedback(now, 16, confirmed=False, execution='pending'))
            operation = decision.get('execution_trial') or {}
            if operation.get('status') == 'pending' and operation.get('purpose') == 'ceiling_exit':
                break
        self.assertEqual((operation.get('from'), operation.get('to')), (16, 15))
        authority, motion = self.controller.high_wind, self.controller.motion
        deadline = operation['original_deadline']
        self.assertTrue(authority.permits(now, 16, 15))
        self.state.update(decision=decision, fan_request=service.FanRequest(15, high_wind=authority, motion=motion))
        memory = self.state['thermal_memory']
        await memory.flush(self.controller)
        self.assertTrue(memory.ready)
        thermal_before = self.controller.export_thermal_memory()
        await self.row(now + 3)
        await self.row(now + 6)
        self.assertEqual(self.state['storage_health'].phase, 'healthy')
        self.assertFalse(authority.permits(self.now, 16, 15))
        self.assertIs(self.controller.high_wind, authority)
        self.assertIs(self.controller.motion, motion)
        self.assertEqual(self.controller.export_thermal_memory(), thermal_before)
        self.assertEqual(self.state['decision']['execution_trial']['original_deadline'], deadline)
        device = FakeFan(['on', 'favorite', 16, 2096])
        await self.apply(device)
        self.assertEqual(device.writes, [])
        self.assertEqual(self.state['motor_schedule'].attempts, 0)
        self.assertFalse(self.state['fan']['confirmed'])


if __name__ == '__main__':
    unittest.main()
