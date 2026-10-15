import errno
import unittest
import service


class StorageHealthTests(unittest.TestCase):
    def episode(self):
        health = service.StorageHealth()
        health.failure(100, 1000, TimeoutError('private/path/token'), 'prune', False, eligible=True)
        return health

    def test_soft_deadline_never_renews(self):
        health = self.episode()
        self.assertEqual(health.refresh(108.999, eligible=True), 'holding')
        health.success(103, eligible=True)
        health.failure(106, 1006, OSError(errno.EAGAIN, 'private'), 'prune', False, eligible=True)
        self.assertEqual(health.first_failure, 100)
        self.assertEqual(health.refresh(109, eligible=True), 'protect')
        self.assertNotIn('private', str(health.last_error))

    def test_third_failure_hard_errors_and_unknown_actual(self):
        health = self.episode()
        health.failure(103, 1003, TimeoutError(), 'prune', False, eligible=True)
        self.assertEqual(health.phase, 'holding')
        health.failure(106, 1006, TimeoutError(), 'prune', False, eligible=True)
        self.assertEqual(health.phase, 'protect')
        for error in (OSError(errno.ENOSPC, 'disk'), PermissionError(), ValueError(), RuntimeError()):
            health = service.StorageHealth()
            health.failure(100, 1000, error, 'prune', False, eligible=True)
            self.assertEqual(health.phase, 'protect')
        self.assertEqual(self.episode().refresh(101, eligible=False), 'protect')

    def test_hard_fault_requires_two_new_qualified_successes(self):
        health = service.StorageHealth()
        health.failure(100, 1000, OSError(errno.ENOSPC, 'disk'), 'prune', False, eligible=True)
        health.success(103, eligible=True)
        self.assertEqual(health.phase, 'protect')
        health.success(106, eligible=True)
        self.assertEqual(health.phase, 'healthy')
        self.assertEqual(health.last_error['errno'], errno.ENOSPC)

    def test_explicit_temporary_errnos_before_append_hold(self):
        for code in (errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ETIMEDOUT):
            health = service.StorageHealth()
            health.failure(100, 1000, OSError(code, 'private'), 'prune', False, eligible=True)
            self.assertEqual(health.phase, 'holding')

    def test_unknown_append_and_postappend_are_hard(self):
        for phase, committed in (("append", None), ("accounting", True)):
            health = service.StorageHealth()
            health.failure(100, 1000, TimeoutError(), phase, committed, eligible=True)
            self.assertEqual(health.phase, 'protect')
            self.assertEqual(health.last_error['committed'], committed)

    def test_pause_release_does_not_cancel_critical_ascent(self):
        from core import Controller
        from test_core import HOSTS
        from test_scenarios import fleet, feedback
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        decision = controller.decide(fleet(100, cpu=75), 100, minimum_level=0, feedback=feedback(100, 7))
        authority = controller.motion
        paused = controller.pause_release('storage hold', 100)
        self.assertEqual(paused['level'], 10)
        self.assertEqual(paused['execution_trial']['status'], 'pending')
        self.assertEqual(controller.motion, authority)

    def test_two_qualified_successes_recover_without_erasing_diagnostic(self):
        health = self.episode()
        health.success(103, eligible=True)
        self.assertEqual(health.phase, 'holding')
        health.success(105.999, eligible=True)
        self.assertEqual(health.phase, 'holding')
        health.success(106, eligible=True)
        self.assertEqual(health.phase, 'healthy')
        self.assertEqual(health.last_error['category'], 'TimeoutError')


class StorageServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def episode_for_test():
        health = service.StorageHealth()
        health.failure(54, 1054, TimeoutError(), 'prune', False, eligible=True)
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
                await state['qr'].close()
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
                await state['qr'].close()
                state['disk'].shutdown(wait=True)
                state['history_disk'].shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
