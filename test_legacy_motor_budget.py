import hashlib
import asyncio
import inspect
import sys
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import ThermalView
from service import (ActuationSnapshot, AdmissionBoundary, FanRequest, HOSTS,
                     MOTOR_IDENTITY, MotorSchedule, fan_apply, create_app, STATE)
from policy_memory import memory_digest
from test_service import FakeFan


class LegacyMotorBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'budget.json'
        self.now = 100.
        self.clock = patch('service.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sync = patch('service.fsync_directory')
        self.sync.start()
        self.addCleanup(self.sync.stop)
        if os.name == 'nt':
            original = Path.lstat
            def private_lstat(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                values = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                values['st_mode'] &= ~0o077
                values['st_ctime_ns'] = info.st_birthtime_ns
                return SimpleNamespace(**values)
            permissions = patch.object(Path, 'lstat', private_lstat)
            permissions.start()
            self.addCleanup(permissions.stop)
            original_fstat = os.fstat
            def private_fstat(fd):
                info = original_fstat(fd)
                values = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                values['st_mode'] &= ~0o077
                values['st_ctime_ns'] = info.st_birthtime_ns
                return SimpleNamespace(**values)
            descriptors = patch('service.os.fstat', side_effect=private_fstat)
            descriptors.start()
            self.addCleanup(descriptors.stop)

    def load(self, version=1, attempts=2, fault=True):
        self.raw = json.dumps(dict(version=version, identity=MOTOR_IDENTITY,
            attempts=attempts, expected_level=5 if attempts else None, fault=fault)).encode()
        self.path.write_bytes(self.raw)
        os.chmod(self.path, 0o600)
        return MotorSchedule.load(self.path, self.root / 'token')

    def request(self, **changes):
        view = ThermalView(1, tuple((h, ()) for h in HOSTS),
            tuple(((h, 'node', 'power', 'cpu', 'cpu'), 50., self.now) for h in HOSTS),
            (), True, 50., self.now + 6.)
        snap = ActuationSnapshot(1, 1, None, None, None, None, 10, 5, 5, None,
            ('on', 'favorite', 5, 1000, 100.), 'ack', 'ack', 1, 'healthy',
            tuple(sorted(MOTOR_IDENTITY.items())), None)
        if hasattr(snap, 'thermal_view'):
            snap = replace(snap, thermal_view=view)
        return FanRequest(5, snapshot=replace(snap, **changes))

    def test_v1_exhaustion_is_diagnostic_on_load_and_qualified_retirement(self):
        schedule = self.load()
        self.assertFalse(schedule.fault, 'historical exhaustion must not be a permanent current fault')
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.now = 108.
        fan = FakeFan(['on', 'favorite', 5, 1000])
        result = fan_apply(fan, self.request, schedule)
        self.assertTrue(result['confirmed'])
        self.assertEqual(json.loads(self.path.read_bytes())['version'], 2)
        self.assertEqual(fan.writes, [])
        archived = list(self.root.glob('.legacy-budget-*/*'))
        self.assertTrue(any(p.read_bytes() == self.raw for p in archived))

    def test_v2_exhaustion_remains_current_debt_on_restart(self):
        schedule = self.load(version=2)
        self.assertTrue(schedule.fault)
        self.assertFalse(schedule.persistence_error)
        self.now = 108.
        fan = FakeFan(['on', 'favorite', 4, 1000])
        fan_apply(fan, self.request, schedule)
        self.assertEqual(fan.writes, [])
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_matching_target_never_bypasses_missing_thermal_view(self):
        schedule = self.load()
        self.now = 108.
        fan = FakeFan(['on', 'favorite', 5, 1000])
        request = self.request()
        if hasattr(request.snapshot, 'thermal_view'):
            request = replace(request, snapshot=replace(request.snapshot, thermal_view=None))
        result = fan_apply(fan, lambda: request, schedule)
        self.assertFalse(result['confirmed'])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(fan.writes, [])

    def test_refusals_preserve_exact_v1_and_never_send(self):
        for case in ('wait', 'auto16', 'off', 'zero', 'invalid', 'hot', 'stale',
                     'faults', 'incomplete', 'storage', 'unacked', 'identity', 'unknown'):
            with self.subTest(case=case):
                self.now = 100.
                schedule = self.load()
                self.now = 107.9 if case == 'wait' else 108.
                request = self.request()
                snap = request.snapshot
                view = snap.thermal_view
                endpoint = ['on', 'favorite', 5, 1000]
                if case == 'auto16': endpoint = ['on', 'auto', 16, 1000]
                if case == 'off': endpoint[0] = 'off'
                if case == 'zero': endpoint[3] = 0
                if case == 'invalid': endpoint[2] = 17
                if case == 'hot': snap = replace(snap, thermal_view=replace(view, maximum_c=80))
                if case == 'stale': snap = replace(snap, thermal_view=replace(view, expires_mono=108.))
                if case == 'faults': snap = replace(snap, thermal_view=replace(view, faults=('bad',)))
                if case == 'incomplete': snap = replace(snap, thermal_view=replace(view, complete=False))
                if case == 'storage': snap = replace(snap, storage_state='protect', blocker='storage_protect')
                if case == 'unacked': snap = replace(snap, acknowledged_digest='old')
                if case == 'identity': snap = replace(snap, budget_identity=())
                request = replace(request, snapshot=snap)
                fan = FakeFan(endpoint)
                fan.fail = case == 'unknown'
                result = fan_apply(fan, lambda: request, schedule)
                self.assertFalse(result['confirmed'])
                self.assertFalse(result['fault'])
                self.assertTrue(result['legacy_migration_pending'])
                self.assertEqual(self.path.read_bytes(), self.raw)
                self.assertEqual(fan.writes, [])

    def test_archive_failure_and_post_archive_snapshot_change_never_send(self):
        for failure in ('archive', 'replace', 'sync', 'snapshot'):
            with self.subTest(failure=failure):
                self.root = Path(self.temp.name) / failure
                self.root.mkdir()
                self.path = self.root / 'budget.json'
                self.now = 100.
                schedule = self.load()
                self.now = 108.
                request = self.request()
                fan = FakeFan(['on', 'favorite', 5, 1000])
                original = schedule._archive_legacy
                def archive():
                    original()
                    nonlocal request
                    request = replace(request, snapshot=replace(request.snapshot, blocker='pairing'))
                target = ('service.atomic_write' if failure == 'replace' else
                    'service.fsync_directory' if failure == 'sync' else None)
                if failure == 'snapshot':
                    context = patch.object(schedule, '_archive_legacy', side_effect=archive)
                elif target:
                    context = patch(target, side_effect=OSError('injected'))
                else:
                    context = patch.object(schedule, '_archive_legacy', side_effect=OSError('injected'))
                with context:
                    result = fan_apply(fan, lambda: request, schedule)
                self.assertFalse(result['confirmed'])
                self.assertEqual(fan.writes, [])
                self.assertEqual(self.path.read_bytes(), self.raw)

    def test_invalid_records_and_one_attempt_are_not_legacy_retirements(self):
        for version, attempts, fault in ((1, 1, False), (1, 1, True), (1, 2, False), (2, 2, True)):
            with self.subTest(version=version, attempts=attempts, fault=fault):
                schedule = self.load(version, attempts, fault)
                self.assertFalse(schedule.legacy_pending)
                self.assertEqual(schedule.attempts, attempts)
                if fault and attempts < 2:
                    self.assertTrue(schedule.persistence_error)
        for raw in (b'{', b'{}', b'{"version":true}'):
            self.path.write_bytes(raw)
            schedule = MotorSchedule.load(self.path, self.root / 'token')
            self.assertTrue(schedule.persistence_error)
            self.assertTrue(schedule.fault)

    def test_migration_archive_and_new_uncertainty_survive_restart(self):
        schedule = self.load()
        self.now = 108.
        original_raw = self.raw
        metadata = schedule.legacy.metadata.copy()
        self.assertTrue(fan_apply(FakeFan(['on', 'favorite', 5, 1000]), self.request, schedule)['confirmed'])
        archive = self.root / ('.legacy-budget-' + hashlib.sha256(original_raw).hexdigest())
        self.assertEqual((archive / 'source.bin').read_bytes(), original_raw)
        self.assertEqual(json.loads((archive / 'provenance.json').read_bytes()),
            dict(metadata=metadata, sha256=hashlib.sha256(original_raw).hexdigest()))
        restarted = MotorSchedule.load(self.path, self.root / 'token')
        self.assertFalse(restarted.legacy_pending)
        self.assertFalse(restarted.fault)
        self.now = 120.
        fan = FakeFan(['on', 'favorite', 5, 1000])
        fan.ignore_writes = fan.lose_ack = True
        fan_apply(fan, lambda: FanRequest(7), restarted)
        self.now = 130.
        fan_apply(fan, lambda: FanRequest(7), restarted)
        again = MotorSchedule.load(self.path, self.root / 'token')
        self.assertEqual(again.attempts, 2)
        self.assertTrue(again.fault)
        self.assertFalse(again.legacy_pending)
        self.assertEqual(json.loads(self.path.read_bytes())['version'], 2)

    def test_archive_conflict_source_change_and_uncertain_commit(self):
        for failure in ('conflict', 'source', 'security', 'committed_sync'):
            with self.subTest(failure=failure):
                self.root = Path(self.temp.name) / failure
                self.root.mkdir()
                self.path = self.root / 'budget.json'
                altered = None
                reached = []
                self.now = 100.
                schedule = self.load()
                self.now = 108.
                fan = FakeFan(['on', 'favorite', 5, 1000])
                original = schedule._archive_legacy
                if failure == 'conflict':
                    original()
                    archive = self.root / ('.legacy-budget-' + schedule.legacy.sha256)
                    (archive / 'source.bin').write_bytes(b'conflicting evidence')
                    context = patch('service.fsync_directory')
                elif failure in ('source', 'security'):
                    def changed():
                        nonlocal altered
                        original()
                        reached.append(failure)
                        if failure == 'source':
                            self.path.write_bytes(self.raw + b' ')
                        else:
                            info = self.path.lstat()
                            values = vars(info).copy() if isinstance(info, SimpleNamespace) else {
                                k: getattr(info, k) for k in dir(info) if k.startswith('st_')}
                            values['st_mode'] |= 0o022
                            altered = patch.object(Path, 'lstat', lambda path: SimpleNamespace(**values)
                                if path == self.path else info)
                            altered.start()
                            self.addCleanup(altered.stop)
                    context = patch.object(schedule, '_archive_legacy', side_effect=changed)
                else:
                    def uncertain(path):
                        if json.loads(self.path.read_bytes())['version'] == 2:
                            reached.append(failure)
                            raise OSError('commit uncertain')
                    context = patch('service.fsync_directory', side_effect=uncertain)
                with context:
                    result = fan_apply(fan, self.request, schedule)
                if failure == 'security':
                    altered.stop()
                self.assertFalse(result['confirmed'])
                self.assertEqual(fan.writes, [])
                self.assertTrue(schedule.persistence_error)
                if failure != 'conflict':
                    self.assertEqual(reached, [failure])
                if failure == 'committed_sync':
                    self.assertEqual(json.loads(self.path.read_bytes())['version'], 2)
                    reboot = MotorSchedule.load(self.path, self.root / 'token')
                    self.assertEqual(reboot.schema_version, 2)
                    self.assertFalse(reboot.legacy_pending)

    def test_source_changed_by_final_qualification_is_not_overwritten(self):
        schedule = self.load()
        self.now = 108.
        archived = False
        original = schedule._archive_legacy
        def archive():
            nonlocal archived
            original()
            archived = True
        def current():
            if archived:
                self.path.write_bytes(self.raw + b' ')
            return self.request()
        fan = FakeFan(['on', 'favorite', 5, 1000])
        with patch.object(schedule, '_archive_legacy', side_effect=archive):
            result = fan_apply(fan, current, schedule)
        self.assertFalse(result['confirmed'])
        self.assertEqual(self.path.read_bytes(), self.raw + b' ')
        self.assertEqual(fan.writes, [])
        self.assertTrue(schedule.persistence_error)

    def test_final_source_validation_delay_cannot_retire_expired_admission(self):
        schedule = self.load()
        self.now = 108.
        request = self.request(original_deadline=110.)
        original = schedule._validate_legacy_source
        reached = []
        def delayed():
            original()
            if inspect.currentframe().f_back.f_code.co_name == 'before_replace':
                self.now = 115.
                reached.append(self.now)
        fan = FakeFan(['on', 'favorite', 5, 1000])
        with patch.object(schedule, '_validate_legacy_source', new=delayed):
            result = fan_apply(fan, lambda: request, schedule)
        self.assertEqual(reached, [115.])
        self.assertFalse(result['confirmed'])
        self.assertTrue(schedule.legacy_pending)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(fan.writes, [])

    def test_actual_monitor_and_fan_retire_existing_high_endpoints_without_writes(self):
        from test_fan_snapshot import LocalTelemetry, matched_fan
        async def exercise(level):
            self.root = Path(self.temp.name) / ('endpoint_' + str(level))
            self.root.mkdir()
            self.path = self.root / 'budget.json'
            self.now = 100.
            schedule = self.load()
            with patch('policy_memory.ThermalMemoryStore.load', return_value=None):
                app = await create_app(self.root)
            state = app[STATE]
            startup = inspect.getclosurevars(app.on_startup[-1]).nonlocals
            monitor, fan_loop = startup['monitor'], startup['fan']
            device = FakeFan(['on', 'favorite', level, 1500])
            state.update(motor_schedule=schedule, device=device, last_identity=100.,
                fan=dict(matched_fan(level, 100.), attempts=2, confirmed=False, execution='pending'))
            async def stop(delay):
                state['stop'] = True
            try:
                with patch('policy_memory.ThermalMemoryStore.save', side_effect=memory_digest):
                    for tick in (100., 103., 106., 109., 112., 115.):
                        self.now = tick
                        state['stop'] = False
                        with patch('service.aiohttp.ClientSession', side_effect=lambda **kw: LocalTelemetry(__import__('time').time())), \
                                patch('service.asyncio.sleep', stop):
                            await monitor()
                        row = state['queue'].get_nowait()
                        self.assertEqual(row['fan']['attempts'], schedule.attempts)
                        await state['thermal_memory'].flush(state['controller'])
                        state['stop'] = False
                        with patch('service.asyncio.sleep', stop):
                            await fan_loop()
                        if not schedule.legacy_pending:
                            break
                    self.assertFalse(schedule.legacy_pending, (level, state['fan'], state['decision']))
                    self.assertEqual(json.loads(self.path.read_bytes())['version'], 2)
                    self.assertEqual(device.writes, [])
                    self.assertEqual(device.actual[2], level)
            finally:
                await state['qr'].close()
                state['disk'].shutdown(wait=True)
                state['history_disk'].shutdown(wait=True)
        for level in (5, 11, 16):
            with self.subTest(level=level):
                asyncio.run(exercise(level))

    def test_load_is_readonly_and_one_attempt_new_write_carries_v2_debt(self):
        schedule = self.load(attempts=1, fault=False)
        metadata = self.path.lstat()
        with patch('service.atomic_write') as write, patch.object(MotorSchedule, '_archive_legacy') as archive:
            loaded = MotorSchedule.load(self.path, self.root / 'token')
        write.assert_not_called()
        archive.assert_not_called()
        self.assertEqual(loaded.attempts, 1)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.path.lstat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertTrue(schedule.persist_debt_then_set_budget(2, 7, True))
        reboot = MotorSchedule.load(self.path, self.root / 'token')
        self.assertEqual((reboot.schema_version, reboot.attempts, reboot.fault), (2, 2, True))

    def test_create_app_snapshot_uses_actual_controller_view_and_current_ack(self):
        from test_scenarios import fleet, feedback
        async def exercise():
            with patch('policy_memory.ThermalMemoryStore.load', return_value=None):
                app = await create_app(self.root)
            state = app[STATE]
            try:
                def closure(function, name):
                    return inspect.getclosurevars(function).nonlocals[name]
                shutdown = closure(app.on_cleanup[-1], 'shutdown')
                apply = closure(shutdown, 'apply_fan')
                execute_snapshot = closure(apply, 'request_snapshot')
                controller = state['controller']
                controller.decide(fleet(100, cpu=45, gpu=47, board=54, load=5, power=60),
                    100, minimum_level=0, feedback=feedback(100, 5))
                self.now = 100.
                schedule = self.load()
                state['motor_schedule'] = schedule
                self.now = 108.
                controller.decide(fleet(108, cpu=45, gpu=47, board=54, load=5, power=60),
                    108, minimum_level=0, feedback=feedback(108, 5))
                state.update(last_poll=108., last_fan=108., fan_request=FanRequest(5))
                state['fan'] = dict(configured=True, fault=False, confirmed=False,
                    power='on', mode='favorite', level=5, rpm=1000,
                    observed_mono=108., verified_at=__import__('time').time(), execution='pending')
                gate = state['thermal_memory']
                digest = memory_digest(controller.export_thermal_memory())
                gate.desired_digest = digest
                gate.acknowledged_digest = 'old'
                request = execute_snapshot()
                self.assertIs(request.snapshot.thermal_view, controller.thermal_view)
                fan = FakeFan(['on', 'favorite', 5, 1000])
                self.assertFalse(fan_apply(fan, execute_snapshot, schedule)['confirmed'])
                gate.acknowledged_digest = digest
                result = fan_apply(fan, execute_snapshot, schedule)
                self.assertFalse(schedule.legacy_pending, result)
                self.assertEqual(fan.writes, [])
                self.root = Path(self.temp.name) / 'expired_episode'
                self.root.mkdir()
                self.path = self.root / 'budget.json'
                self.now = 100.
                schedule = self.load()
                state['motor_schedule'] = schedule
                self.now = 108.
                controller._request(5, 4, 108., None, purpose='release', profile='ordinary')
                deadline = controller._policy['operation']['episode'].original_deadline
                self.now = deadline + 1
                nodes = fleet(self.now, cpu=45, gpu=47, board=54, load=5, power=60)
                controller._validate_admission(nodes, self.now)
                state.update(last_poll=self.now, last_fan=self.now)
                state['fan'].update(observed_mono=self.now, verified_at=__import__('time').time())
                digest = memory_digest(controller.export_thermal_memory())
                gate.desired_digest = gate.acknowledged_digest = digest
                self.assertEqual(execute_snapshot().snapshot.original_deadline, deadline)
                self.assertFalse(state['admission'].retire_legacy(schedule, execute_snapshot,
                    lambda: (fan.actual, self.now, 0.)))
                self.assertTrue(schedule.legacy_pending)
                self.now += 3
                controller.decide(fleet(self.now, cpu=45, gpu=47, board=54, load=5, power=60),
                    self.now, minimum_level=0, feedback=feedback(self.now, 5))
                self.assertNotEqual(execute_snapshot().snapshot.original_deadline, deadline)
                state.update(last_poll=self.now, last_fan=self.now)
                state['fan'].update(observed_mono=self.now, verified_at=__import__('time').time())
                digest = memory_digest(controller.export_thermal_memory())
                gate.desired_digest = gate.acknowledged_digest = digest
                self.assertTrue(state['admission'].retire_legacy(schedule, execute_snapshot,
                    lambda: (fan.actual, self.now, 0.)),
                    (execute_snapshot().snapshot, schedule.persistence_error))
            finally:
                await state['qr'].close()
                state['disk'].shutdown(wait=True)
                state['history_disk'].shutdown(wait=True)
        asyncio.run(exercise())

    @unittest.skipUnless(sys.platform == 'linux', 'Native Linux ownership, symlinks and directory durability required')
    def test_native_links_ownership_and_real_directory_sync(self):
        self.sync.stop()
        for case in ('source_link', 'archive_link', 'directory_link', 'wrong_uid', 'native_success'):
            with self.subTest(case=case):
                self.root = Path(self.temp.name) / case
                self.root.mkdir(mode=0o700)
                self.path = self.root / 'budget.json'
                self.now = 100.
                schedule = self.load()
                self.now = 108.
                archive = self.root / ('.legacy-budget-' + schedule.legacy.sha256)
                if case == 'source_link':
                    old = self.root / 'original'
                    self.path.rename(old)
                    self.path.symlink_to(old)
                elif case == 'archive_link':
                    archive.mkdir(mode=0o700)
                    target = self.root / 'other'
                    target.write_bytes(self.raw)
                    (archive / 'source.bin').symlink_to(target)
                elif case == 'directory_link':
                    target = self.root / 'other'
                    target.mkdir(mode=0o700)
                    archive.symlink_to(target, target_is_directory=True)
                elif case == 'wrong_uid':
                    archive.mkdir(mode=0o700)
                    with patch('service.os.getuid', return_value=os.getuid() + 1):
                        with self.assertRaises(ValueError):
                            schedule._archive_legacy()
                    continue
                result = fan_apply(FakeFan(['on', 'favorite', 5, 1000]), self.request, schedule)
                self.assertEqual(result['confirmed'], case == 'native_success')
