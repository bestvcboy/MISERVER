import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import service
from learning import Learning, SLOT
from test_learning import SETTINGS, frame, populated_learning
from test_learning_service import closure


class LearningArchiveTests(unittest.TestCase):
    def test_missing_archive_is_measured_without_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(service.learning_archive_usage(root),
                             dict(checkpoint_bytes=0, preserved_bytes=0, reserved_bytes=0))
            self.assertFalse((root / 'learning').exists())

    def test_owned_checkpoint_and_pending_are_preserved_and_fully_accounted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'learning'
            archive.mkdir(mode=0o700)
            (archive / 'checkpoint.json').write_bytes(b'invalid historical schema')
            (archive / '.pending-abcdefgh').write_bytes(b'pending')
            before = {path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                      for path in archive.iterdir()}
            self.assertEqual(service.learning_archive_usage(root),
                             dict(checkpoint_bytes=25, preserved_bytes=32, reserved_bytes=32))
            self.assertEqual({path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                              for path in archive.iterdir()}, before)

    def test_archive_rejects_unexpected_name_directory_hardlink_and_oversize(self):
        for failure in ('name', 'directory', 'hardlink', 'oversize', 'count'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / 'learning'
                archive.mkdir()
                path = archive / 'checkpoint.json'
                path.write_bytes(b'checkpoint')
                if failure == 'name':
                    (archive / 'other').write_bytes(b'x')
                elif failure == 'directory':
                    (archive / '.pending-abcdefgh').mkdir()
                elif failure == 'hardlink':
                    os.link(path, archive / '.pending-abcdefgh')
                elif failure == 'oversize':
                    with path.open('r+b') as stream:
                        stream.truncate(4 * 1024**2 + 1)
                else:
                    for index in range(128):
                        (archive / f'.pending-{index:08d}').write_bytes(b'x')
                with self.assertRaises(ValueError):
                    service.learning_archive_usage(root)
                self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == 'posix', 'symlink creation requires native Unix filesystem')
    def test_archive_rejects_symlinks_and_wrong_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'learning').mkdir()
            (root / 'learning' / 'checkpoint.json').symlink_to(root / 'outside')
            with self.assertRaises(ValueError):
                service.learning_archive_usage(root)
            (root / 'learning' / 'checkpoint.json').unlink()
            with patch('service.os.getuid', return_value=os.getuid() + 1):
                with self.assertRaises(ValueError):
                    service.learning_archive_usage(root)


class RulesModeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.end = int(time.time() // SLOT) * SLOT
        learner = populated_learning(14, cls.end)
        cls.checkpoint = learner.checkpoint(cls.end, SETTINGS, cls.end - SLOT)
        cls.policy = json.loads(cls.checkpoint)['published']

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        archive = self.root / 'learning'
        archive.mkdir(mode=0o700)
        (archive / 'checkpoint.json').write_bytes(self.checkpoint.encode())
        (archive / '.pending-abcdefgh').write_bytes(b'keep interrupted checkpoint')
        self.before = {path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                       for path in archive.iterdir()}
        (self.root / 'settings.json').write_bytes(json.dumps(SETTINGS).encode())
        self.patches = ExitStack()
        self.forbidden = []
        for name in ('restore', 'trim', 'observe', 'checkpoint', 'fail', 'status'):
            self.forbidden.append(self.patches.enter_context(patch.object(
                Learning, name, side_effect=AssertionError(f'learning {name} must remain disabled'))))
        self.forbidden.append(self.patches.enter_context(patch('service.Learning', side_effect=AssertionError('learner construction'))))
        self.forbidden.append(self.patches.enter_context(patch('service.context_of', side_effect=AssertionError('learned context'))))
        self.forbidden.append(self.patches.enter_context(patch('service.checkpoint_limit', side_effect=AssertionError('training budget'))))
        self.patches.enter_context(patch('service.fsync_directory'))
        self.app = await service.create_app(self.root, high_wind_gate=False)
        self.state = self.app[service.STATE]
        self.functions = closure(self.app.on_startup[-1])
        self.storage = self.functions['storage']
        self.worker = asyncio.create_task(self.storage())
        self.state['tasks'] = [self.worker]

    async def asyncTearDown(self):
        if not self.state['stop']:
            await self.app.on_cleanup[-1](self.app)
        for spy in self.forbidden:
            spy.assert_not_called()
        self.assertEqual({path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                          for path in (self.root / 'learning').iterdir()}, self.before)
        self.patches.close()
        self.temp.cleanup()

    def handler(self, path):
        return next(route.handler for route in self.app.router.routes() if route.resource.canonical == path)

    async def write(self):
        row = frame(time.time(), gear=16)
        await self.state['queue'].put(row)
        await asyncio.wait_for(self.state['queue'].join(), 3)
        return row

    async def test_default_storage_settings_status_and_shutdown_preserve_old_learning_bytes(self):
        self.assertTrue(self.state["controller"].rules_only)
        self.assertEqual(len(self.policy), 2)
        row = await self.write()
        self.assertEqual(self.state['history_recording']['writes'], 1)
        self.assertEqual(self.state['storage_error'], '')
        stored = json.loads(self.state['journal'].files()[-1].read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(stored, row)
        usage = service.learning_archive_usage(self.root)
        self.assertEqual(self.state['log_bytes'], self.state['journal'].size() + usage['preserved_bytes'])
        response = await self.handler('/api/settings')(SimpleNamespace(json=AsyncMock(return_value={
            'retention_days': 3, 'max_log_mb': 2048, 'manual_level': None})))
        self.assertEqual(response.status, 200)
        status = json.loads((await self.handler('/api/status')(None)).body)
        self.assertEqual(status['strategy_mode'], 'rules')
        self.assertEqual((status['learning']['enabled'], status['learning']['reason']), (False, 'disabled'))
        self.assertEqual((status['learning']['published'], status['learning']['active'],
                          status['learning']['candidate']), ({}, {}, {}))
        self.assertIsNone(status['learning']['next_at'])
        self.assertEqual(status['learning']['preserved_bytes'], usage['preserved_bytes'])
        await self.app.on_cleanup[-1](self.app)

    async def test_valid_old_threshold_never_reaches_controller(self):
        from test_service import OfflineSession
        from test_scenarios import feedback
        self.state['learning'] = dict(self.state['learning'], published=self.policy,
                                      settings_revision=0)
        mono, wall = time.monotonic(), time.time()
        nodes = frame(wall)['nodes']
        for node in nodes:
            node.update(requested_mono=mono, sampled_at=wall, source_ts=wall)
        by_host = {node['host']: node for node in nodes}
        self.state.update(learning_wall=wall, last_poll=mono, last_fan=mono, nodes=nodes)
        self.state['fan'] = dict(feedback(mono, 16), verified_at=wall)
        self.state['decision'] = {'level': 16}
        async def stop_after_frame(delay):
            self.state['stop'] = True
        with patch('service.aiohttp.ClientSession', OfflineSession), \
                patch('service.normalize', side_effect=lambda host, *args: by_host[host]), \
                patch('service.asyncio.sleep', stop_after_frame), \
                patch.object(self.state['controller'], 'decide', return_value={'level': 16, 'learning_eligible': True}) as decide:
            await self.functions['monitor']()
        self.state['stop'] = False
        self.assertEqual(decide.call_args.kwargs['power_thresholds'], {})
        self.assertTrue(all(node['online'] and node['power_w'] == 35
                            for node in decide.call_args.args[0]))
        await asyncio.wait_for(self.state['queue'].join(), 3)
        raw = json.loads(self.state['journal'].files()[-1].read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(raw['active_power_policy'], {})
        self.assertFalse(raw['decision']['learning_eligible'])
        self.assertFalse(raw['learning']['healthy'])

    async def test_archive_bytes_are_reserved_before_log_prune_and_storage_failures_still_protect(self):
        usage = service.learning_archive_usage(self.root)
        prune = self.state['journal'].prune
        with patch.object(self.state['journal'], 'prune', wraps=prune) as measured:
            row = await self.write()
        encoded = len(json.dumps(row, ensure_ascii=False).encode()) + 1
        self.assertEqual(measured.call_args.args[2], SETTINGS['max_log_mb'] * 1024**2
                         - encoded - service.Journal.SEGMENT_BYTES - usage['preserved_bytes'])
        with patch.object(self.state['journal'], 'append', side_effect=OSError('disk unavailable')):
            await self.write()
        self.assertEqual(self.state['history_recording']['failed_writes'], 1)
        self.assertEqual(self.state['decision']['level'], 16)
        self.assertTrue(self.state['storage_error'])


if __name__ == '__main__':
    unittest.main()
