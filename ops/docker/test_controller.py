import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import controller_supervisor as control
from service import MOTOR_IDENTITY


class HealthTests(unittest.TestCase):
    def test_formal_controller_reserves_one_cpu_for_sampling_and_journal(self):
        lines = (Path(__file__).with_name('compose.yaml')).read_text(encoding='utf-8').splitlines()
        self.assertEqual([line.split(':', 1)[1].strip() for line in lines
                          if line.strip().startswith('cpus:')], ['1.0'])

    def test_foreign_pid_and_malformed_packets_do_not_refresh_watchdog(self):
        health = control.Health(42, 100)
        for packet in (b'READY=1', b'WATCHDOG=1'):
            health.receive(43, packet, 139)
        self.assertFalse(health.ready)
        self.assertTrue(health.expired(140, 40, 40))
        health.receive(42, b'WATCHDOG=1', 139)
        health.receive(42, b'READY=1\nWATCHDOG=1', 139)
        self.assertFalse(health.ready)

    def test_ready_is_not_repeated_credit_and_only_child_heartbeat_refreshes(self):
        health = control.Health(42, 100)
        health.receive(42, b'READY=1', 101)
        health.receive(42, b'WATCHDOG=1', 102)
        health.receive(42, b'READY=1', 139)
        health.receive(43, b'WATCHDOG=1', 139)
        self.assertFalse(health.expired(141, 40, 40))
        self.assertTrue(health.expired(142, 40, 40))
        health.receive(42, b'WATCHDOG=1', 142)
        self.assertFalse(health.expired(143, 40, 40))

    def test_duplicate_admission_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            json.loads('{"state":"blocked","state":"accepted"}', object_pairs_hook=control.unique)


@unittest.skipUnless(sys.platform == 'linux', 'Linux Unix credentials and directory permissions required')
class NativeControllerTests(unittest.TestCase):
    def test_preflight_preserves_faulted_budget_and_refuses_wrong_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / 'state';state.mkdir(mode=0o700)
            admit = root / 'admit';admit.mkdir(mode=0o700)
            files = {'settings.json': {'retention_days': 7, 'max_log_mb': 4096, 'manual_level': None},
                     'motor-budget.json': {'version': 1, 'identity': MOTOR_IDENTITY, 'attempts': 2, 'expected_level': 16, 'fault': True}}
            for name, record in files.items():
                path = state / name;path.write_text(json.dumps(record));path.chmod(0o600)
            for name, body in (('device-token', 'a'*32), ('tls.key', 'fake key'), ('tls.crt', 'fake certificate')):
                path = state / name;path.write_text(body);path.chmod(0o600)
            (state/'logs').mkdir(mode=0o700)
            learning = state/'learning';learning.mkdir(mode=0o700)
            for name, body in (('checkpoint.json', b'paused invalid legacy content'), ('.pending-abcdefgh', b'preserved pending')):
                path = learning/name;path.write_bytes(body);path.chmod(0o600)
            catalog = root / 'catalog.json';catalog.write_bytes(b'{}')
            record = dict(version=1,state='accepted',release=control.RELEASE,catalog_sha256=hashlib.sha256(b'{}').hexdigest(),
                          state_identity=[state.stat().st_dev,state.stat().st_ino],authorization='natural_load_continuous')
            admission = admit/'admission.json';admission.write_text(json.dumps(record));admission.chmod(0o600)
            before = {p.relative_to(state).as_posix():p.read_bytes() for p in state.rglob('*') if p.is_file()}
            with patch.object(control.check,'CATALOG',catalog), patch.object(control.check,'validate_catalog'), patch.object(control.ssl.SSLContext,'load_cert_chain'):
                result = control.preflight(state,admission)
                self.assertEqual((result['motor_attempts'],result['motor_fault']), (2,False))
                self.assertTrue(result['motor_legacy_pending'])
                self.assertEqual(result['motor_budget_version'], 1)
                self.assertEqual((result['strategy_mode'], result['learning_enabled']), ('rules', False))
                self.assertEqual(result['learning_preserved_bytes'], 46)
                for changes in ({'state':'blocked'},{'catalog_sha256':'0'*64},{'authorization':'historical_trial'},{'state_identity':[0,0]}):
                    admission.write_text(json.dumps(dict(record,**changes)))
                    with self.assertRaises(ValueError): control.preflight(state,admission)
            self.assertEqual({p.relative_to(state).as_posix():p.read_bytes() for p in state.rglob('*') if p.is_file()},before)

    def test_actual_child_timeout_kills_reaps_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            evidence=Path(folder)
            code=control.supervise([sys.executable,'-c','import time;time.sleep(10)'],evidence,{},startup=.3,watchdog=.3,shutdown=.3)
            self.assertNotEqual(code,0)
            report=json.loads(next(evidence.glob('exit-*.json')).read_text())
            self.assertEqual(report['outcome'],'startup_timeout')
            with self.assertRaises(ProcessLookupError): os.kill(report['child_pid'],0)

    def test_child_ready_without_heartbeat_expires_and_ignored_term_is_killed(self):
        child="import os,socket,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);s.sendto(b'READY=1',os.environ['NOTIFY_SOCKET']);time.sleep(10)"
        with tempfile.TemporaryDirectory() as folder:
            code=control.supervise([sys.executable,'-c',child],Path(folder),{},startup=.8,watchdog=.4,shutdown=.2)
            report=json.loads(next(Path(folder).glob('exit-*.json')).read_text())
            self.assertNotEqual(code,0)
            self.assertEqual(report['outcome'],'watchdog_timeout')
            self.assertEqual(report['child_exit'],-signal.SIGKILL)

    def test_foreign_process_real_socket_cannot_forge_child_readiness(self):
        sender="import os,socket,time;s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);s.sendto(b'READY=1',os.environ['NOTIFY_SOCKET']);s.sendto(b'WATCHDOG=1',os.environ['NOTIFY_SOCKET']);time.sleep(10)"
        child="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"+repr(sender)+"]);time.sleep(10)"
        with tempfile.TemporaryDirectory() as folder:
            code=control.supervise([sys.executable,'-c',child],Path(folder),{},startup=.7,watchdog=.4,shutdown=.2)
            report=json.loads(next(Path(folder).glob('exit-*.json')).read_text())
            self.assertNotEqual(code,0)
            self.assertEqual(report['outcome'],'startup_timeout')


if __name__=='__main__':
    unittest.main()
