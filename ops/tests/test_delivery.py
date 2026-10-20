import json
import os
import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import check
from ops.precommit import verify_staged


class DeliveryTests(unittest.TestCase):
    def test_docker_context_covers_every_inventory_file(self):
        catalog = json.loads(check.CATALOG.read_text(encoding='utf-8'))
        rules = (check.ROOT / 'ops/docker/Dockerfile.dockerignore').read_text(encoding='utf-8').splitlines()
        self.assertEqual(rules[0], '**')
        allowed = {line[1:] for line in rules if line.startswith('!')}
        missing = set(catalog['files']) - allowed
        self.assertFalse(missing, 'Docker context omits required inventory files: ' + ', '.join(sorted(missing)))

    def test_missing_suite_or_historical_native_entrypoint_is_refused(self):
        catalog = json.loads(check.CATALOG.read_text(encoding='utf-8'))
        for changes in ({'suites': ['application']},
                        {'entrypoints': {'check': 'check.py', 'deployment': '.audit/startup-r5-ops/deploy-graded-cooling.py', 'acceptance': None}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                check.validate_catalog(dict(catalog, **changes))

    def test_inventory_missing_file_or_wrong_hash_is_refused(self):
        catalog = json.loads(check.CATALOG.read_text(encoding='utf-8'))
        for replacement in ({}, dict(catalog['files'], **{'core.py': '0' * 64})):
            with self.assertRaisesRegex(ValueError, 'drift'):
                check.validate_catalog(dict(catalog, files=replacement))

    def test_local_success_never_sets_runtime_success(self):
        self.assertEqual(check.runtime_report(), dict(observed_at=None, installed='unknown',
            process='unknown', admission='unknown', control='unknown', ui='unknown', history='unknown',
            physical_acceptance='not_observed', full_wind='unconfirmed'))

    def test_readonly_page_and_recording_failure_are_separate_facts(self):
        report = check.runtime_report(dict(generated_at=100, control_active=False,
                                    history_recording=dict(writes=2, error='disk failed')), 101)
        self.assertEqual(report['control'], 'inactive_reported')
        self.assertEqual(report['ui'], 'responding')
        self.assertEqual(report['history'], 'failed_reported')
        self.assertEqual(report['installed'], 'unknown')
        self.assertEqual(check.runtime_report(dict(generated_at=100, control_active=True), 120)['control'], 'unknown')

    def test_missing_dependency_skip_cannot_pass(self):
        results = {key: dict(successful=True, tests=2, skipped=[]) for key in check.SUITES}
        results['application']['skipped'] = [dict(test='test', reason='miio missing')]
        self.assertEqual(check.outcome(results, list(check.SUITES)), 'incomplete')

    def test_windows_success_is_not_native_proof(self):
        results = {key: dict(successful=True, tests=1, skipped=[]) for key in check.SUITES}
        with patch.object(check, 'native_platform', return_value=False):
            self.assertEqual(check.outcome(results, list(check.SUITES)), 'offline_pass_native_pending')
        self.assertEqual(check.outcome({'application': results['application']}, ['application']), 'partial_pass_native_pending')
        self.assertEqual(check.outcome({}, list(check.SUITES)), 'failed')

    def test_platform_word_does_not_authorize_arbitrary_skip(self):
        results = {key: dict(successful=True, tests=2, skipped=[]) for key in check.SUITES}
        results['application']['skipped'] = [dict(id='unexpected', reason='Windows missing dependency')]
        self.assertEqual(check.outcome(results, list(check.SUITES)), 'incomplete')
        results['application']['tests'] = 1
        self.assertEqual(check.outcome(results, list(check.SUITES)), 'failed')

    def test_windows_specific_refusal_skip_is_allowed_only_on_posix(self):
        results = {key: dict(successful=True, tests=2, skipped=[]) for key in check.SUITES}
        results['startup_evidence']['skipped'] = [dict(
            id='test_startup_evidence.EvidenceTests.test_unsupported_windows_private_creation_is_explicit_refusal',
            reason='Windows refusal applies only to Windows')]
        with patch.object(check.os, 'name', 'posix'), patch.object(check, 'native_platform', return_value=False):
            self.assertEqual(check.outcome(results, list(check.SUITES)), 'offline_pass_native_pending')
        with patch.object(check.os, 'name', 'nt'):
            self.assertEqual(check.outcome(results, list(check.SUITES)), 'incomplete')

    def test_small_clock_skew_and_stale_status(self):
        self.assertEqual(check.runtime_report(dict(generated_at=101), 100)['ui'], 'responding')
        for stamp in (103, 90):
            self.assertNotEqual(check.runtime_report(dict(generated_at=stamp), 100)['ui'], 'responding')

    def test_unverified_staged_content_cannot_commit(self):
        local_env = subprocess.check_output(['git', 'rev-parse', '--local-env-vars'], text=True).splitlines()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            for name in local_env:
                os.environ.pop(name, None)
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=root, check=True)
            source = root / 'source.py'
            source.write_bytes(b'first\n')
            subprocess.run(['git', 'add', 'source.py'], cwd=root, check=True)
            verify_staged(root, ['source.py'])
            source.write_bytes(b'second\n')
            with self.assertRaisesRegex(ValueError, 'Staged source differs'):
                verify_staged(root, ['source.py'])
            with self.assertRaises(ValueError):
                verify_staged(root, ['missing.py'])


if __name__ == '__main__':
    unittest.main()
