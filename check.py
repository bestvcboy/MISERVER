import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / 'release_index.json'
SUITES = {
    'application': ('.', 'test_*.py'),
    'forward_contract': ('.audit/startup-r5-ops', 'test_forward_ops.py'),
    'startup_evidence': ('ops/startup_evidence', 'test_*.py'),
    'observer': ('ops/observer', 'test_*.py'),
    'delivery': ('ops/tests', 'test_*.py'),
}
REFERENCES = (
    '.audit/startup-r5-ops/admission_reader.py',
    '.audit/startup-r5-ops/deploy-graded-cooling.py',
    '.audit/startup-r5-ops/observation_duration.py',
    '.audit/startup-r5-ops/test_forward_ops.py',
    '.audit/startup-r5-ops/verify-graded-cooling-live.py',
    '.audit/startup-r5-ops/graded-cooling-release-manifest.json',
    '.audit/startup-r5-ops/50-forward-admission.conf',
    '.audit/cold-release-v14-r4/graded-cooling-release-manifest.json',
    '.audit/forward-ops-r1/observation_duration.py',
    '.audit/forward-ops-r1/verify-graded-cooling-live.py',
    '.audit/lower-airflow-live-trace.json',
    '.audit/background-probe-long-trace.json',
)
ROOT_NAMES = ('AGENTS.md', 'README.md', 'THIRD_PARTY_NOTICES.md', 'requirements.txt',
              'install.sh', 'dgx-thermal.service', 'ui.html', 'ui.js', 'ui.css',
              'test_fan_snapshot_transitions.json', '.gitignore', '.gitattributes',
              '.githooks/pre-commit', '.github/workflows/check.yml')
EXTENSIONS = {'.py', '.cjs', '.js', '.css', '.html', '.service', '.json', '.txt', '.md'}


def source_paths(root=ROOT):
    paths = set(ROOT_NAMES) | set(REFERENCES)
    paths.update(p.name for p in root.glob('*.py'))
    paths.update(p.relative_to(root).as_posix() for p in (root / 'ops').rglob('*')
                 if p.is_file() and p.suffix in EXTENSIONS and '__pycache__' not in p.parts)
    return sorted(paths)


def safe_source(root, name):
    path = root / name
    if name not in source_paths(root) or path.resolve() != path.absolute():
        raise ValueError('Source path is outside the current inventory or traverses a link')
    if not path.is_file() or path.is_symlink():
        raise ValueError('Source must be a regular file: ' + name)
    return path


def inventory(root=ROOT):
    return {name: hashlib.sha256(safe_source(root, name).read_bytes()).hexdigest()
            for name in source_paths(root)}


def validate_catalog(catalog, root=ROOT):
    if (catalog.get('schema') != 1 or catalog.get('state') != 'offline_candidate'
            or catalog.get('entrypoints') != {'check': 'check.py', 'deployment': None, 'acceptance': None}
            or catalog.get('suites') != list(SUITES)):
        raise ValueError('Current catalog has missing suites or an unsafe execution entrypoint')
    actual = inventory(root)
    if catalog.get('files') != actual:
        expected = catalog.get('files', {})
        changed = sorted(name for name in set(actual) | set(expected) if actual.get(name) != expected.get(name))
        raise ValueError('Source inventory drift: ' + ', '.join(changed))
    return actual


def native_platform():
    return sys.platform == 'linux' and platform.machine().lower() in ('aarch64', 'arm64')


def outcome(results, selected):
    if set(results) != set(selected):
        return 'failed'
    if any(not r.get('successful') or r.get('tests', 0) - len(r.get('skipped', [])) <= 0 for r in results.values()):
        return 'failed'
    skipped = [v for r in results.values() for v in r.get('skipped', [])]
    from ops.platform_skips import POSIX_SKIPS, WINDOWS_SKIPS
    allowed = WINDOWS_SKIPS if os.name == 'nt' else POSIX_SKIPS if os.name == 'posix' else set()
    unexpected = [v for v in skipped if v.get('id') not in allowed]
    if unexpected:
        return 'incomplete'
    if set(selected) != set(SUITES):
        return 'partial_pass_native_pending'
    if not native_platform():
        return 'offline_pass_native_pending'
    return 'native_offline_pass_physical_pending'


