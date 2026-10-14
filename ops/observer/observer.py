import sys
sys.dont_write_bytecode = True

import argparse
import asyncio
import functools
import json
import logging
import math
import os
import re
import ssl
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiohttp
from aiohttp import web
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import HistoryLimit, Journal, normalize
from history import readonly_history

HOSTS = tuple('192.168.3.' + str(n) for n in (180, 181, 183, 184))
DEVICE_IP = '192.168.3.24'
MODEL, DID, MAC = 'zhimi.airpurifier.m2', 70632677, '78:11:dc:47:87:bf'
PROPS = ('power', 'mode', 'favorite_level', 'motor1_speed', 'temp_dec')
ASSETS = {'/': ('ui.html', 'text/html'), '/ui.js': ('ui.js', 'application/javascript'),
          '/ui.css': ('ui.css', 'text/css')}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _empty(host=None):
    return dict(host=host, online=False, sampled_at=None, received_at=None,
                received_mono=None, source_ts=None, sensors=[], error='尚未取得读数')


def _fresh(sample, now, mono, source_required=True):
    received, source = sample.get('received_at'), sample.get('source_ts')
    received_mono = sample.get('received_mono')
    age = mono - received_mono if _finite(received_mono) else None
    source_age = now - source if _finite(source) else None
    skew = source - received if _finite(source) and _finite(received) else None
    fresh = bool(sample.get('online') and age is not None and 0 <= age <= 9
                 and (not source_required or source_age is not None and 0 <= source_age <= 9)
                 and (not source_required or skew is not None and skew <= 0)
                 and _finite(received) and received <= now)
    return {**{k: v for k, v in sample.items() if k not in ('received_mono', 'requested_mono')},
            'fresh': fresh, 'reception_age_seconds': age, 'source_age_seconds': source_age,
            'source_clock_skew_seconds': skew, 'source_timestamp_available': source_age is not None}


