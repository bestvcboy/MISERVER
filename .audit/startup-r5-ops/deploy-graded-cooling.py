import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import stat
from types import ModuleType
from pathlib import Path
from contextlib import contextmanager
from enum import Enum
import admission_reader as gate


ROOT = Path('/home/bestvcboy/dgx-thermal')
BACKUP = Path('/home/bestvcboy/dgx-thermal-before-startup-repair-v14-r5')
STAGE = Path('/home/bestvcboy/dgx-ambient-tests-startup-repair-v14-r5')
TOOLS = Path('/home/bestvcboy/dgx-thermal-release-tools')
ADMISSION = TOOLS / 'admission.json'
ATTEMPT = TOOLS / 'startup-repair-v14-r5-attempt.json'
LOCK = TOOLS / 'deployment.lock'
DROPIN = Path('/etc/systemd/system/dgx-thermal.service.d/50-forward-admission.conf')
GATE_SHA256 = '129e6275bafd5bd7a7787e2fddd92455bb74ed1a36c50bc20171e96a8fa6d67f'
DROPIN_BYTES = b'[Service]\nExecStartPre=/usr/bin/python3 /home/bestvcboy/dgx-thermal-release-tools/admission_reader.py\n'
MANIFEST = 'graded-cooling-release-manifest.json'
VERIFIER = 'verify-graded-cooling-live.py'
VERIFIER_SHA256 = '81c1e0d356d598a0c7ed717e4f9bea94c99267a81a7eca7f66b707f213e22df4'
DURATION = 'observation_duration.py'
DURATION_SHA256 = '067db49da91943a35ac01a61cea7ce4cf7fa01ccd3f7a2128804685563736192'
DEADLINE_ANCHOR = 'trial-deadline.json'
UNIT = 'dgx-thermal.service'
REQUIRED_OLD = {
    'core.py': 'f61dabb94b8f27486051746e09d5734d366278a2c45da6e909fcb28ce2986718',
    'test_background_probe.py': '6786a42dd1a45da54069bc0ae3b7b362988c66de19b2aeccf17618a4c8173937',
    'ui.js': '0fbf03fec2de48d07cb1b1596048d3f7582f53ef37e866e2d3c29c378f4ee2d6',
    'test_matrix_timelines.py': 'c7c59984534d98836e16310cdd9883156a838746ee20b7fca17146a6d1f824b8',
    'test_historical_requalification.py': 'ae3b3621a969f8602f3b8879a7071251342a2206b8432811d0be596f082fbb81',
    'test_inconclusive_retry.py': 'f904a24339749cffff25a4ef65b877b9fbd45f191df0a8e705c96dc17e6ea73e',
    'test_rebound_envelope.py': '05ec1e74f2fcd628a4dd2d001edc505a749842e6289e68fec3e5069c8866373b',
    'test_predictive_guard.py': '98fcf2b0a16f8fcd4965350485aea5b62b0b6439a952d050eb7fe8aa51d2c5da',
    'test_service.py': 'a9bb8e682367a8a72deeb1b9e028177dec7ef2c1aab69d10c56b95d469a2d415',
    'test_thermal_noise.py': '6a7d04e2a1576bba7cee88fd3b50bfd5e91af0ba88e35685e205f1cc98b28160',
    'test_recovery_authority.py': '194cbe1152e5de104b0e1645520719b3de52cd001b3e68286d5a642f34c867e7',
    'test_learning_service.py': '8b3638d342de76866250a9402e1db2cc8f6ed8bd0838051f59a66daf2ca2a4e6',
    'test_core.py': '515554823e20f3aa50b7ff56e6cb0d1e576dffe084f8f8ae4c4b949e790f76f1',
    'test_window_policy.py': 'af4914f297859a292fb3f828ceb2bbcb009ff4a358672eec0f3a509030d1d225',
    'test_equilibrium.py': 'b7341c129edccbb2df5c60ac3909d19e91eed419e0f3757c5916d0408770b344',
    'test_learning.py': 'bcb2f6e9af60705ee3039a6aefe6b73188bcffdba9df967147718ead51cb8b57',
    'test_lower_airflow.py': '3f90c03b36de92de0ff703590d9e9884a2660bb7b1688b837d094d4dbcb4c969',
    'test_graded_cooling.py': 'a6095fd27837ee6c25d0cc3619ccc467eb041645e6f3a7e50733ae389b93a175',
    'README.md': '1967c424e9d33ce0d954e19e0e2d8b29255249e4524f00a5c9d94bd7c37109de',
    'ui.html': '05563ce81e403b022c47b04f8ba1f9288728e5efdc02dac2a7eb5abfb37f04eb',
    'learning.py': 'c211968cd0c6ce288382b331c40f219dffdb9119494d286dda7e9a94f421611b',
    'test_settled_step.py': '6ae7c532f13a8f5428f80efcce87024ca0214958b91d82d27f313d14a77003ee',
    'test_history_ui.py': 'f184ae5738fed3f934c0ccdc82831e27de07ff6093878b189e1707b283c4a9f7',
    'test_scenarios.py': '9b38e41f5234a05c047516b63139ecb84a3e2fc2abf13f85630f814b42b89a60',
    'service.py': '57fb6c81243b187311ccad38ece3dde470fb3b94f8cab47836e82f53dd72f32b'
}
MANIFEST_SHA256 = '8a1a1484ca99237ea97353e2d0f4e0f10569d5715425c0be006ed0313904a6c0'
CACHE_CONTRACT = {'mode': 'validated-sidecars',
                  'core_sha256': '2b175b352bc3110816793e3cf5ea4a8b34a59f0523b4d6fbf54a45c02e1ff8f9',
                  'service_sha256': '57fb6c81243b187311ccad38ece3dde470fb3b94f8cab47836e82f53dd72f32b'}
