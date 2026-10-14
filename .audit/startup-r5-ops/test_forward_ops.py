import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import admission_reader as gate


BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('forward_deploy', BASE / 'deploy-graded-cooling.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
REAL_CACHE_CHECK = release.check_cache_contract


def digest(data):
    return hashlib.sha256(data).hexdigest()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root = base / 'root'
        self.stage = base / 'dgx-ambient-tests-unit'
        self.tools = base / 'tools'
        self.backup = base / 'backup'
        self.dropin = base / '50-forward-admission.conf'
        self.root.mkdir()
        self.stage.mkdir()
        self.tools.mkdir()
        (self.root / 'state/learning').mkdir(parents=True)
        self.checkpoint = self.root / 'state/learning/checkpoint.json'
        self.initial_checkpoint = b'{"controller":14,"generation":"before-stop"}\n'
        self.stopped_checkpoint = self.initial_checkpoint
        self.current_checkpoint = b'{"controller":14,"generation":"candidate"}\n'
        self.checkpoint.write_bytes(self.initial_checkpoint)
        names = ('core.py', 'learning.py', 'service.py', 'ui.js', 'ui.html', 'README.md')
        self.old = {name: ('old ' + name).encode() for name in names}
        self.old.update({name: ('old ' + name).encode() for name in release.REQUIRED_OLD if name not in names})
        order = ['core.py', 'test_background_probe.py', 'learning.py']
        order += [name for name in self.old if name not in order]
        self.new = {name: ('new ' + name).encode() for name in order}
        for name, data in self.old.items():
            (self.root / name).write_bytes(data)
        for name, data in self.new.items():
            (self.stage / name).write_bytes(data)
        self.dependencies = {'qr_connector.py': b'unchanged QR connector', 'ui.css': b'unchanged css'}
        for name, data in self.dependencies.items():
            (self.root / name).write_bytes(data)
        self.protected = {'device-token': b'secret-token', 'settings.json': b'{"manual_level":null}',
                          'tls.crt': b'certificate', 'tls.key': b'secret-key'}
        for name, data in self.protected.items():
            (self.root / 'state' / name).write_bytes(data)
        self.raw = self.root / 'state/learning/raw.jsonl'
        self.raw.write_bytes(b'raw history\n')
        self.manifest = {'old': {name: digest(data) for name, data in self.old.items()},
                         'new': {name: digest(data) for name, data in self.new.items()}}
        self.write_manifest()
        self.verifier_data = (BASE / release.VERIFIER).read_bytes()
        (self.stage / release.VERIFIER).write_bytes(self.verifier_data)
        (self.stage / release.DURATION).write_bytes((BASE / release.DURATION).read_bytes())
        for name in ('deploy-graded-cooling.py', 'admission_reader.py', release.DURATION):
            shutil.copyfile(BASE / name, self.tools / name)
        self.dropin.write_bytes(release.DROPIN_BYTES)
        self.admission = self.tools / 'admission.json'
        self.attempt = self.tools / 'startup-repair-v14-r5-attempt.json'
        self.lock = self.tools / 'deployment.lock'
        self.deadline = 2000.0
        self.wall = 1000.0
        self.mono = 10.0
        self.state = {'pid': 0, 'stops': 0, 'starts': 0}
        self.calls = []
        self.queries = []
        self.replaced = []
        self.syncs = []
        self.failure = None
        self.primary = subprocess.CalledProcessError(1, ['mock-verifier'], output=b'secret-token-payload')
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {'ROOT': self.root, 'STAGE': self.stage, 'BACKUP': self.backup,
                            'TOOLS': self.tools, 'ADMISSION': self.admission, 'ATTEMPT': self.attempt,
                            'LOCK': self.lock, 'DROPIN': self.dropin,
                            'REQUIRED_OLD': self.manifest['old'],
                            'MANIFEST_SHA256': digest((self.stage / release.MANIFEST).read_bytes()),
                            'CACHE_CONTRACT': {'mode': 'validated-sidecars',
                                               'core_sha256': self.manifest['new']['core.py'],
                                               'service_sha256': self.manifest['new']['service.py']}}.items():
            self.stack.enter_context(patch.object(release, name, value, create=True))
        self.cache_checks = []
        self.stack.enter_context(patch.object(release, 'check_cache_contract', side_effect=self.cache_boundary))
        self.stack.enter_context(patch.object(gate, 'DEPENDENCIES', {name: digest(data) for name, data in self.dependencies.items()}))
        self.stack.enter_context(patch.object(gate, 'permission_bits', side_effect=lambda path: 0o750 if path.is_dir() else 0o600))
        self.stack.enter_context(patch.object(release.time, 'time', side_effect=lambda: self.wall))
        self.stack.enter_context(patch.object(release.time, 'monotonic', side_effect=lambda: self.mono))
        self.stack.enter_context(patch.object(release.time, 'sleep'))
        self.stack.enter_context(patch.object(release, 'service_pids', side_effect=self.pids))
        self.stack.enter_context(patch.object(release, 'sync_directory', side_effect=self.sync))
        self.stack.enter_context(patch.object(subprocess, 'run', side_effect=self.run_command))
        self.stack.enter_context(patch.object(subprocess, 'check_output', side_effect=self.query))
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))
        self.admission.write_text(json.dumps({'version': 1, 'state': 'blocked',
                                             'release': gate.STOPPED_BASELINE_RELEASE, 'files': {}}))
        self.r4_attempt = self.tools / 'cold-release-v14-r4-attempt.json'
        self.r4_attempt.write_bytes(b'{"outcome":"failed","installation":"complete","controller":14}\n')
        self.r4_evidence = self.r4_attempt.read_bytes()
        self.syncs.clear()
        real_replace = os.replace

        def replace(source, destination):
            if Path(destination).parent == self.root:
                self.assertEqual(self.state['pid'], 0)
                self.replaced.append(Path(destination).name)
                if self.failure == 'replace' and len(self.replaced) == 3:
                    raise self.primary
            real_replace(source, destination)

        self.stack.enter_context(patch.object(os, 'replace', side_effect=replace))

    def write_manifest(self):
        (self.stage / release.MANIFEST).write_text(json.dumps(self.manifest), encoding='utf-8')

    def cache_boundary(self, candidate, deadline):
        self.assertEqual(self.state['pid'], 0)
        self.assertEqual(candidate['core.py'], self.manifest['new']['core.py'])
        self.assertEqual(candidate['service.py'], self.manifest['new']['service.py'])
        self.cache_checks.append(bool(self.replaced))
        if self.failure == 'cache-after' and self.replaced:
            raise RuntimeError('Offline cache readiness refused')

    def pids(self, deadline, clock=None):
        return {self.state['pid']} if self.state['pid'] else set()

    def sync(self, path):
        self.syncs.append(Path(path))
        if self.failure == 'sync' and Path(path) == self.root and len(self.replaced) == 1:
            raise self.primary

    def query(self, args, **kwargs):
        self.assertEqual(args[:3], ['systemctl', 'show', release.UNIT])
        self.assertEqual(args[3::2], ['-p', '--value'])
        self.assertTrue(0 < kwargs['timeout'] <= 8)
        self.queries.append((args[4], kwargs['timeout']))
        if self.failure == 'manager' or self.failure == 'manager-after' and 'verify' in self.calls:
            raise OSError('secret-manager-payload')
        if args[4] == 'MainPID':
            return str(self.state['pid']).encode()
        if args[4] == 'ActiveState':
            return b'active' if self.state['pid'] else b'inactive'
        policies = {'Restart': b'always', 'UnitFileState': b'enabled', 'User': b'bestvcboy',
                    'Group': b'bestvcboy', 'ProtectHome': b'read-only',
                    'ReadWritePaths': os.fsencode(self.root / 'state'), 'NeedDaemonReload': b'no',
                    'DropInPaths': os.fsencode(self.dropin),
                    'ExecStartPre': ('{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 '
                                     + (self.tools / 'admission_reader.py').as_posix()
                                     + ' ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }').encode()}
        return policies[args[4]]

    def run_command(self, args, **kwargs):
        self.assertTrue(kwargs['check'])
        self.assertTrue(0 < kwargs['timeout'] <= 720)
        args = list(args)
        if args[0] == str(self.root / '.venv/bin/python'):
            self.calls.append('verify')
            self.assertEqual(args[:6], [str(self.root / '.venv/bin/python'), str(self.stage / release.VERIFIER),
                                      '--expected-pid', '202', '--deadline', str(self.deadline)])
            self.assertEqual(args[6:], ['--minimum-observation-seconds', '569'])
            if self.failure in ('verify', 'containment', 'manager-after', 'budget', 'close-failure'):
                raise self.primary
            if self.failure == 'late':
                self.wall = self.deadline
            return subprocess.CompletedProcess(args, 0)
        self.assertEqual(args[:3], ['sudo', '-n', 'systemctl'])
        self.assertEqual(args[4:], [release.UNIT])
        action = args[3]
        self.calls.append(action)
        if action == 'stop':
            self.state['stops'] += 1
            if self.state['stops'] == 1:
                if self.failure in ('containment', 'manager-after'):
                    raise OSError('secret-stop-payload')
                if self.failure == 'budget':
                    self.assertEqual(kwargs['timeout'], 60)
                    self.mono += 70
                    raise subprocess.TimeoutExpired(args, 60)
            else:
                self.assertEqual(json.loads(self.admission.read_text())['state'], 'blocked')
                self.assertIn(self.tools, self.syncs)
            self.state['pid'] = 0
            return subprocess.CompletedProcess(args, 0)
        self.assertEqual(action, 'start')
        self.state['starts'] += 1
        self.assertEqual(self.checkpoint.read_bytes(), self.initial_checkpoint)
        gate.check_admission(self.root, self.admission)
        self.assertEqual(self.state['pid'], 0)
        self.state['pid'] = 202
        self.checkpoint.write_bytes(self.current_checkpoint)
        if self.failure == 'start':
            raise self.primary
        return subprocess.CompletedProcess(args, 0)

    def report(self):
        return json.loads(self.attempt.read_text())

    def assert_preserved(self):
        for name, data in self.protected.items():
            self.assertEqual((self.root / 'state' / name).read_bytes(), data)
        self.assertEqual(self.raw.read_bytes(), b'raw history\n')
        self.assertEqual(self.r4_attempt.read_bytes(), self.r4_evidence)
        self.assertNotIn('secret-', self.stdout.getvalue() + self.stderr.getvalue())

    def assert_failed(self, calls, installation, checkpoint):
        self.assertEqual(self.calls, calls)
        report = self.report()
        self.assertEqual(report['outcome'], 'failed')
        self.assertEqual(report['installation'], installation)
        self.assertEqual(report['containment']['fullwind'], 'unconfirmed')
        self.assertEqual(json.loads(self.admission.read_text())['state'], 'blocked')
        self.assertEqual(self.checkpoint.read_bytes(), checkpoint)
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            gate.check_admission(self.root, self.admission)
        self.assert_preserved()

    def test_success(self):
        report = release.deploy(self.stage, self.deadline)
        self.assertEqual(report['outcome'], 'accepted')
        self.assertEqual(self.calls, ['start', 'verify'])
        self.assertEqual(self.state['pid'], 202)
        self.assertEqual(self.cache_checks, [False, True])
        self.assertEqual(self.report(), report)
        self.assertEqual(gate.check_admission(self.root, self.admission)['state'], 'accepted')
        self.assertEqual(self.checkpoint.read_bytes(), self.current_checkpoint)
        self.assertEqual((self.backup / 'learning-checkpoint.json').read_bytes(), self.stopped_checkpoint)
        for name, data in self.new.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assert_preserved()

    def test_preflight_refusal(self):
        (self.stage / release.VERIFIER).write_bytes(b'tampered')
        before = self.admission.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Verifier hash mismatch'):
            release.deploy(self.stage, self.deadline)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state['pid'], 0)
        self.assertEqual(self.admission.read_bytes(), before)
        self.assertEqual(self.checkpoint.read_bytes(), self.initial_checkpoint)
        self.assertFalse(self.backup.exists())
        self.assert_preserved()

    def test_mid_install_failure_keeps_prefix_and_stopped_checkpoint14(self):
        self.failure = 'replace'
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            release.deploy(self.stage, self.deadline)
        self.assertIs(caught.exception, self.primary)
        self.assert_failed(['stop'], 'uncertain', self.stopped_checkpoint)
        self.assertEqual((self.root / 'core.py').read_bytes(), b'new core.py')
        self.assertEqual((self.root / 'test_background_probe.py').read_bytes(), b'new test_background_probe.py')
        self.assertEqual((self.root / 'learning.py').read_bytes(), b'old learning.py')

    def test_start_failure_keeps_complete_candidate_and_current_checkpoint(self):
        self.failure = 'start'
        with self.assertRaises(subprocess.CalledProcessError):
            release.deploy(self.stage, self.deadline)
        self.assert_failed(['start', 'stop'], 'complete', self.current_checkpoint)
        self.assertEqual((self.root / 'core.py').read_bytes(), b'new core.py')

    def test_verifier_refusal_is_terminal(self):
        self.failure = 'verify'
        with self.assertRaises(subprocess.CalledProcessError):
            release.deploy(self.stage, self.deadline)
        self.assert_failed(['start', 'verify', 'stop'], 'complete', self.current_checkpoint)
        self.assertEqual(self.report()['phase'], 'verify')

    def test_containment_failure_preserves_primary_and_uncertainty(self):
        self.failure = 'containment'
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            release.deploy(self.stage, self.deadline)
        self.assertIs(caught.exception, self.primary)
        self.assert_failed(['start', 'verify', 'stop'], 'complete', self.current_checkpoint)
        self.assertEqual(self.report()['primary_error'], 'CalledProcessError')
        self.assertEqual(self.report()['containment']['errors'], [
            {'operation': 'stop', 'error': 'OSError'}, {'operation': 'unit-query', 'error': 'RuntimeError'}])
        self.assertEqual(self.report()['containment']['stop_confirmed'], False)
        self.assertEqual(self.state['pid'], 202)

    def test_complete_closure_is_validated_before_any_start(self):
        original = release.replace_synced

        def changed(source, target, expected):
            original(source, target, expected)
            if target.name == 'test_scenarios.py':
                (self.root / 'qr_connector.py').write_bytes(b'changed dependency')

        with patch.object(release, 'replace_synced', side_effect=changed), self.assertRaisesRegex(RuntimeError, 'closure hash'):
            release.deploy(self.stage, self.deadline)
        self.assert_failed(['stop'], 'uncertain', self.stopped_checkpoint)

    def test_consumed_attempt_refuses_without_commands_or_mutation(self):
        self.failure = 'verify'
        with self.assertRaises(subprocess.CalledProcessError):
            release.deploy(self.stage, self.deadline)
        snapshot = {path: path.read_bytes() for path in self.tools.iterdir() if path.is_file()}
        self.calls.clear()
        self.queries.clear()
        self.syncs.clear()
        with self.assertRaisesRegex(RuntimeError, 'consumed'):
            release.deploy(self.stage, 2100)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.queries, [])
        self.assertEqual(self.syncs, [])
        self.assertEqual({path: path.read_bytes() for path in snapshot}, snapshot)

    def test_blocked_crash_record_is_consumed_even_without_terminal_report(self):
        release.admission(gate.AdmissionState.BLOCKED, {})
        with self.assertRaisesRegex(RuntimeError, 'consumed'):
            release.deploy(self.stage, self.deadline)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.queries, [])
        self.assertEqual(json.loads(self.admission.read_text())['state'], 'blocked')

    def test_concurrent_helper_exclusion_uses_actual_local_file_lock(self):
        with release.deployment_lock(), self.assertRaises(OSError):
            release.deploy(self.stage, self.deadline)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.queries, [])
        self.assertEqual(self.state['pid'], 0)
        self.assertEqual(json.loads(self.admission.read_text())['release'], gate.STOPPED_BASELINE_RELEASE)

    def test_containment_uses_one_monotonic_budget(self):
        self.failure = 'budget'
        self.primary = KeyboardInterrupt('secret-interrupt-payload')
        original = self.query
        tail_queries = []

        def query(args, **kwargs):
            if self.state['stops'] == 1:
                tail_queries.append(kwargs['timeout'])
                self.mono += kwargs['timeout']
            return original(args, **kwargs)

        with patch.object(subprocess, 'check_output', side_effect=query), self.assertRaises(KeyboardInterrupt) as caught:
            release.deploy(self.stage, self.deadline)
        self.assertIs(caught.exception, self.primary)
        self.assertEqual(self.mono, 85)
        self.assertEqual(tail_queries, [5])
        self.assertEqual(caught.exception.forward_report['containment']['stop_confirmed'], False)
        self.assertEqual(self.calls, ['start', 'verify', 'stop'])
        self.assertEqual(json.loads(self.admission.read_text())['state'], 'blocked')

    def test_systemexit_and_cancellation_preserve_original_object(self):
        for error in (SystemExit(23), asyncio.CancelledError('secret-cancellation')):
            with self.subTest(kind=type(error).__name__):
                fixture = DeploymentTests()
                fixture.setUp()
                try:
                    fixture.failure = 'verify'
                    fixture.primary = error
                    with self.assertRaises(type(error)) as caught:
                        release.deploy(fixture.stage, fixture.deadline)
                    self.assertIs(caught.exception, error)
                    fixture.assert_failed(['start', 'verify', 'stop'], 'complete', fixture.current_checkpoint)
                finally:
                    fixture.doCleanups()

    def test_manager_unavailable_in_preflight_never_stops_or_installs(self):
        self.failure = 'manager'
        with self.assertRaises(OSError):
            release.deploy(self.stage, self.deadline)
        self.assertEqual(self.state['pid'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.checkpoint.read_bytes(), self.initial_checkpoint)

    def test_manager_unavailable_after_failure_reports_unconfirmed_stop(self):
        self.failure = 'manager-after'
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            release.deploy(self.stage, self.deadline)
        self.assertIs(caught.exception, self.primary)
        self.assertEqual(self.report()['containment']['stop_confirmed'], False)
        self.assertEqual(self.report()['containment']['errors'], [
            {'operation': 'stop', 'error': 'OSError'}, {'operation': 'unit-query', 'error': 'OSError'}])
        self.assertEqual(self.state['pid'], 202)
        self.assertEqual(self.checkpoint.read_bytes(), self.current_checkpoint)

    def test_gate_closure_failure_still_attempts_stop(self):
        self.failure = 'verify'
        original = release.admission

        def admission(state, files):
            if state == gate.AdmissionState.BLOCKED and self.state['starts']:
                raise OSError('secret-admission-payload')
            return original(state, files)

        with patch.object(release, 'admission', side_effect=admission), self.assertRaises(subprocess.CalledProcessError) as caught:
            release.deploy(self.stage, self.deadline)
        self.assertIs(caught.exception, self.primary)
        self.assertEqual(self.calls, ['start', 'verify', 'stop'])
        self.assertEqual(self.report()['containment']['admission_closed'], False)
        self.assertEqual(self.report()['containment']['stop_confirmed'], True)
        self.assertEqual(self.report()['containment']['errors'], [{'operation': 'admission', 'error': 'OSError'}])

    def test_fsync_failure_retains_renamed_bytes_and_blocks_start(self):
        self.failure = 'sync'
        with self.assertRaises(subprocess.CalledProcessError):
            release.deploy(self.stage, self.deadline)
        self.assert_failed(['stop'], 'uncertain', self.stopped_checkpoint)
        self.assertEqual((self.root / 'core.py').read_bytes(), b'new core.py')

    def test_minimum569_keeps_original_caps_and_cli(self):
        result = release.deploy(self.stage, self.deadline, minimum_observation_seconds=569)
        self.assertEqual(result['outcome'], 'accepted')
        self.assertEqual(release.ObservationDuration(569).status_samples, 192)
        self.assertEqual(release.ObservationDuration(569).history_min_samples, 191)
        self.assertEqual(release.ObservationDuration(569).outer_limit, 720)
        with patch.object(release, 'deploy') as deploy:
            release.main(['--stage', self.stage.as_posix(), '--deadline', '2000', '--minimum-observation-seconds', '569'])
        deploy.assert_called_once_with(self.stage, 2000.0, minimum_observation_seconds=569)

    def test_stage_anchor_cannot_renew_after_preflight_refusal(self):
        (self.stage / release.VERIFIER).write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            release.deploy(self.stage, self.deadline)
        anchor = (self.stage / release.DEADLINE_ANCHOR).read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Cannot renew'):
            release.deploy(self.stage, 2100)
        self.assertEqual((self.stage / release.DEADLINE_ANCHOR).read_bytes(), anchor)
        self.assertEqual(self.calls, [])

    def test_expired_completion_is_failed_with_no_new_trial(self):
        self.failure = 'late'
        with self.assertRaises(RuntimeError):
            release.deploy(self.stage, self.deadline)
        self.assert_failed(['start', 'verify', 'stop'], 'complete', self.current_checkpoint)

    def test_preflight_checks_effective_dropin_and_source(self):
        for path in (self.dropin, self.tools / 'admission_reader.py', self.tools / 'deploy-graded-cooling.py'):
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_bytes(b'tampered')
                with self.assertRaises(RuntimeError):
                    release.deploy(self.stage, self.deadline)
                path.write_bytes(before)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.state['pid'], 0)

    def test_unit_policy_mismatch_refuses(self):
        original = self.query
        for property_name, wrong in (('NeedDaemonReload', b'yes'), ('DropInPaths', b''),
                                     ('ExecStartPre', b'{ path=/bin/true ; argv[]=/bin/true ; ignore_errors=no ; }'),
                                     ('ReadWritePaths', b'/home/bestvcboy/dgx-thermal')):
            with self.subTest(property_name=property_name), patch.object(
                    subprocess, 'check_output', side_effect=lambda args, **kwargs: wrong if args[4] == property_name else original(args, **kwargs)):
                with self.assertRaises(RuntimeError):
                    release.deploy(self.stage, self.deadline)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.state['pid'], 0)

    def admit_complete_candidate(self):
        release.admission(gate.AdmissionState.CANDIDATE,
                          dict(self.manifest['new'], **gate.DEPENDENCIES))
        for name, data in self.new.items():
            (self.root / name).write_bytes(data)

    def assert_preflight_untouched(self, before):
        self.assertEqual(self.admission.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.replaced, [])
        self.assertFalse(self.attempt.exists())
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.checkpoint.read_bytes(), self.initial_checkpoint)
        self.assert_preserved()

    def test_r4_blocked_record_is_preflight_only_and_cannot_start(self):
        before = self.admission.read_bytes()
        record = gate.parse_record(before, stopped_upgrade=True)
        self.assertEqual(record['release'], 'cold-release-v14-r4')
        self.assertEqual(record['state'], 'blocked')
        with self.assertRaisesRegex(RuntimeError, 'release'):
            gate.check_admission(self.root, self.admission)
        with patch.object(gate, 'check_admission', side_effect=lambda: gate.parse_record(before)):
            self.assertEqual(gate.main(), 1)
        self.assert_preflight_untouched(before)

    def test_historical_r4_candidate_and_accepted_never_admitted(self):
        self.admit_complete_candidate()
        record = json.loads(self.admission.read_text())
        record['release'] = 'cold-release-v14-r4'
        for state in ('candidate', 'accepted'):
            record['state'] = state
            data = json.dumps(record).encode()
            for stopped_upgrade in (False, True):
                with self.subTest(state=state, stopped_upgrade=stopped_upgrade), self.assertRaises(RuntimeError):
                    gate.parse_record(data, stopped_upgrade=stopped_upgrade)
            self.admission.write_bytes(data)
            with self.assertRaises(RuntimeError):
                gate.check_admission(self.root, self.admission)
        self.assertEqual(self.calls, [])

    def test_accepted_bootstrap_is_never_admitted_even_with_complete27(self):
        self.admit_complete_candidate()
        record = json.loads(self.admission.read_text())
        record.update(release='bootstrap-current', state='accepted')
        data = json.dumps(record).encode()
        for stopped_upgrade in (False, True):
            with self.assertRaises(RuntimeError):
                gate.parse_record(data, stopped_upgrade=stopped_upgrade)

    def test_active_failed_or_unmanaged_process_baseline_refuses(self):
        before = self.admission.read_bytes()
        original = self.query
        for pid, active, pids in ((101, b'active', {101}), (0, b'failed', set()),
                                   (0, b'inactive', {303}), (0, b'active', set())):
            def query(args, **kwargs):
                if args[4] == 'MainPID':
                    return str(pid).encode()
                if args[4] == 'ActiveState':
                    return active
                return original(args, **kwargs)
            with self.subTest(pid=pid, active=active, pids=pids), \
                    patch.object(subprocess, 'check_output', side_effect=query), \
                    patch.object(release, 'service_pids', return_value=pids), self.assertRaisesRegex(RuntimeError, 'stopped'):
                release.deploy(self.stage, self.deadline)
            self.assert_preflight_untouched(before)

    def test_pending_manifest_refuses_before_deadline_anchor(self):
        before = self.admission.read_bytes()
        with patch.object(release, 'MANIFEST_SHA256', 'PENDING_PARENT_FREEZE'), self.assertRaisesRegex(RuntimeError, 'PENDING'):
            release.deploy(self.stage, self.deadline)
        self.assertFalse((self.stage / release.DEADLINE_ANCHOR).exists())
        self.assert_preflight_untouched(before)

    def test_frozen_manifest_mismatch_refuses_before_deadline_anchor(self):
        before = self.admission.read_bytes()
        with patch.object(release, 'MANIFEST_SHA256', '0' * 64), self.assertRaisesRegex(RuntimeError, 'Frozen manifest'):
            release.deploy(self.stage, self.deadline)
        self.assertFalse((self.stage / release.DEADLINE_ANCHOR).exists())
        self.assert_preflight_untouched(before)

    def test_missing_or_unpinned_sidecar_contract_refuses_before_mutation(self):
        before = self.admission.read_bytes()
        for contract, message in ((None, 'PENDING'),
                ({'mode': 'not-required', 'core_sha256': self.manifest['new']['core.py'],
                  'service_sha256': self.manifest['new']['service.py']}, 'PENDING'),
                ({'mode': 'validated-sidecars', 'core_sha256': '0' * 64,
                  'service_sha256': self.manifest['new']['service.py']}, 'must pin')):
            with self.subTest(contract=contract), patch.object(release, 'CACHE_CONTRACT', contract), \
                    patch.object(release, 'check_cache_contract', side_effect=REAL_CACHE_CHECK), \
                    self.assertRaisesRegex(RuntimeError, message):
                release.deploy(self.stage, self.deadline)
            self.assertFalse((self.stage / release.DEADLINE_ANCHOR).exists())
            self.assert_preflight_untouched(before)

    def test_post_install_readiness_refusal_never_starts(self):
        self.failure = 'cache-after'
        with self.assertRaisesRegex(RuntimeError, 'cache readiness'):
            release.deploy(self.stage, self.deadline)
        self.assert_failed(['stop'], 'complete', self.initial_checkpoint)
        self.assertEqual(self.cache_checks, [False, True])
        for name, data in self.new.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def test_changed_stopped_checkpoint_is_retained_and_never_started(self):
        original = release.replace_synced
        changed = b'{"controller":14,"generation":"external-change"}\n'

        def replace(source, target, expected):
            original(source, target, expected)
            if len(self.replaced) == 1:
                self.checkpoint.write_bytes(changed)

        with patch.object(release, 'replace_synced', side_effect=replace), self.assertRaisesRegex(RuntimeError, 'checkpoint changed'):
            release.deploy(self.stage, self.deadline)
        self.assert_failed(['stop'], 'uncertain', changed)
        self.assertEqual((self.backup / 'learning-checkpoint.json').read_bytes(), self.initial_checkpoint)

    def test_process_appearing_during_install_stops_without_starting(self):
        original = release.replace_synced

        def replace(source, target, expected):
            original(source, target, expected)
            if len(self.replaced) == 1:
                self.state['pid'] = 303

        with patch.object(release, 'replace_synced', side_effect=replace), self.assertRaisesRegex(RuntimeError, 'stopped'):
            release.deploy(self.stage, self.deadline)
        self.assert_failed(['stop'], 'uncertain', self.initial_checkpoint)
        self.assertEqual(self.replaced, ['core.py'])

    def test_current_controller10_checkpoint_refuses(self):
        self.checkpoint.write_bytes(b'{"controller":10}')
        before = self.admission.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'current v14 checkpoint'):
            release.deploy(self.stage, self.deadline)
        self.assertEqual(self.admission.read_bytes(), before)
        self.assertEqual(self.checkpoint.read_bytes(), b'{"controller":10}')
        self.assertEqual(self.calls, [])

    def test_wrong_current25_baseline_pin_refuses(self):
        before = self.admission.read_bytes()
        with patch.object(release, 'REQUIRED_OLD', dict(release.REQUIRED_OLD, **{'service.py': '0' * 64})), \
                self.assertRaisesRegex(RuntimeError, 'current25 baseline'):
            release.deploy(self.stage, self.deadline)
        self.assert_preflight_untouched(before)

    def test_current22_manifest_is_rejected(self):
        before = self.admission.read_bytes()
        self.manifest['old'] = dict(list(self.manifest['old'].items())[:22])
        self.write_manifest()
        with patch.object(release, 'MANIFEST_SHA256', digest((self.stage / release.MANIFEST).read_bytes())), \
                self.assertRaisesRegex(RuntimeError, 'current25/candidate25'):
            release.deploy(self.stage, self.deadline)
        self.assert_preflight_untouched(before)

    def test_install_requires_durable_blocked_admission_and_attempt(self):
        original = release.replace_synced

        def replace(source, target, expected):
            self.assertEqual(json.loads(self.admission.read_text())['state'], 'blocked')
            self.assertEqual(self.report()['release'], 'startup-repair-v14-r5')
            self.assertEqual(self.report()['outcome'], 'running')
            self.assertIn(self.tools, self.syncs)
            original(source, target, expected)

        with patch.object(release, 'replace_synced', side_effect=replace):
            release.deploy(self.stage, self.deadline)

    def test_minimum_and_aggregate_budget_cannot_expand_or_shrink(self):
        before = self.admission.read_bytes()
        for minimum in (480, 568, 570, True):
            with self.subTest(minimum=minimum), self.assertRaisesRegex(RuntimeError, 'minimum observation569'):
                release.deploy(self.stage, self.deadline, minimum_observation_seconds=minimum)
        with self.assertRaisesRegex(RuntimeError, 'overbudget'):
            release.deploy(self.stage, self.wall + 1201)
        self.assert_preflight_untouched(before)

    def test_no_bootstrap_entry_or_function(self):
        self.assertFalse(hasattr(release, 'bootstrap_admission'))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            release.main(['--bootstrap-manifest', str(self.stage / release.MANIFEST)])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.attempt.exists())

    def test_gate_denies_missing_corrupt_partial_duplicate_and_unsafe_records(self):
        self.admit_complete_candidate()
        good = self.admission.read_bytes()
        self.admission.unlink()
        with self.assertRaises(FileNotFoundError):
            gate.check_admission(self.root, self.admission)
        for data in (b'invalid', b'{"version":1,"version":1}', b' ' * 16385,
                     json.dumps({'version': 1, 'state': 'accepted', 'release': 'bootstrap-current',
                                 'files': {'core.py': digest(self.old['core.py'])}}).encode()):
            with self.subTest(data=data[:80]):
                self.admission.write_bytes(data)
                with self.assertRaises((RuntimeError, ValueError)):
                    gate.check_admission(self.root, self.admission)
        self.admission.write_bytes(good)
        self.assertEqual(gate.check_admission(self.root, self.admission)['state'], 'candidate')
        self.assertEqual(self.admission.read_bytes(), good)

    def test_gate_denies_symlink_files_and_parents(self):
        self.admit_complete_candidate()
        original = Path.is_symlink
        for target in (self.admission, self.tools, self.root, self.root / 'core.py'):
            with self.subTest(target=target), patch.object(Path, 'is_symlink', side_effect=lambda path: path == target or original(path), autospec=True):
                with self.assertRaises(RuntimeError):
                    gate.check_admission(self.root, self.admission)
        self.assertEqual(gate.check_admission(self.root, self.admission)['state'], 'candidate')

    def test_gate_denies_nonregular_file_and_wrong_hash(self):
        self.admit_complete_candidate()
        path = self.root / 'core.py'
        path.unlink()
        path.mkdir()
        with self.assertRaises((RuntimeError, OSError)):
            gate.check_admission(self.root, self.admission)
        path.rmdir()
        path.write_bytes(b'wrong hash')
        with self.assertRaisesRegex(RuntimeError, 'closure hash'):
            gate.check_admission(self.root, self.admission)

    def test_gate_denies_permissions_names_types_and_count(self):
        self.admit_complete_candidate()
        good = json.loads(self.admission.read_text())
        for name in ('../core.py', 'state/checkpoint.json', '/core.py', 'xiaomi_qr.py'):
            record = dict(good, files=dict(good['files'], **{name: '0' * 64}))
            with self.assertRaises(RuntimeError):
                gate.parse_record(json.dumps(record).encode())
        for change in ({'version': True}, {'state': 'unknown'}, {'files': []},
                       {'files': {'test_%d.py' % i: '0' * 64 for i in range(28)}}):
            with self.assertRaises((RuntimeError, ValueError)):
                gate.parse_record(json.dumps(dict(good, **change)).encode())
        with patch.object(gate, 'permission_bits', return_value=0o777), self.assertRaisesRegex(RuntimeError, 'Writable tools'):
            gate.check_admission(self.root, self.admission)

    def test_cleanup_failure_cannot_mask_primary(self):
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name.startswith('.ambient-release-'):
                raise PermissionError('secret-cleanup-payload')
            return original(path, *args, **kwargs)

        with patch.object(os, 'fchown', side_effect=self.primary, create=True), patch.object(Path, 'unlink', unlink), self.assertRaises(subprocess.CalledProcessError) as caught:
            release.replace_synced(self.stage / 'core.py', self.root / 'core.py', digest(self.new['core.py']))
        self.assertIs(caught.exception, self.primary)
        self.assertEqual(caught.exception.forward_cleanup_error, 'PermissionError')
        self.assertEqual((self.root / 'core.py').read_bytes(), b'old core.py')


class ReadinessBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE)
        self.addCleanup(temporary.cleanup)
        self.stage = Path(temporary.name).resolve()
        self.candidate = {'core.py': digest(b'candidate core'), 'service.py': digest(b'candidate service')}
        (self.stage / 'core.py').write_bytes(b'candidate core')
        (self.stage / 'service.py').write_bytes(b'candidate service')
        self.contract = {'mode': 'validated-sidecars', 'core_sha256': self.candidate['core.py'],
                         'service_sha256': self.candidate['service.py']}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(release, 'STAGE', self.stage))
        self.stack.enter_context(patch.object(release, 'CACHE_CONTRACT', self.contract))
        self.stack.enter_context(patch.object(release.time, 'time', return_value=100))
        self.report = {'ready': True, 'segments': 4, 'raw_bytes': 1200, 'index_bytes': 800}

    def test_exact_report_uses_isolated_fixed_readonly_probe_and_remaining_budget(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(self.report).encode(), stderr=b'')
        with patch.object(subprocess, 'run', return_value=result) as run:
            self.assertEqual(REAL_CACHE_CHECK(self.candidate, 102), self.report)
        args = run.call_args[0][0]
        self.assertEqual(args[0:4], [str(release.ROOT / '.venv/bin/python'), '-I', '-B', '-c'])
        self.assertEqual(args[-2:], [str(self.stage), str(release.ROOT / 'state/logs')])
        self.assertIn('Journal.index_ready', args[4])
        self.assertNotIn('Journal(', args[4])
        self.assertEqual(run.call_args[1]['timeout'], 2)

    def test_changed_candidate_refuses_without_probe(self):
        (self.stage / 'core.py').write_bytes(b'changed')
        with patch.object(subprocess, 'run') as run, self.assertRaisesRegex(RuntimeError, 'source changed'):
            REAL_CACHE_CHECK(self.candidate, 115)
        run.assert_not_called()

    def test_invalid_readiness_reports_cannot_admit(self):
        invalid = [dict(self.report, ready=False), dict(self.report, segments=True),
                   dict(self.report, raw_bytes=-1), dict(self.report, index_bytes=4096 * 1024**2),
                   dict(self.report, extra=1), {'ready': True}]
        for report in invalid:
            with self.subTest(report=report):
                result = subprocess.CompletedProcess([], 0, stdout=json.dumps(report).encode(), stderr=b'')
                with patch.object(subprocess, 'run', return_value=result), self.assertRaises(RuntimeError):
                    REAL_CACHE_CHECK(self.candidate, 115)
        for output, stderr in ((b'x' * 4097, b''), (b'{}', b'unexpected'), (b'{"ready":true,"ready":true}', b'')):
            result = subprocess.CompletedProcess([], 0, stdout=output, stderr=stderr)
            with patch.object(subprocess, 'run', return_value=result), self.assertRaises(RuntimeError):
                REAL_CACHE_CHECK(self.candidate, 115)

    def test_refused_or_timed_out_probe_preserves_original_exception(self):
        for error in (subprocess.CalledProcessError(1, 'readonly'), subprocess.TimeoutExpired('readonly', 15)):
            with patch.object(subprocess, 'run', side_effect=error), self.assertRaises(type(error)) as caught:
                REAL_CACHE_CHECK(self.candidate, 115)
            self.assertIs(caught.exception, error)