def runtime_report(status=None, observed_at=None):
    report = dict(observed_at=observed_at, installed='unknown', process='unknown', admission='unknown',
                  control='unknown', ui='unknown', history='unknown', physical_acceptance='not_observed',
                  full_wind='unconfirmed')
    if status is None:
        return report
    stamp = status.get('generated_at', status.get('server_time'))
    if type(stamp) not in (int, float) or not -2 <= observed_at - stamp <= 9:
        report['ui'] = 'responding_with_stale_or_invalid_status'
        return report
    report['ui'] = 'responding'
    if status.get('control_active') is False:
        report['control'] = 'inactive_reported'
    elif status.get('control_active') is True:
        report['control'] = 'active_reported'
    recorder = status.get('history_recording')
    if isinstance(recorder, dict):
        report['history'] = 'failed_reported' if recorder.get('error') else 'writing_reported' if recorder.get('writes', 0) > 0 else 'not_yet_recorded'
    elif status.get('acquisition_paused') is True:
        report['history'] = 'paused_reported'
    return report


def prerequisites():
    if sys.version_info[:2] not in ((3, 8), (3, 12)):
        raise ValueError('Startup evidence supports Python 3.8 or 3.12 only')
    if not shutil.which('node'):
        raise ValueError('Node is required for offline UI checks')
    for line in (ROOT / 'requirements.txt').read_text().splitlines():
        name, version = line.split('==')
        if importlib.metadata.version(name) != version:
            raise ValueError('Dependency version mismatch: ' + name)


def run_checks(selected, directory):
    results = {}
    for key in selected:
        location, pattern = SUITES[key]
        result_path = directory / (key + '.json')
        command = [sys.executable, '-B', str(ROOT / 'ops/run_suite.py'), location, pattern, str(result_path)]
        with (directory / (key + '.log')).open('w', encoding='utf-8') as log:
            try:
                process = subprocess.run(command, cwd=ROOT, stdout=log, stderr=log, timeout=900)
                result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.is_file() else {}
                result['successful'] = bool(process.returncode == 0 and result.get('successful'))
            except subprocess.TimeoutExpired:
                result = dict(successful=False, tests=0, error='timeout')
        results[key] = dict(result, command=command, log=str(directory / (key + '.log')))
        print(key + ': ' + ('passed' if results[key]['successful'] else 'failed'), flush=True)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='DGX current offline checks and factual public status. Never starts a service.')
    parser.add_argument('--refresh-inventory', action='store_true')
    parser.add_argument('--inventory-only', action='store_true')
    parser.add_argument('--suite', choices=tuple(SUITES), action='append')
    parser.add_argument('--require-native', action='store_true')
    parser.add_argument('--status-url', choices=('https://192.168.3.180:8910',))
    parser.add_argument('--ca-file', type=Path)
    args = parser.parse_args(argv)
    if args.refresh_inventory:
        catalog = dict(schema=1, candidate='retro-remediation-20261014', state='offline_candidate',
                       entrypoints=dict(check='check.py', deployment=None, acceptance=None),
                       suites=list(SUITES), files=inventory(),
                       reference_policy='R3/R5 ended attempts and native entrypoints are historical; fixed offline tests only')
        CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Inventory refreshed. No test, deployment or physical verdict is granted.')
        return 0
    if args.status_url:
        if args.ca_file is None:
            parser.error('--status-url requires --ca-file; TLS verification remains enabled')
        context = ssl.create_default_context(cafile=str(args.ca_file))
        with urlopen(args.status_url + '/api/status', context=context, timeout=5) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError('Public status response exceeds its budget')
        print(json.dumps(runtime_report(json.loads(raw), time.time()), ensure_ascii=False, indent=2))
        return 0
    catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
    validate_catalog(catalog)
    if args.inventory_only:
        print(json.dumps(dict(inventory='matched', files=len(catalog['files']), runtime=runtime_report())))
        return 0
    prerequisites()
    directory = ROOT / '.audit' / ('check-' + uuid.uuid4().hex)
    directory.mkdir()
    selected = list(dict.fromkeys(args.suite or SUITES))
    results = run_checks(selected, directory)
    validate_catalog(catalog)
    verdict = outcome(results, selected)
    report = dict(verdict=verdict, python=sys.version, platform=sys.platform, machine=platform.machine(),
                  suites=results, runtime=runtime_report(), physical_acceptance='not_run',
                  native_platform=native_platform(), catalog_sha256=hashlib.sha256(CATALOG.read_bytes()).hexdigest())
    path = directory / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(verdict=verdict, report=str(path)), ensure_ascii=False))
    return 1 if verdict in ('failed', 'incomplete') else 2 if args.require_native and not native_platform() else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, importlib.metadata.PackageNotFoundError) as error:
        print('Check refused: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