async def _poll_node(session, host, previous):
    started = time.monotonic()
    try:
        async with session.get('http://' + host + ':9100/status', allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError
            raw = bytearray()
            async for chunk in response.content.iter_chunked(32768):
                raw.extend(chunk)
                if len(raw) > 262144:
                    raise ValueError
            data = json.loads(raw)
        received = time.time()
        node = normalize(host, data, started, received)
        node.update(received_at=received, received_mono=time.monotonic(), error='')
        node['non_gpu_temp'] = max((s['temp'] for s in node['sensors']
                                    if s['group'] != 'gpu' and s['calibrated']), default=None)
        return node
    except Exception:
        return {**previous, 'online': False, 'error': '节点离线、超时或数据无效'}


def _regular_bytes(path, limit, mode=None):
    path = Path(os.path.abspath(str(path)))
    for parent in (path,) + tuple(path.parents):
        if parent.is_symlink():
            raise ValueError
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
            or mode is not None and stat.S_IMODE(before.st_mode) != mode):
        raise ValueError
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_mode != before.st_mode or (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise ValueError
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError
    return content


def _token(path):
    token = _regular_bytes(path, 128, 0o600).decode('ascii').strip()
    if not re.fullmatch('[a-fA-F0-9]{32}', token) or token.lower() in ('0' * 32, 'f' * 32):
        raise ValueError
    return token


def _sdk(ip, token, timeout):
    logging.getLogger('miio').setLevel(100)
    from miio import AirPurifier
    for name in list(logging.Logger.manager.loggerDict):
        if name == 'miio' or name.startswith('miio.'):
            logging.getLogger(name).disabled = True
    device = AirPurifier(ip=ip, token=token, timeout=timeout)
    device.retry_count = 0
    # python-miio 0.5.12 otherwise retries handshake three times independently.
    protocol = device._protocol
    protocol.send_handshake = functools.partial(protocol.send_handshake, retry_count=0)
    return device


def _purifier_read(state_path, factory=_sdk):
    device = factory(ip=DEVICE_IP, token=_token(Path(state_path) / 'device-token'), timeout=1)
    device.retry_count = 0
    info = device.info(skip_cache=True)
    if info.model != MODEL or device.device_id != DID or str(info.mac_address).lower() != MAC:
        raise ValueError('净化器身份不匹配')
    values = device.send('get_prop', list(PROPS), retry_count=0)
    if (not isinstance(values, list) or len(values) != 5 or values[0] not in ('on', 'off')
            or values[1] not in ('favorite', 'auto', 'silent', 'idle')
            or type(values[2]) is not int or not 0 <= values[2] <= 16
            or not _finite(values[3]) or not 0 <= values[3] <= 100000):
        raise ValueError('净化器读数无效')
    ambient = values[4] / 10 if _finite(values[4]) and -200 <= values[4] <= 800 else None
    return dict(online=True, power=values[0], mode=values[1], favorite_setting=values[2],
                actual_level=values[2] if values[0] == 'on' and values[1] == 'favorite' else None,
                rpm=values[3], ambient_c=ambient, ambient_source='purifier:70632677:temp_dec',
                room_temperature_verified=False, source_ts=None, received_at=time.time(),
                received_mono=time.monotonic(), error='')


def _project_history(result):
    samples = []
    for row in result['samples']:
        fan = row.get('fan', {})
        samples.append(dict(ts=row['ts'], snapshot_at=row.get('snapshot_at'),
                            requested_level=row.get('requested_level', row.get('decision', {}).get('level')),
                            fan={k: fan.get(k) for k in ('level', 'rpm', 'power', 'mode', 'verified_at',
                                                       'ambient_c', 'ambient_verified_at')},
                            reasons=row.get('decision', {}).get('reasons', []),
                            nodes=[{k: n.get(k) for k in ('host', 'online', 'sampled_at', 'source_ts',
                                    'cpu_temp', 'gpu_temp', 'board_temp', 'fresh', 'received_at',
                                    'reception_age_seconds', 'source_age_seconds', 'source_clock_skew_seconds')} for n in row.get('nodes', [])]))
    return dict(rows=result['rows'], samples=samples, samples_truncated=result['samples_truncated'],
                coverage='只读原始记录；缺失或失配索引使用预算内原始读取；格式无效记录可能被跳过',
                read_only=True, acquisition_paused=True)


@web.middleware
async def _get_only(request, handler):
    if request.method != 'GET':
        return web.Response(status=405, text='只读页面，仅允许 GET', headers={'Allow': 'GET'})
    if request.path not in (*ASSETS, '/api/status', '/api/history'):
        return web.Response(status=403, text='只读接口未开放此路径')
    response = await handler(request)
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'"})
    return response


def _safe_directory(path):
    path = Path(os.path.abspath(str(path)))
    for parent in (path,) + tuple(path.parents):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if (parent.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
                    or not stat.S_ISDIR(info.st_mode)):
                raise ValueError('目录不得包含链接、重解析点或普通文件')
    return path


def validate_record_root(state_path, record_root):
    state, records = _safe_directory(state_path), _safe_directory(record_root)
    if state == records or state in records.parents or records in state.parents:
        raise ValueError('观察历史目录不得与正式状态目录重叠')
    return records


def _validate_record_files(records):
    for path in records.iterdir():
        info = path.lstat()
        if (path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
                or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise ValueError('观察历史中出现链接或重解析点')


class RecordLock:
    def __init__(self, state_path, record_root):
        self.root = validate_record_root(state_path, record_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        validate_record_root(state_path, self.root)
        path = self.root / '.observer-writer.lock'
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                    or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_nlink != 1):
                raise ValueError('观察历史锁不是普通文件')
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b'0')
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def create_observer_app(state_path, asset_path, record_root):
    root, assets = Path(state_path), Path(asset_path)
    records = validate_record_root(root, record_root)
    control_reader = readonly_history(root / 'logs')
    observer_reader = readonly_history(records)
    app = web.Application(middlewares=[_get_only], client_max_size=1024)
    state = dict(nodes={h: _empty(h) for h in HOSTS}, purifier=_empty(), tasks=[],
                 history_future=None, motor_future=None, record_future=None, history_disk=None, motor_disk=None,
                 record_lock=None, journal=None, history_recording=dict(source='observer',
                 state='starting', writes=0, failed_writes=0, last_timestamp=None, error=None, retention_days=3,
                 max_bytes=64 * 1024 * 1024, learning_eligible=False))
    app['observer'] = state

    async def status(request):
        now, mono = time.time(), time.monotonic()
        return web.json_response(dict(read_only=True, control_active=False, requested_level=None,
            settings_enabled=False, qr_enabled=False, manual_enabled=False, generated_at=now,
            acquisition_paused=False, history_recording=dict(state['history_recording']),
            nodes=[_fresh(state['nodes'][h], now, mono) for h in HOSTS],
            purifier=_fresh(state['purifier'], now, mono, source_required=False)), dumps=_json)

    async def history(request):
        try:
            source = request.query.get('source', 'observer')
            if source not in ('observer', 'control'):
                raise ValueError
            reader = observer_reader if source == 'observer' else control_reader
            hours = float(request.query.get('hours', 24))
            now = time.time()
            until = float(request.query.get('until', now))
            if hours not in (1, 3, 6, 24) or not _finite(until) or not 0 <= until <= now:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            return web.json_response({'error': '请选择 1/3/6/24 小时和非未来的结束时间'}, status=400)
        if state['history_future'] is not None and not state['history_future'].done():
            return web.json_response({'error': '历史正在读取，请稍后重试'}, status=429)
        since = max(0, until - hours * 3600)
        future = asyncio.get_running_loop().run_in_executor(state['history_disk'], reader.history, since, until)
        state['history_future'] = future
        future.add_done_callback(_consume_exception)
        try:
            result = await asyncio.shield(future)
            projected = _project_history(result)
            if source == 'observer':
                for sample in projected['samples']:
                    sample.update(source='observer', control_active=False, learning_eligible=False)
            return web.json_response(dict(projected, source=source,
                acquisition_paused=source == 'control', history_recording=dict(state['history_recording']),
                **{'from': since, 'to': until}), dumps=_json)
        except HistoryLimit:
            return web.json_response({'error': '历史读取超出预算，请缩小时间范围或选择更早的结束时间',
                                      'budget_refused': True}, status=503)
        except Exception:
            return web.json_response({'error': '历史目录缺失、不可读或格式无效；未修改文件'}, status=503)

    async def asset(request):
        name, content_type = ASSETS[request.path]
        return web.Response(body=(assets / name).read_bytes(), content_type=content_type)

    def write_snapshot(row):
        validate_record_root(root, records)
        _validate_record_files(records)
        journal = state['journal']
        journal.prune(row['ts'], 3, 64 * 1024 * 1024)
        journal.append(row)
        journal.prune(row['ts'], 3, 64 * 1024 * 1024)

    async def record_snapshot():
        stamp, mono = time.time(), time.monotonic()
        purifier = _fresh(state['purifier'], stamp, mono, source_required=False)
        fan = {key: purifier.get(prop) if purifier['fresh'] else None for key, prop in (
            ('level', 'actual_level'), ('rpm', 'rpm'), ('power', 'power'), ('mode', 'mode'),
            ('verified_at', 'received_at'), ('ambient_c', 'ambient_c'))}
        row = dict(ts=stamp, snapshot_at=stamp, source='observer', control_active=False,
                   learning_eligible=False, requested_level=None, fan=fan,
                   nodes=[_fresh(state['nodes'][h], stamp, mono) for h in HOSTS])
        future = asyncio.get_running_loop().run_in_executor(state['history_disk'], write_snapshot, row)
        state['record_future'] = future
        future.add_done_callback(_consume_exception)
        try:
            await asyncio.shield(future)
            state['history_recording'].update(state='recording',
                writes=state['history_recording']['writes'] + 1, last_timestamp=stamp, error=None)
        except Exception:
            state['history_recording'].update(state='failed',
                failed_writes=state['history_recording']['failed_writes'] + 1,
                error='观察历史持久写入失败；采集不保证连续')

    async def nodes_loop():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5), trust_env=False) as session:
            while True:
                started = time.monotonic()
                values = await asyncio.gather(*(_poll_node(session, h, state['nodes'][h]) for h in HOSTS))
                state['nodes'] = dict(zip(HOSTS, values))
                await record_snapshot()
                await asyncio.sleep(max(0, 3 - (time.monotonic() - started)))

    async def purifier_loop():
        while True:
            started = time.monotonic()
            future = asyncio.get_running_loop().run_in_executor(state['motor_disk'], _purifier_read, root)
            state['motor_future'] = future
            future.add_done_callback(_consume_exception)
            try:
                state['purifier'] = await asyncio.shield(future)
            except Exception:
                state['purifier'] = {**state['purifier'], 'online': False,
                                     'error': '净化器 SDK 不可用、凭据无效、身份不符或读回失败'}
            await asyncio.sleep(max(0, 8 - (time.monotonic() - started)))

    async def startup(application):
        state['record_lock'] = RecordLock(root, records)
        try:
            _validate_record_files(records)
            state['journal'] = Journal(records)
        except BaseException:
            state['record_lock'].close()
            raise
        state['history_disk'] = ThreadPoolExecutor(max_workers=1, thread_name_prefix='readonly-history')
        state['motor_disk'] = ThreadPoolExecutor(max_workers=1, thread_name_prefix='readonly-query')
        state['tasks'] = [asyncio.create_task(nodes_loop()), asyncio.create_task(purifier_loop())]

    async def cleanup(application):
        for task in state['tasks']:
            task.cancel()
        await asyncio.gather(*state['tasks'], return_exceptions=True)
        futures = [f for f in (state['history_future'], state['motor_future'], state['record_future']) if f is not None]
        await asyncio.gather(*(asyncio.shield(f) for f in futures), return_exceptions=True)
        for key in ('history_disk', 'motor_disk'):
            state[key].shutdown(wait=False)
        state['record_lock'].close()
        state['history_recording']['state'] = 'stopped'

    for path in ASSETS:
        app.router.add_get(path, asset, allow_head=False)
    app.router.add_get('/api/status', status, allow_head=False)
    app.router.add_get('/api/history', history, allow_head=False)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _consume_exception(future):
    if not future.cancelled():
        future.exception()