class IntegrityAndCliTests(unittest.TestCase):
    def test_installed25_pins_match_r4_candidate_not_r4_old22(self):
        r4 = json.loads((BASE.parent / 'cold-release-v14-r4/graded-cooling-release-manifest.json').read_text())
        self.assertEqual(release.REQUIRED_OLD, r4['new'])
        self.assertEqual(len(release.REQUIRED_OLD), 25)
        self.assertNotEqual(release.REQUIRED_OLD, r4['old'])
        self.assertEqual(release.REQUIRED_OLD['core.py'], 'f61dabb94b8f27486051746e09d5734d366278a2c45da6e909fcb28ce2986718')
        self.assertEqual(release.REQUIRED_OLD['service.py'], '57fb6c81243b187311ccad38ece3dde470fb3b94f8cab47836e82f53dd72f32b')
        self.assertEqual(release.REQUIRED_OLD['learning.py'], 'c211968cd0c6ce288382b331c40f219dffdb9119494d286dda7e9a94f421611b')

    def test_frozen_manifest_and_cache_pins_match_fresh_candidate(self):
        manifest_path = BASE / release.MANIFEST
        manifest = json.loads(manifest_path.read_bytes())
        self.assertEqual(release.MANIFEST_SHA256, digest(manifest_path.read_bytes()))
        self.assertEqual(manifest['old'], release.REQUIRED_OLD)
        self.assertEqual(release.CACHE_CONTRACT, {'mode': 'validated-sidecars',
            'core_sha256': manifest['new']['core.py'], 'service_sha256': manifest['new']['service.py']})
        self.assertEqual(gate.RELEASE, 'startup-repair-v14-r5')
        self.assertEqual(release.STAGE.as_posix(), '/home/bestvcboy/dgx-ambient-tests-startup-repair-v14-r5')
        self.assertEqual(release.BACKUP.as_posix(), '/home/bestvcboy/dgx-thermal-before-startup-repair-v14-r5')
        self.assertEqual(release.ATTEMPT.name, 'startup-repair-v14-r5-attempt.json')

    def test_cli_interruptions_are_sanitized_without_child_processes(self):
        for error, expected_code, category in (
                (SystemExit(23), 23, 'SystemExit'),
                (SystemExit('secret-interruption-sentinel'), 1, 'SystemExit'),
                (SystemExit(0), 1, 'SystemExit'),
                (KeyboardInterrupt('secret-interruption-sentinel'), 130, 'KeyboardInterrupt'),
                (asyncio.CancelledError('secret-interruption-sentinel'), 1, 'CancelledError')):
            error.forward_report = {'outcome': 'failed'}
            stderr = io.StringIO()
            stdout = io.StringIO()
            with self.subTest(category=category), patch.object(release, 'deploy', side_effect=error), \
                    contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout), \
                    self.assertRaises(SystemExit) as caught:
                release.main(['--stage', release.STAGE.as_posix(), '--deadline', '2000'])
            self.assertEqual(caught.exception.code, expected_code)
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(stderr.getvalue(), 'Forward release failed (' + category
                             + '); inspect retained evidence and unit state.\n')
            self.assertNotIn('secret-interruption-sentinel', stderr.getvalue())

    def test_copied_reviewed_sources_are_byte_identical(self):
        historical = BASE.parent / 'forward-ops-r1'
        for name, expected in ((release.DURATION, '067db49da91943a35ac01a61cea7ce4cf7fa01ccd3f7a2128804685563736192'),
                               (release.VERIFIER, '81c1e0d356d598a0c7ed717e4f9bea94c99267a81a7eca7f66b707f213e22df4')):
            self.assertEqual(digest((BASE / name).read_bytes()), expected)
            self.assertEqual((BASE / name).read_bytes(), (historical / name).read_bytes())
        self.assertEqual(digest((BASE / 'admission_reader.py').read_bytes()), release.GATE_SHA256)
        self.assertEqual((BASE / '50-forward-admission.conf').read_bytes(), release.DROPIN_BYTES)
        self.assertEqual(gate.DEPENDENCIES, {
            'qr_connector.py': '98dd6e5b74eaba733db12d24126655cbf802e3af5e3419d3a7fe85f1a91eb363',
            'ui.css': '3934c1146112c1ce18647a2d02a4357063914d727a066d9e1eb8aa86001e8d98'})

    def test_cli_error_payload_is_not_reported(self):
        stderr = io.StringIO()
        with patch.object(release, 'deploy', side_effect=ValueError('secret-token-payload')), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            release.main(['--stage', release.STAGE.as_posix(), '--deadline', '2000'])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(stderr.getvalue(), 'Forward release refused (ValueError); inspect retained evidence and unit state.\n')

    def test_required_cli_and_finite_deadline(self):
        for options in ([], ['--stage', release.STAGE.as_posix()],
                        ['--stage', release.STAGE.as_posix(), '--deadline', 'nan']):
            with patch.object(release, 'deploy') as deploy, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                release.main(options)
            self.assertEqual(caught.exception.code, 2)
            deploy.assert_not_called()
        with self.assertRaises(argparse.ArgumentTypeError):
            release.stage_argument('/home/bestvcboy/dgx-ambient-tests-cold-release-v14-r3')


if __name__ == '__main__':
    unittest.main()
