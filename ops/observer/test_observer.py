import asyncio
import json
import io
import logging
import os
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import core
import observer
from history import readonly_history
from aiohttp.test_utils import TestClient, TestServer

HERE = Path(__file__).parent


class HistoryFreshnessTests(unittest.TestCase):
    def test_recorded_freshness_and_age_survive_projection(self):
        node = dict(host='192.168.3.180', online=True, fresh=False, source_ts=1,
                    reception_age_seconds=0, source_age_seconds=99, received_at=100,
                    source_clock_skew_seconds=99, cpu_temp=51)
        result = observer._project_history(dict(samples=[dict(ts=100, nodes=[node])],
                                                rows=1, samples_truncated=False))
        projected = result['samples'][0]['nodes'][0]
        for key in ('fresh', 'source_age_seconds', 'reception_age_seconds', 'received_at'):
            self.assertEqual(projected[key], node[key])


def telemetry(stamp=100):
    return {'timestamp': stamp, 'token': 'NEVER_PUBLIC', 'settings': {'secret': 'NEVER_PUBLIC'},
            'temperatures': {'cpu': [{'temp': 51, 'label': 'CPU'}],
                             'nvme': [{'temp': 61, 'label': 'disk'}],
                             'spbm': [{'temp': 149, 'label': 'uncalibrated'}], 'gpu': 55},
            'system_power_w': 80, 'system_power_source': 'spbm'}


class FakeResponse:
    def __init__(self, body, status=200):
        self.status, self.body = status, body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class FakeSession:
    def __init__(self, response=None, **kwargs):
        self.response = response
        self.kwargs = kwargs
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeDevice:
    def __init__(self, model=observer.MODEL, did=observer.DID, mac=observer.MAC, values=None):
        self.identity = types.SimpleNamespace(model=model, mac_address=mac)
        self.device_id, self.calls = did, []
        self.values = values or ['on', 'favorite', 16, 2095, 312]
        self.retry_count = 3

    def info(self, **kwargs):
        self.calls.append(('info', kwargs, self.retry_count))
        return self.identity

    def send(self, command, props, **kwargs):
        self.calls.append((command, props, kwargs))
        return self.values


class ReadonlyHistoryTests(unittest.TestCase):
    def test_factory_bypasses_constructor_and_preserves_scratch(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            scratch = root / 'thermal-work-abcdefgh'
            scratch.write_bytes(b'evidence')
            raw = root / 'thermal-00000000000000000001.jsonl'
            raw.write_text(json.dumps({'ts': 100, 'nodes': [], 'requested_level': 12}) + '\n')
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
            with patch.object(core.Journal, '__init__', side_effect=AssertionError('mutating constructor')):
                view = readonly_history(root)
                self.assertEqual(view.history(0, 200)['samples'][0]['requested_level'], 12)
            for forbidden in ('append', 'bounds', 'prune', 'files', 'read', '_publish'):
                self.assertFalse(hasattr(view, forbidden))
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()})

    def test_missing_root_is_not_created(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder) / 'missing'
            view = readonly_history(root)
            with self.assertRaises(FileNotFoundError):
                view.history(0, 200)
            self.assertFalse(root.exists())

    def test_malformed_lines_and_missing_certificate_are_readonly(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            raw = root / 'thermal-00000000000000000001.jsonl'
            raw.write_text('broken\n[]\n' + json.dumps({'ts': 100, 'nodes': []}) + '\n')
            (root / (raw.name + '.bounds.json')).write_text('{malformed')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            self.assertEqual(readonly_history(root).history(0, 200)['samples'][0]['ts'], 100)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})