PRESERVED = ('device-token', 'settings.json', 'tls.crt', 'tls.key')
NAME = r'(?:core\.py|learning\.py|service\.py|ui\.js|ui\.html|ui\.css|README\.md|test_[a-z0-9_]{1,80}\.py)'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


_duration_path = Path(__file__).resolve().with_name(DURATION)
require(_duration_path.is_file() and not _duration_path.is_symlink(), 'Missing or unsafe observation duration source')
_duration_source = _duration_path.read_bytes()
require(hashlib.sha256(_duration_source).hexdigest() == DURATION_SHA256, 'Observation duration hash mismatch')
_duration_module = ModuleType('_graded_observation_duration')
_duration_module.__name__ += '_' + str(id(_duration_module))
_duration_module.__file__ = str(_duration_path)
sys.modules[_duration_module.__name__] = _duration_module
exec(compile(_duration_source, str(_duration_path), 'exec'), _duration_module.__dict__)
ObservationDuration = _duration_module.ObservationDuration
minimum_observation_argument = _duration_module.minimum_observation_argument
del _duration_source


def sha256(path):
    return hashlib.sha256(gate.read_regular(path, gate.MAX_FILE)).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON key')
        result[key] = value
    return result


def stage_argument(value):
    if value != STAGE.as_posix():
        raise argparse.ArgumentTypeError('Stage must be the explicit startup-repair-v14-r5 directory')
    return Path(value)


def deadline_argument(value):
    try:
        deadline = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Deadline must be a finite epoch timestamp')
    if not math.isfinite(deadline):
        raise argparse.ArgumentTypeError('Deadline must be a finite epoch timestamp')
    return deadline


def check_deadline(deadline):
    require(time.time() < deadline, 'Aggregate supervised trial deadline reached')


def command_timeout(deadline, limit):
    remaining = deadline - time.time()
    require(remaining > 0, 'Aggregate supervised trial deadline reached')
    return min(limit, remaining)


