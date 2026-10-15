import argparse
import hashlib
import json
import os
import re
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import check
from core import Journal
from service import MotorSchedule, learning_archive_usage, settings_from_disk

RELEASE = 'docker-stable-control-r4-20261015'
STATE = Path('/home/bestvcboy/dgx-thermal/state')
ADMISSION = Path('/home/bestvcboy/dgx-thermal-control-admission/admission.json')
EVIDENCE = Path('/home/bestvcboy/dgx-thermal-control-evidence')


def regular(path, limit, private=False):
    path = Path(path)
    for parent in (path.parent, *path.parents):
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError('Unsafe file parent')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('Unsafe or oversized file')
        if private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600):
            raise ValueError('Private file permissions required')
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(data) != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('File changed while reading')
        return data
    finally:
        os.close(fd)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def preflight(state=STATE, admission=ADMISSION):
    if state.is_symlink() or not state.is_dir() or state.resolve() != state:
        raise ValueError('Unsafe state directory')
    info = state.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('State ownership or permissions invalid')
    if stat.S_IMODE(admission.parent.stat().st_mode) != 0o700:
        raise ValueError('Private admission directory required')
    catalog_bytes = regular(check.CATALOG, 256 * 1024)
    check.validate_catalog(json.loads(catalog_bytes))
    record = json.loads(regular(admission, 4096, private=True), object_pairs_hook=unique)
    expected = dict(version=1, state='accepted', release=RELEASE,
                    catalog_sha256=hashlib.sha256(catalog_bytes).hexdigest(),
                    state_identity=[info.st_dev, info.st_ino], authorization='natural_load_continuous')
    if record != expected:
        raise ValueError('Current release admission mismatch')
    settings = settings_from_disk(json.loads(regular(state / 'settings.json', 4096, True)))
    token = regular(state / 'device-token', 256, True).decode('ascii').strip()
    if not re.fullmatch('[0-9a-fA-F]{32}', token):
        raise ValueError('Invalid saved device credential')
    regular(state / 'motor-budget.json', 4096, True)
    budget = MotorSchedule.load(state / 'motor-budget.json', state / 'device-token')
    if budget.persistence_error:
        raise ValueError('Invalid persisted motor budget')
    regular(state / 'tls.key', 65536, True)
    regular(state / 'tls.crt', 65536)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(state / 'tls.crt', state / 'tls.key')
    learning_usage = learning_archive_usage(state)
    index = Journal.index_ready(state / 'logs')
    if os.statvfs(state).f_bavail * os.statvfs(state).f_frsize < 128 * 1024 * 1024:
        raise ValueError('Insufficient free storage')
    return dict(release=RELEASE, catalog_sha256=expected['catalog_sha256'],
                state_identity=expected['state_identity'], index=index, motor_fault=budget.fault,
                motor_attempts=budget.attempts, settings=settings, strategy_mode='rules',
                learning_enabled=False, high_wind_gate=True, learning_preserved_bytes=learning_usage['preserved_bytes'])


@dataclass
class Health:
    pid: int
    started: float
    ready: bool = False
    last: float = None

    def receive(self, sender, packet, now):
        if sender != self.pid:
            return
        if packet == b'READY=1' and not self.ready:
            self.ready = True
        elif packet == b'WATCHDOG=1' and self.ready:
            self.last = now

    def expired(self, now, startup, watchdog):
        return now - (self.last if self.ready and self.last is not None else self.started) >= (watchdog if self.ready else startup)


def supervise(command, evidence, readiness, *, startup=40, watchdog=40, shutdown=35):
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    child = None
    outcome, code = 'supervisor_failure', 1
    started = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix='dgx-watchdog-') as directory, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            path = str(Path(directory) / 'notify')
            channel.bind(path)
            channel.settimeout(.25)
            child = subprocess.Popen(command, env={**os.environ, 'NOTIFY_SOCKET': path}, start_new_session=True)
            health = Health(child.pid, time.monotonic())
            while child.poll() is None and not stopped:
                if health.expired(time.monotonic(), startup, watchdog):
                    outcome = 'watchdog_timeout' if health.ready else 'startup_timeout'
                    break
                try:
                    packet, credentials, flags, address = channel.recvmsg(64, socket.CMSG_SPACE(struct.calcsize('3i')))
                    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                        continue
                    for level, kind, data in credentials:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) >= struct.calcsize('3i'):
                            health.receive(struct.unpack('3i', data[:struct.calcsize('3i')])[0], packet, time.monotonic())
                except socket.timeout:
                    pass
            if stopped:
                outcome, code = 'requested_stop', 0
            elif child.poll() is not None:
                outcome, code = 'child_exit', child.returncode or 1
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=shutdown)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if os.getpid() == 1:
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    try:
                        if os.waitpid(-1, os.WNOHANG)[0] == 0:
                            time.sleep(.05)
                    except ChildProcessError:
                        break
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        evidence.mkdir(mode=0o700, exist_ok=True)
        report = dict(outcome=outcome, exitcode=code, started=started, ended=time.time(),
                      child_pid=child.pid if child else None, child_exit=child.returncode if child else None,
                      readiness=readiness, full_wind='not_asserted')
        destination = evidence / ('exit-' + str(time.time_ns()) + '.json')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(evidence, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        print(json.dumps(report), flush=True)
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux' or not Path('/.dockerenv').is_file():
        raise ValueError('Controller entry requires Docker Linux')
    os.umask(0o077)
    readiness = preflight()
    if args.preflight:
        print(json.dumps(readiness))
        return 0
    if (EVIDENCE.is_symlink() or not EVIDENCE.is_dir() or EVIDENCE.resolve() != EVIDENCE
            or EVIDENCE.stat().st_uid != os.getuid() or stat.S_IMODE(EVIDENCE.stat().st_mode) != 0o700
            or STATE == EVIDENCE or STATE in EVIDENCE.parents or EVIDENCE in STATE.parents):
        raise ValueError('Independent private diagnostics directory required')
    return supervise([sys.executable, '-B', str(check.ROOT / 'service.py'), '--state', str(STATE)], EVIDENCE, readiness)


if __name__ == '__main__':
    raise SystemExit(main())