class PurifierTests(unittest.TestCase):
    def read(self, device):
        calls = []
        def factory(**kwargs):
            calls.append(kwargs)
            return device
        with patch.object(observer, '_token', return_value='1' * 32):
            result = observer._purifier_read('fake', factory)
        self.assertEqual(calls, [dict(ip=observer.DEVICE_IP, token='1' * 32, timeout=1)])
        return result

    def test_exact_identity_query_trace_and_no_actuation_ack(self):
        device = FakeDevice()
        result = self.read(device)
        self.assertEqual(device.calls, [('info', {'skip_cache': True}, 0),
            ('get_prop', list(observer.PROPS), {'retry_count': 0})])
        self.assertEqual((result['actual_level'], result['rpm'], result['ambient_c']), (16, 2095, 31.2))
        self.assertFalse(result['room_temperature_verified'])
        self.assertNotIn('confirmed', result)
        self.assertNotIn('1' * 32, json.dumps(result))

    def test_identity_mismatch_never_calls_get_prop(self):
        for kwargs in ({'model': 'other'}, {'did': 1}, {'mac': '00:00:00:00:00:00'}):
            device = FakeDevice(**kwargs)
            with self.assertRaises(ValueError):
                self.read(device)
            self.assertEqual([c[0] for c in device.calls], ['info'])

    def test_auto_silent_and_off_do_not_claim_actual_gear(self):
        for power, mode in [('on', 'auto'), ('on', 'silent'), ('off', 'favorite')]:
            result = self.read(FakeDevice(values=[power, mode, 16, 500, 301]))
            self.assertIsNone(result['actual_level'])
            self.assertEqual(result['favorite_setting'], 16)

    def test_invalid_properties_rejected(self):
        for values in (['on', 'favorite', True, 900, 300], ['on', 'NEVER_PUBLIC', 16, 900, 300],
                       ['on', 'favorite', 16, float('nan'), 300], ['on', 'favorite', 16, 900]):
            with self.assertRaises(ValueError):
                self.read(FakeDevice(values=values))

    def test_sdk_handshake_no_retries_and_logs_suppressed(self):
        logger = logging.getLogger('miio.fake-observer-test')
        out = io.StringIO()
        handler = logging.StreamHandler(out)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        trace = []
        def sdk(**kwargs):
            logger.error('TOKEN-NEVER-PUBLIC')
            device = FakeDevice()
            def handshake(*, retry_count=3):
                trace.append(retry_count)
            device._protocol = types.SimpleNamespace(send_handshake=handshake)
            return device
        try:
            with patch.dict(sys.modules, {'miio': types.SimpleNamespace(AirPurifier=sdk)}):
                device = observer._sdk(observer.DEVICE_IP, '1' * 32, 1)
                device._protocol.send_handshake()
            self.assertEqual(trace, [0])
            self.assertEqual(device.retry_count, 0)
            self.assertEqual(out.getvalue(), '')
        finally:
            logger.removeHandler(handler)

    def test_token_refuses_oversize_bad_value_and_symlink(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            path = Path(folder) / 'device-token'
            path.write_bytes(b'x' * 129)
            with self.assertRaises(ValueError):
                observer._token(path)
            with patch.object(observer, '_regular_bytes', return_value=b'0' * 32):
                with self.assertRaises(ValueError):
                    observer._token(path)
            with patch.object(observer.Path, 'is_symlink', return_value=True):
                with self.assertRaises(ValueError):
                    observer._token(path)

    @unittest.skipUnless(sys.platform != 'win32', 'Windows stat does not expose POSIX 0600')
    def test_real_token_mode_600_readonly(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            path = Path(folder) / 'device-token'
            path.write_text('1' * 32)
            path.chmod(0o600)
            self.assertEqual(observer._token(path), '1' * 32)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                observer._token(path)


class PollTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalize_whitelist_non_gpu_calibration_and_fixed_get(self):
        session = FakeSession(FakeResponse(json.dumps(telemetry()).encode()))
        with patch.object(observer.time, 'time', return_value=100):
            result = await observer._poll_node(session, observer.HOSTS[0], observer._empty())
        self.assertEqual(session.calls, [('http://192.168.3.180:9100/status', {'allow_redirects': False})])
        self.assertEqual(result['non_gpu_temp'], 61)
        self.assertEqual(result['gpu_temp'], 55)
        self.assertEqual(len(result['sensors']), 4)
        self.assertNotIn('NEVER_PUBLIC', json.dumps(result))

    async def test_failed_poll_does_not_refresh_timestamp_or_secrets(self):
        previous = dict(observer._empty(), online=True, received_at=100, source_ts=99, received_mono=10)
        for response in (RuntimeError('TOKEN-NEVER-PUBLIC'), FakeResponse(b'x' * 262145),
                         FakeResponse(b'{}', 302), FakeResponse(b'bad'), FakeResponse(b'{}')):
            result = await observer._poll_node(FakeSession(response), observer.HOSTS[0], previous)
            self.assertEqual((result['received_at'], result['source_ts'], result['received_mono']), (100, 99, 10))
            self.assertFalse(result['online'])
            self.assertNotIn('TOKEN-NEVER-PUBLIC', json.dumps(result))

    async def test_freshness_reception_source_and_future_skew(self):
        sample = dict(observer._empty(), online=True, received_at=100, received_mono=10, source_ts=100)
        self.assertTrue(observer._fresh(sample, 109, 19)['fresh'])
        for changes, now, mono in [({}, 109.01, 19.01), ({'source_ts': 90}, 100, 10),
                                  ({'source_ts': 101}, 102, 12), ({'source_ts': None}, 100, 10),
                                  ({'received_mono': 11}, 100, 10), ({'online': False}, 100, 10)]:
            self.assertFalse(observer._fresh(dict(sample, **changes), now, mono)['fresh'])
        result = observer._fresh(dict(sample, source_ts=101), 102, 12)
        self.assertEqual(result['source_clock_skew_seconds'], 1)
        self.assertTrue(observer._fresh(dict(sample, source_ts=None), 100, 10, False)['fresh'])


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.record_temp = tempfile.TemporaryDirectory(dir=HERE)
        self.records = Path(self.record_temp.name)
        self.root = Path(self.temp.name)
        logs = self.root / 'logs'
        logs.mkdir()
        self.stamp = time.time() - 60
        row = {'ts': self.stamp, 'requested_level': 12, 'token': 'NEVER_PUBLIC',
               'decision': {'level': 12, 'reasons': ['historical reason'], 'trial': {'token': 'NEVER_PUBLIC'}},
               'fan': {'level': 16, 'rpm': 2095, 'power': 'on', 'mode': 'favorite', 'error': 'NEVER_PUBLIC',
                       'ambient_c': 31.2, 'ambient_verified_at': self.stamp},
               'nodes': [{'host': h, 'online': True, 'cpu_temp': 50, 'gpu_temp': 55, 'board_temp': 60,
                          'token': 'NEVER_PUBLIC'} for h in observer.HOSTS]}
        (logs / 'thermal-00000000000000000001.jsonl').write_text(json.dumps(row) + '\n')
        (logs / 'thermal-work-abcdefgh').write_bytes(b'evidence')
        (self.root / 'learning-checkpoint.json').write_bytes(b'learning evidence')
        (self.root / 'settings.json').write_bytes(b'settings evidence')
        self.before = self.manifest()
        self.polls = []
        real_poll = observer._poll_node
        async def poll(session, host, previous):
            self.polls.append(host)
            response = FakeResponse(json.dumps(telemetry(time.time())).encode())
            return await real_poll(FakeSession(response), host, previous)
        self.session_factory = patch.object(observer.aiohttp, 'ClientSession', FakeSession)
        self.node_patch = patch.object(observer, '_poll_node', poll)
        self.motor_patch = patch.object(observer, '_purifier_read', side_effect=RuntimeError('TOKEN-NEVER-PUBLIC'))
        self.session_factory.start(); self.node_patch.start(); self.motor_patch.start()
        self.app = observer.create_observer_app(self.root, HERE, self.records)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        for _ in range(100):
            if self.app['observer']['history_recording']['writes']:
                break
            await asyncio.sleep(.01)

    def manifest(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    async def asyncTearDown(self):
        await self.client.close()
        self.session_factory.stop(); self.node_patch.stop(); self.motor_patch.stop()
        self.assertEqual(self.before, self.manifest())
        self.temp.cleanup()
        self.record_temp.cleanup()

    async def test_get_status_history_assets_and_preservation(self):
        status, history = await asyncio.gather(self.client.get('/api/status'), self.client.get('/api/history?source=control'))
        live, past = await status.json(), await history.json()
        self.assertEqual((status.status, history.status), (200, 200))
        self.assertEqual([n['host'] for n in live['nodes']], list(observer.HOSTS))
        self.assertEqual(set(self.polls), set(observer.HOSTS))
        self.assertFalse(live['control_active'])
        self.assertIsNone(live['requested_level'])
        self.assertTrue(live['read_only'])
        for flag in ('settings_enabled', 'qr_enabled', 'manual_enabled'):
            self.assertFalse(live[flag])
        self.assertFalse(live['purifier']['online'])
        self.assertEqual(past['samples'][0]['ts'], self.stamp)
        self.assertEqual(past['samples'][0]['fan']['rpm'], 2095)
        self.assertEqual(past['samples'][0]['fan']['ambient_c'], 31.2)
        self.assertEqual(past['samples'][0]['fan']['ambient_verified_at'], self.stamp)
        self.assertGreaterEqual(past['to'] - past['from'], 86400 - .01)
        self.assertNotIn('NEVER_PUBLIC', json.dumps([live, past]))
        for path, (name, _) in observer.ASSETS.items():
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), (HERE / name).read_bytes())

    async def test_all_non_get_methods_denied_even_unknown_routes(self):
        for path in ('/', '/api/status', '/api/history?source=control', '/api/settings', '/api/fan',
                     '/api/xiaomi/qr/bind', '/unknown', '/core.py', '/device-token'):
            for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
                response = await self.client.request(method, path, data=b'NEVER_PUBLIC')
                self.assertEqual(response.status, 405, (method, path))
                self.assertEqual(response.headers['Allow'], 'GET')
        for path in ('/unknown', '/core.py', '/device-token', '/tls.key', '/ui.html', '/ui.js/extra'):
            self.assertEqual((await self.client.get(path)).status, 403)

    async def test_history_budget_refusal_not_fake_empty(self):
        with patch.object(core.Journal, 'HISTORY_BYTES', 0):
            response = await self.client.get('/api/history?source=control')
            result = await response.json()
        self.assertEqual(response.status, 503)
        self.assertTrue(result['budget_refused'])
        self.assertNotIn('samples', result)

    async def test_missing_history_directory_and_bad_query(self):
        for query in ('hours=nan', 'hours=2', 'until=nan', 'until=999999999999'):
            self.assertEqual((await self.client.get('/api/history?source=control&' + query)).status, 400)
        with patch.object(core.Journal, 'history', side_effect=FileNotFoundError('TOKEN-NEVER-PUBLIC')):
            response = await self.client.get('/api/history?source=control')
            self.assertEqual(response.status, 503)
            self.assertNotIn('TOKEN-NEVER-PUBLIC', await response.text())

    async def test_cancelled_history_keeps_admission_until_executor_finishes(self):
        started, release = threading.Event(), threading.Event()
        original = core.Journal.history
        def blocked(reader, *args):
            started.set()
            if not release.wait(2):
                raise AssertionError('fixture was not released')
            return original(reader, *args)
        handler = next(route.handler for route in self.app.router.routes()
                       if route.resource.canonical == '/api/history')
        with patch.object(core.Journal, 'history', blocked):
            task = asyncio.create_task(handler(types.SimpleNamespace(query={'source': 'control'})))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.005)
                self.assertTrue(started.is_set())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(self.app['observer']['history_future'].done())
                status = await self.client.get('/api/status')
                self.assertEqual(status.status, 200)
                self.assertEqual((await self.client.get('/api/history?source=control')).status, 429)
            finally:
                release.set()
                await asyncio.shield(self.app['observer']['history_future'])
        self.assertEqual((await self.client.get('/api/history?source=control&hours=1')).status, 200)

    async def test_startup_and_cleanup_drain_only_observation(self):
        started, release = threading.Event(), threading.Event()
        def blocked_read(root):
            started.set()
            release.wait(2)
            raise ModuleNotFoundError('optional SDK absent')
        other_records = self.records / 'other'
        client = TestClient(TestServer(observer.create_observer_app(self.root, HERE, other_records)))
        closing = None
        with patch.object(observer, '_purifier_read', blocked_read):
            try:
                await asyncio.wait_for(client.start_server(), .5)
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.005)
                self.assertTrue(started.is_set())
                self.assertEqual((await client.get('/api/status')).status, 200)
                closing = asyncio.create_task(client.close())
                await asyncio.sleep(.03)
                self.assertFalse(closing.done(), 'cleanup must not orphan a running SDK query')
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 1)
                else:
                    await client.close()

    async def test_newest_200_historical_samples_keep_original_times(self):
        log = self.root / 'logs' / 'thermal-00000000000000000001.jsonl'
        original = log.read_bytes()
        stamp = time.time() - 1000
        rows = [{'ts': stamp + i, 'requested_level': 10, 'nodes': []} for i in range(205)]
        try:
            log.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            response = await self.client.get('/api/history?source=control&hours=1')
            result = await response.json()
            self.assertEqual(response.status, 200)
            self.assertTrue(result['samples_truncated'])
            self.assertEqual([r['ts'] for r in result['samples']], [stamp + i for i in range(204, 4, -1)])
        finally:
            log.write_bytes(original)
            import os
            before_mtime = self.before[str(log.relative_to(self.root))][1]
            os.utime(str(log), ns=(log.stat().st_atime_ns, before_mtime))

    async def test_observer_records_are_persisted_independently(self):
        response = await self.client.get('/api/history?source=observer')
        past = await response.json()
        live = await (await self.client.get('/api/status')).json()
        self.assertEqual(response.status, 200)
        self.assertEqual(past['source'], 'observer')
        self.assertGreaterEqual(live['history_recording']['writes'], 1)
        self.assertFalse(live['history_recording']['learning_eligible'])
        raw = [json.loads(line) for p in self.records.glob('thermal-*.jsonl')
               for line in p.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(raw)
        self.assertTrue(all(row['source'] == 'observer' and not row['control_active']
                            and not row['learning_eligible'] for row in raw))
        self.assertTrue(all(row['requested_level'] is None for row in raw))
        self.assertEqual(len(raw[0]['nodes']), 4)
        self.assertTrue(past['samples'])

    async def test_disk_failure_visible_and_protected_state_preserved(self):
        await self.client.close()
        with patch.object(core.Journal, 'append', side_effect=OSError('SECRET DISK')):
            self.app = observer.create_observer_app(self.root, HERE, self.records)
            self.client = TestClient(TestServer(self.app))
            await self.client.start_server()
            for _ in range(100):
                if self.app['observer']['history_recording']['state'] == 'failed':
                    break
                await asyncio.sleep(.01)
            result = await (await self.client.get('/api/status')).json()
            self.assertEqual(result['history_recording']['state'], 'failed')
            self.assertEqual(result['history_recording']['writes'], 0)
            self.assertEqual(result['history_recording']['failed_writes'], 1)
            self.assertNotIn('SECRET DISK', json.dumps(result))


class RecordBoundaryTests(unittest.TestCase):
    def test_overlap_and_reparse_rejected(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            state = Path(folder) / 'state'
            for records in (state, state / 'logs', Path(folder)):
                with self.assertRaises(ValueError):
                    observer.validate_record_root(state, records)
            state.mkdir()
            with patch.object(observer.Path, 'is_symlink', return_value=True):
                with self.assertRaises(ValueError):
                    observer.validate_record_root(state, Path(folder) / 'record')

    def test_windows_reparse_attributes_rejected(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            with patch.object(observer.Path, 'lstat', return_value=types.SimpleNamespace(
                    st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
                with self.assertRaises(ValueError):
                    observer.validate_record_root(Path(folder), Path(folder) / 'other')

    def test_writer_lock_is_exclusive_until_closed(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            state, records = Path(folder) / 'state', Path(folder) / 'records'
            first = observer.RecordLock(state, records)
            try:
                with self.assertRaises(OSError):
                    observer.RecordLock(state, records)
            finally:
                first.close()
            second = observer.RecordLock(state, records)
            second.close()

    def test_hardlinked_history_rejected_before_append(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root = Path(folder)
            records = root / 'records'
            records.mkdir()
            protected = root / 'protected'
            protected.write_bytes(b'evidence')
            os.link(protected, records / 'thermal-00000000000000000001.jsonl')
            with self.assertRaises(ValueError):
                observer._validate_record_files(records)
            self.assertEqual(protected.read_bytes(), b'evidence')



class LauncherTests(unittest.TestCase):
    def test_boot_preflight_requires_blocked_and_no_mainpid_not_exact_inactive(self):
        for active in ('inactive', 'failed', 'activating'):
            result = types.SimpleNamespace(stdout=('MainPID=0\nActiveState=' + active + '\n').encode())
            with patch.object(observer, '_regular_bytes', return_value=b'{"state":"blocked"}') as read, \
                    patch.object(observer.subprocess, 'run', return_value=result) as run:
                observer._preflight()
            self.assertEqual(read.call_args[0][0], '/home/bestvcboy/dgx-thermal-release-tools/admission.json')
            self.assertEqual(run.call_args[0][0], ['/usr/bin/systemctl', 'show', 'dgx-thermal.service',
                                                 '--property=MainPID', '--property=ActiveState'])
        for data, output in [(b'{"state":"admitted"}', b'MainPID=0\n'),
                             (b'{"state":"blocked"}', b'MainPID=100\n'),
                             (b'{"state":"blocked"}', b'ActiveState=inactive\n')]:
            with patch.object(observer, '_regular_bytes', return_value=data), \
                    patch.object(observer.subprocess, 'run', return_value=types.SimpleNamespace(stdout=output)):
                with self.assertRaises(ValueError):
                    observer._preflight()

    def test_unit_only_writes_external_observer_history(self):
        unit = (HERE / 'dgx-thermal-observer-candidate.service').read_text()
        self.assertIn('ReadOnlyPaths=/home/bestvcboy/dgx-thermal/state', unit)
        self.assertIn('ReadWritePaths=/home/bestvcboy/dgx-thermal-observer-history', unit)
        self.assertIn('--record-root /home/bestvcboy/dgx-thermal-observer-history', unit)
        self.assertNotIn('Type=notify', unit)

    def test_fixed_session_policy_nonblocking_startup_and_cleanup_shapes(self):
        source = (HERE / 'observer.py').read_text(encoding='utf-8')
        self.assertIn('ClientTimeout(total=2.5), trust_env=False', source)
        self.assertIn('fcntl.LOCK_SH | fcntl.LOCK_NB', source)
        self.assertIn('context.minimum_version = ssl.TLSVersion.TLSv1_2', source)
        self.assertNotIn('from service', source)


if __name__ == '__main__':
    unittest.main()