def accepted_deadline(stage, deadline, accepted_at):
    path = stage / DEADLINE_ANCHOR
    require(not path.is_symlink(), 'Unsafe original trial deadline anchor')
    try:
        with path.open('xb') as stream:
            stream.write(json.dumps({'accepted_at': accepted_at, 'deadline': deadline}, allow_nan=False).encode('utf-8'))
            stream.flush()
            os.chmod(path, 0o600)
            os.fsync(stream.fileno())
        sync_directory(stage)
    except FileExistsError:
        pass
    require(path.is_file() and not path.is_symlink(), 'Unsafe original trial deadline anchor')
    require(path.stat().st_size <= 4096, 'Unbounded original trial deadline anchor')
    anchor = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    require(isinstance(anchor, dict) and set(anchor) == {'accepted_at', 'deadline'}, 'Invalid original trial deadline anchor')
    require(all(type(anchor[key]) in (int, float) and math.isfinite(anchor[key])
                for key in ('accepted_at', 'deadline')), 'Invalid finite original trial deadline anchor')
    require(anchor['accepted_at'] <= accepted_at and 540 < anchor['deadline'] - anchor['accepted_at'] <= 1200,
            'Invalid original trial deadline budget')
    require(deadline == anchor['deadline'], 'Cannot renew or change the original trial deadline')
    check_deadline(anchor['deadline'])
    return anchor['deadline']


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_synced(source, destination, digest):
    require(sha256(source) == digest, 'Source hash changed')
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush()
        if hasattr(os, 'fchown'):
            ownership = source.stat()
            os.fchown(outgoing.fileno(), ownership.st_uid, ownership.st_gid)
        os.chmod(destination, source.stat().st_mode & 0o777)
        os.fsync(outgoing.fileno())
    require(sha256(destination) == digest, 'Copied file hash mismatch')


def replace_synced(source, destination, digest):
    require(not destination.is_symlink(), 'Replacement target is a symlink')
    require(sha256(source) == digest, 'Replacement source hash changed')
    temporary = None
    primary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.ambient-release-', dir=destination.parent,
                                         delete=False) as outgoing:
            temporary = Path(outgoing.name)
            with source.open('rb') as incoming:
                shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            if hasattr(os, 'fchown'):
                ownership = source.stat()
                os.fchown(outgoing.fileno(), ownership.st_uid, ownership.st_gid)
            os.chmod(temporary, source.stat().st_mode & 0o777)
            os.fsync(outgoing.fileno())
        require(sha256(temporary) == digest, 'Replacement hash mismatch')
        os.replace(temporary, destination)
        sync_directory(destination.parent)
    except BaseException as error:
        primary = error
        raise
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.forward_cleanup_error = safe_error(cleanup)


def unit_property(name, deadline, clock=None):
    clock = clock or time.time
    remaining = deadline - clock()
    require(remaining > 0, 'Unit query budget reached')
    result = subprocess.check_output(['systemctl', 'show', UNIT, '-p', name, '--value'],
                                     timeout=min(8, remaining), stderr=subprocess.DEVNULL).strip()
    require(clock() < deadline, 'Unit query budget reached')
    return result