def _preflight():
    gate = json.loads(_regular_bytes('/home/bestvcboy/dgx-thermal-release-tools/admission.json', 65536))
    if not isinstance(gate, dict) or gate.get('state') != 'blocked':
        raise ValueError('控制准入必须保持 blocked')
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'dgx-thermal.service',
                             '--property=MainPID', '--property=ActiveState'],
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
    fields = dict(line.split('=', 1) for line in result.stdout.decode('ascii').splitlines())
    if fields.get('MainPID') != '0':
        raise ValueError('控制进程仍在运行')


def main():
    parser = argparse.ArgumentParser(description='DGX 只读观察器（无控制能力）')
    parser.add_argument('--state', type=Path, default=Path('/home/bestvcboy/dgx-thermal/state'))
    parser.add_argument('--record-root', type=Path, required=True)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    validate_record_root(args.state, args.record_root)
    if args.preflight:
        _preflight()
        return
    import fcntl
    state_fd = os.open(str(args.state), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fcntl.flock(state_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    _preflight()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(args.state / 'tls.crt'), str(args.state / 'tls.key'))
    web.run_app(create_observer_app(args.state, Path(__file__).resolve().parent, args.record_root),
                host='0.0.0.0', port=8910, ssl_context=context, access_log=None)


if __name__ == '__main__':
    main()