def service_pids(deadline, clock=None):
    clock = clock or time.time
    result = set()
    target = os.fsencode(ROOT / 'service.py')
    for process in Path('/proc').iterdir():
        require(clock() < deadline, 'Process query budget reached')
        if not process.name.isdigit():
            continue
        try:
            arguments = (process / 'cmdline').read_bytes().split(b'\0')
            if target in arguments or (
                any(argument in (b'service.py', b'./service.py') for argument in arguments)
                and (process / 'cwd').resolve(strict=True) == ROOT
            ):
                result.add(int(process.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
    require(clock() < deadline, 'Process query budget reached')
    return result


def start(previous_pid, deadline):
    subprocess.run(['sudo', '-n', 'systemctl', 'start', UNIT], check=True,
                   timeout=command_timeout(deadline, 60), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(45):
        pid = int(unit_property('MainPID', deadline))
        if pid > 1 and pid != previous_pid and unit_property('ActiveState', deadline) == b'active':
            require(service_pids(deadline) == {pid}, 'Expected exactly one replacement service process')
            check_deadline(deadline)
            return pid
        time.sleep(command_timeout(deadline, 1))
    raise RuntimeError('No active replacement PID')


class Installation(Enum):
    UNCHANGED = 'unchanged'
    UNCERTAIN = 'uncertain'
    COMPLETE = 'complete'


def safe_error(error):
    name = type(error).__name__
    return name if name in {'RuntimeError', 'ValueError', 'OSError', 'PermissionError',
                           'FileNotFoundError', 'FileExistsError', 'CalledProcessError',
                           'TimeoutExpired', 'KeyboardInterrupt', 'SystemExit',
                           'CancelledError', 'BrokenPipeError', 'JSONDecodeError'} else 'BaseException'


@contextmanager
def deployment_lock():
    gate.safe_parents(LOCK)
    require(TOOLS.is_dir() and not TOOLS.is_symlink(), 'Unsafe tools directory')
    require(not LOCK.is_symlink(), 'Unsafe deployment lock')
    descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_size == 0, 'Unsafe deployment lock contents')
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def atomic_record(path, value):
    gate.safe_parents(path)
    require(path.parent == TOOLS and not path.is_symlink(), 'Unsafe tools record target')
    data = json.dumps(value, sort_keys=True, allow_nan=False).encode('utf-8')
    require(len(data) <= gate.MAX_JSON, 'Unbounded tools record')
    temporary = None
    primary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.forward-', dir=TOOLS, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(TOOLS)
    except BaseException as error:
        primary = error
        raise
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.forward_cleanup_error = safe_error(cleanup)


def admission(state, files):
    record = {'version': 1, 'state': state.value, 'release': gate.RELEASE, 'files': files}
    gate.parse_record(json.dumps(record).encode('utf-8'))
    atomic_record(ADMISSION, record)


def check_cache_contract(candidate, deadline):
    require(type(CACHE_CONTRACT) is dict
            and set(CACHE_CONTRACT) == {'mode', 'core_sha256', 'service_sha256'}
            and CACHE_CONTRACT['mode'] == 'validated-sidecars',
            'PENDING validated-sidecars pre-start cache readiness contract')
    require(all(CACHE_CONTRACT[name + '_sha256'] == candidate[name + '.py']
                for name in ('core', 'service')), 'Cache contract must pin candidate core and service')
    require(all(sha256(STAGE / (name + '.py')) == candidate[name + '.py']
                for name in ('core', 'service')), 'Readiness source changed')
    program = ('import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
               'from core import Journal; print(json.dumps(Journal.index_ready(Path(sys.argv[2]))))')
    result = subprocess.run([str(ROOT / '.venv/bin/python'), '-I', '-B', '-c', program,
                             str(STAGE), str(ROOT / 'state/logs')], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=command_timeout(deadline, 15))
    require(len(result.stdout) <= 4096 and not result.stderr, 'Invalid readiness output')
    report = json.loads(result.stdout.decode('utf-8'), object_pairs_hook=unique_object)
    require(type(report) is dict and set(report) == {'ready', 'segments', 'raw_bytes', 'index_bytes'}
            and report['ready'] is True, 'Invalid readiness report')
    require(all(type(report[key]) is int and report[key] >= 0
                for key in ('segments', 'raw_bytes', 'index_bytes')), 'Invalid readiness counts')
    require(report['segments'] <= 32768 and report['raw_bytes'] + report['index_bytes'] <= 4096 * 1024**2,
            'Readiness inventory exceeds supported budget')
    check_deadline(deadline)
    return report


def require_stopped(deadline):
    require(unit_property('MainPID', deadline) == b'0'
            and unit_property('ActiveState', deadline) == b'inactive'
            and not service_pids(deadline), 'Expected stopped inactive unit and zero matching processes')


def consumed_attempt():
    require(not ATTEMPT.exists() and not ATTEMPT.is_symlink(), 'Attempt already consumed; inspect retained evidence')
    record = gate.parse_record(gate.read_regular(ADMISSION, gate.MAX_JSON), stopped_upgrade=True)
    require(record['release'] == gate.STOPPED_BASELINE_RELEASE and record['state'] == 'blocked'
            and record['files'] == {}, 'Attempt already consumed; inspect retained admission')


def check_gate_configuration(deadline):
    require(gate.read_regular(DROPIN, 4096) == DROPIN_BYTES, 'Forward drop-in bytes mismatch')
    require(sha256(TOOLS / 'admission_reader.py') == GATE_SHA256, 'Admission reader source hash mismatch')
    require(sha256(TOOLS / 'deploy-graded-cooling.py') == sha256(Path(__file__).resolve()), 'Deployment source hash mismatch')
    require(sha256(TOOLS / DURATION) == DURATION_SHA256, 'Tools duration source hash mismatch')
    require(not gate.permission_bits(TOOLS) & 0o022, 'Writable tools directory')
    require(not gate.permission_bits(ADMISSION) & 0o077, 'Unsafe admission permissions')
    consumed_attempt()
    expected = {'Restart': b'always', 'UnitFileState': b'enabled', 'User': b'bestvcboy',
                'Group': b'bestvcboy', 'ProtectHome': b'read-only',
                'ReadWritePaths': os.fsencode(ROOT / 'state'), 'NeedDaemonReload': b'no',
                'DropInPaths': os.fsencode(DROPIN)}
    for name, value in expected.items():
        require(unit_property(name, deadline) == value, 'Unexpected effective unit policy')
    command = '/usr/bin/python3 ' + (TOOLS / 'admission_reader.py').as_posix()
    effective = unit_property('ExecStartPre', deadline).decode('utf-8')
    require(re.fullmatch(r'\{ path=/usr/bin/python3 ; argv\[\]=' + re.escape(command)
                        + r' ; ignore_errors=no ;[^{}]*\}', effective), 'Unexpected effective ExecStartPre')
    check_deadline(deadline)


def contain_failure(report):
    end = time.monotonic() + 75
    errors = []
    report['containment'] = {'stop_confirmed': False, 'admission_closed': False,
                             'fullwind': 'unconfirmed', 'errors': errors}
    for label, action in (
            ('admission', lambda: admission(gate.AdmissionState.BLOCKED, {})),
            ('stop', lambda: subprocess.run(['sudo', '-n', 'systemctl', 'stop', UNIT], check=True,
                                           timeout=min(60, end - time.monotonic()),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))):
        try:
            require(time.monotonic() < end, 'Containment budget reached')
            action()
            require(time.monotonic() < end, 'Containment budget reached')
            if label == 'admission':
                report['containment']['admission_closed'] = True
        except BaseException as error:
            errors.append({'operation': label, 'error': safe_error(error)})
    manager_stopped = False
    processes_stopped = False
    try:
        manager_stopped = unit_property('MainPID', end, time.monotonic) == b'0'
        require(unit_property('ActiveState', end, time.monotonic) in (b'inactive', b'failed'), 'Unit remains active')
    except BaseException as error:
        errors.append({'operation': 'unit-query', 'error': safe_error(error)})
        manager_stopped = False
    try:
        require(time.monotonic() < end, 'Containment budget reached')
        processes_stopped = not service_pids(end, time.monotonic)
    except BaseException as error:
        errors.append({'operation': 'process-query', 'error': safe_error(error)})
    report['containment']['stop_confirmed'] = manager_stopped and processes_stopped and not any(
        error['operation'] == 'stop' for error in errors)
    try:
        require(time.monotonic() < end, 'Containment budget reached')
        atomic_record(ATTEMPT, report)
        require(time.monotonic() < end, 'Containment budget reached')
    except BaseException as error:
        errors.append({'operation': 'evidence', 'error': safe_error(error)})
    try:
        print(json.dumps(report, sort_keys=True), file=sys.stderr, flush=True)
    except BaseException as error:
        errors.append({'operation': 'report', 'error': safe_error(error)})


def deploy(stage, deadline, *, minimum_observation_seconds=569):
    with deployment_lock():
        consumed_attempt()
        return _deploy_locked(stage, deadline, minimum_observation_seconds=minimum_observation_seconds)


def run_verifier(child, deadline, limit, report):
    descriptors = []
    primary = None
    cleanup_error = None
    report['verifier_evidence'] = ['verifier-stdout.log', 'verifier-stderr.log']
    try:
        for name in report['verifier_evidence']:
            descriptors.append(os.open(BACKUP / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        subprocess.run(child, check=True, timeout=command_timeout(deadline, limit),
                       stdout=descriptors[0], stderr=descriptors[1])
    except BaseException as error:
        primary = error
        raise
    finally:
        for descriptor in descriptors:
            for operation in (os.fsync, os.close):
                try:
                    operation(descriptor)
                except BaseException as error:
                    cleanup_error = cleanup_error or error
                    report.setdefault('secondary_errors', []).append({'operation': 'verifier-evidence', 'error': safe_error(error)})
        try:
            sync_directory(BACKUP)
        except BaseException as error:
            cleanup_error = cleanup_error or error
            report.setdefault('secondary_errors', []).append({'operation': 'verifier-evidence', 'error': safe_error(error)})
        if primary is None and cleanup_error is not None:
            raise cleanup_error


def _deploy_locked(stage, deadline, *, minimum_observation_seconds=569):
    require(minimum_observation_seconds == 569 and type(minimum_observation_seconds) is int,
            'R5 requires minimum observation569')
    duration = ObservationDuration(minimum_observation_seconds)
    require(type(deadline) in (int, float) and math.isfinite(deadline), 'Invalid finite trial deadline')
    accepted_at = time.time()
    remaining = deadline - accepted_at
    require(540 < remaining <= 1200, 'Insufficient time or overbudget aggregate trial deadline')
    require(stage == STAGE, 'Invalid startup-repair-v14-r5 stage')
    require(stage.is_dir() and stage.resolve() == stage, 'Unsafe directory')
    require(isinstance(MANIFEST_SHA256, str) and re.fullmatch(r'[0-9a-f]{64}', MANIFEST_SHA256),
            'PENDING parent frozen manifest and independent review before mutation')
    manifest_path = stage / MANIFEST
    manifest_digest = sha256(manifest_path)
    require(manifest_digest == MANIFEST_SHA256, 'Frozen manifest hash mismatch')
    require_stopped(deadline)
    check_cache_contract({name + '.py': sha256(stage / (name + '.py')) for name in ('core', 'service')}, deadline)
    deadline = accepted_deadline(stage, deadline, accepted_at)
    require(stage.parent == ROOT.parent and re.fullmatch(
        r'dgx-ambient-tests-[A-Za-z0-9][A-Za-z0-9_-]{0,79}', stage.name), 'Invalid stage child')
    for directory in (ROOT, stage, ROOT / 'state', ROOT / 'state/learning'):
        require(directory.is_dir() and directory.resolve() == directory, 'Unsafe directory')
    require(not BACKUP.exists() and not BACKUP.is_symlink(), 'Backup already exists')
    require(sha256(stage / VERIFIER) == VERIFIER_SHA256, 'Verifier hash mismatch')
    require(sha256(stage / DURATION) == DURATION_SHA256, 'Observation duration hash mismatch')
    manifest = json.loads(gate.read_regular(manifest_path, gate.MAX_JSON).decode('utf-8'), object_pairs_hook=unique_object)
    require(isinstance(manifest, dict) and set(manifest) == {'old', 'new'}, 'Invalid manifest')
    for version in ('old', 'new'):
        entries = manifest[version]
        require(isinstance(entries, dict) and len(entries) == 25, 'Expected current25/candidate25 manifest file count')
        for name, digest in entries.items():
            require(re.fullmatch(NAME, name) is not None, 'Unbounded manifest filename')
            require(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'Invalid SHA256')
            require(sha256((ROOT if version == 'old' else stage) / name) == digest, 'Manifest hash mismatch')
    old, new = manifest['old'], manifest['new']
    require(not new.keys() & gate.DEPENDENCIES.keys(), 'Unchanged dependency cannot be replaced')
    closure = dict(new, **gate.DEPENDENCIES)
    gate.parse_record(json.dumps({'version': 1, 'state': 'candidate', 'release': gate.RELEASE, 'files': closure}).encode('utf-8'))
    require(all(sha256(ROOT / name) == digest for name, digest in gate.DEPENDENCIES.items()), 'Unchanged dependency hash mismatch')
    require(old == REQUIRED_OLD, 'Wrong installed v14 current25 baseline')
    require(set(new) == set(old), 'Candidate must replace exactly current25 files')
    require({'core.py', 'learning.py'} <= new.keys(), 'Missing controller release files')
    for name in new:
        require(not (ROOT / name).is_symlink(), 'Target symlink')
    preserved = {name: sha256(ROOT / 'state' / name) for name in PRESERVED}
    require(json.loads((ROOT / 'state/settings.json').read_text(encoding='utf-8'))['manual_level'] is None,
            'Manual control is enabled')
    checkpoint = ROOT / 'state/learning/checkpoint.json'
    checkpoint_digest = sha256(checkpoint)
    require(json.loads(checkpoint.read_text(encoding='utf-8'))['controller'] == 14, 'Expected current v14 checkpoint')
    pid = 0
    require_stopped(deadline)
    check_gate_configuration(deadline)
    require(sha256(stage / VERIFIER) == VERIFIER_SHA256, 'Verifier hash mismatch before backup')
    require(sha256(stage / DURATION) == DURATION_SHA256, 'Observation duration hash mismatch before backup')
    check_deadline(deadline)
    require(deadline - time.time() > duration.outer_limit, 'Insufficient original budget before backup')
    BACKUP.mkdir(mode=0o700)
    sync_directory(BACKUP.parent)
    require_stopped(deadline)
    check_deadline(deadline)
    report = {'outcome': 'running', 'release': gate.RELEASE, 'deadline': deadline,
              'phase': 'close-admission', 'installation': Installation.UNCHANGED.value}
    try:
        admission(gate.AdmissionState.BLOCKED, {})
        atomic_record(ATTEMPT, report)
        require_stopped(deadline)
        check_deadline(deadline)
        report['phase'] = 'backup'
        for name, digest in old.items():
            copy_synced(ROOT / name, BACKUP / name, digest)
        copy_synced(manifest_path, BACKUP / MANIFEST, manifest_digest)
        saved = BACKUP / 'learning-checkpoint.json'
        stopped_digest = sha256(checkpoint)
        require(stopped_digest == checkpoint_digest, 'Stopped checkpoint changed before backup')
        require(json.loads(checkpoint.read_text(encoding='utf-8'))['controller'] == 14, 'Expected stopped v14 checkpoint')
        copy_synced(checkpoint, saved, stopped_digest)
        sync_directory(BACKUP)
        check_deadline(deadline)
        require(all(sha256(ROOT / 'state' / name) == digest for name, digest in preserved.items()),
                'Protected state changed')
        require_stopped(deadline)
        report['phase'] = 'install'
        for name, digest in new.items():
            check_deadline(deadline)
            require_stopped(deadline)
            report['installation'] = Installation.UNCERTAIN.value
            replace_synced(stage / name, ROOT / name, digest)
        report['phase'] = 'validate-installation'
        gate.validate_closure(ROOT, closure)
        sync_directory(ROOT)
        require(all(sha256(ROOT / 'state' / name) == digest for name, digest in preserved.items()),
                'Protected state changed')
        check_deadline(deadline)
        require_stopped(deadline)
        require(sha256(checkpoint) == checkpoint_digest, 'Stopped checkpoint changed during installation')
        report['installation'] = Installation.COMPLETE.value
        check_cache_contract(new, deadline)
        report['phase'] = 'admit-candidate'
        admission(gate.AdmissionState.CANDIDATE, closure)
        gate.check_admission(ROOT, ADMISSION)
        check_deadline(deadline)
        report['phase'] = 'start'
        require(report['installation'] == Installation.COMPLETE.value, 'Partial installation cannot start')
        require_stopped(deadline)
        gate.validate_closure(ROOT, closure)
        require(sha256(checkpoint) == checkpoint_digest, 'Stopped checkpoint changed before start')
        require(all(sha256(ROOT / 'state' / name) == digest for name, digest in preserved.items()),
                'Protected state changed before start')
        current = start(pid, deadline)
        report['phase'] = 'verify'
        require(sha256(stage / VERIFIER) == VERIFIER_SHA256, 'Verifier hash mismatch before invocation')
        require(sha256(stage / DURATION) == DURATION_SHA256, 'Observation duration hash mismatch before invocation')
        check_deadline(deadline)
        require(deadline - time.time() > duration.minimum_launch_remaining, 'Insufficient original budget before verifier launch')
        child = [str(ROOT / '.venv/bin/python'), str(stage / VERIFIER), '--expected-pid', str(current),
                 '--deadline', str(deadline)]
        child += ['--minimum-observation-seconds', str(duration.minimum_seconds)]
        run_verifier(child, deadline, duration.outer_limit, report)
        report['phase'] = 'final-validation'
        check_deadline(deadline)
        require(int(unit_property('MainPID', deadline)) == current and unit_property('ActiveState', deadline) == b'active'
                and service_pids(deadline) == {current}, 'Replacement PID changed during verification')
        gate.validate_closure(ROOT, closure)
        require(all(sha256(ROOT / 'state' / name) == digest for name, digest in preserved.items()),
                'Protected state changed at completion')
        require(json.loads(checkpoint.read_text(encoding='utf-8'))['controller'] == 14,
                'Expected v14 checkpoint at completion')
        check_deadline(deadline)
        report['phase'] = 'accept'
        admission(gate.AdmissionState.ACCEPTED, closure)
        report.update(outcome='accepted', controller=14, old_pid=pid, new_pid=current,
                      backup=str(BACKUP), binding_settings_tls_preserved=True)
        atomic_record(ATTEMPT, report)
        result = json.dumps(report)
        check_deadline(deadline)
        print(result, flush=True)
        check_deadline(deadline)
        return report
    except BaseException as primary:
        report.update(outcome='failed', primary_error=safe_error(primary))
        report.setdefault('secondary_errors', [])
        if hasattr(primary, 'forward_cleanup_error'):
            report['secondary_errors'].append({'operation': 'cleanup', 'error': primary.forward_cleanup_error})
        try:
            contain_failure(report)
        except BaseException as secondary:
            report['secondary_errors'].append({'operation': 'containment', 'error': safe_error(secondary)})
        primary.forward_report = report
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Forward-only R5 repair from blocked stopped controller14')
    parser.add_argument('--stage', type=stage_argument, required=True)
    parser.add_argument('--deadline', type=deadline_argument, required=True)
    parser.add_argument('--minimum-observation-seconds', type=minimum_observation_argument, default=569)
    args = parser.parse_args(argv)
    if args.minimum_observation_seconds != 569:
        parser.error('R5 requires --minimum-observation-seconds569')
    try:
        deploy(args.stage, args.deadline, minimum_observation_seconds=args.minimum_observation_seconds)
    except BaseException as error:
        outcome = 'failed' if hasattr(error, 'forward_report') else 'refused'
        code = 130 if isinstance(error, KeyboardInterrupt) else 1
        if isinstance(error, SystemExit) and type(error.code) is int and error.code != 0:
            code = error.code
        parser.exit(code, 'Forward release ' + outcome + ' (' + safe_error(error) + '); inspect retained evidence and unit state.\n')


if __name__ == '__main__':
    main()
