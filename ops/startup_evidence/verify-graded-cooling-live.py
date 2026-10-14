import argparse
import hashlib
import json
import math
import os
import re
import ssl
import subprocess
import sys
import time
from types import ModuleType
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from urllib.request import urlopen
from startup_evidence import EvidenceRun, Stage


DURATION_SHA256 = '067db49da91943a35ac01a61cea7ce4cf7fa01ccd3f7a2128804685563736192'
_duration_path = Path(__file__).resolve().with_name('observation_duration.py')
assert _duration_path.is_file() and not _duration_path.is_symlink(), 'Missing or unsafe observation duration source'
_duration_source = _duration_path.read_bytes()
assert hashlib.sha256(_duration_source).hexdigest() == DURATION_SHA256, 'Observation duration hash mismatch'
_duration_module = ModuleType('_graded_observation_duration')
_duration_module.__name__ += '_' + str(id(_duration_module))
_duration_module.__file__ = str(_duration_path)
sys.modules[_duration_module.__name__] = _duration_module
exec(compile(_duration_source, str(_duration_path), 'exec'), _duration_module.__dict__)
ObservationDuration = _duration_module.ObservationDuration
minimum_observation_argument = _duration_module.minimum_observation_argument
del _duration_source


parser = argparse.ArgumentParser()
parser.add_argument('--expected-pid', required=True, type=int)
parser.add_argument('--deadline', required=True, type=float)
parser.add_argument('--minimum-observation-seconds', type=minimum_observation_argument, default=480)
args = parser.parse_args()
duration = ObservationDuration(args.minimum_observation_seconds)
assert args.expected_pid > 1, 'Invalid replacement PID'
assert math.isfinite(args.deadline), 'Invalid trial deadline'
assert 0 < args.deadline - time.time() <= 1200, 'Aggregate supervised trial deadline invalid or overbudget'
pid = str(args.expected_pid).encode()
root = Path('/home/bestvcboy/dgx-thermal')
stage = Path(__file__).resolve().parent


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def check_deadline():
    assert time.time() < args.deadline, 'Aggregate supervised trial deadline reached'


def timeout(limit):
    remaining = args.deadline - time.time()
    assert remaining > 0, 'Aggregate supervised trial deadline reached'
    return min(limit, remaining)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result, 'Duplicate JSON key in original trial deadline anchor'
        result[key] = value
    return result


def check_original_deadline():
    path = stage / 'trial-deadline.json'
    assert path.is_file() and not path.is_symlink(), 'Missing or unsafe original trial deadline anchor'
    assert path.stat().st_size <= 4096, 'Unbounded original trial deadline anchor'
    anchor = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    assert isinstance(anchor, dict) and set(anchor) == {'accepted_at', 'deadline'}, 'Invalid original trial deadline anchor'
    assert all(finite(anchor[key]) for key in ('accepted_at', 'deadline')), 'Invalid finite original trial deadline anchor'
    assert anchor['accepted_at'] <= time.time() and 540 < anchor['deadline'] - anchor['accepted_at'] <= 1200, 'Invalid original trial deadline budget'
    assert args.deadline == anchor['deadline'], 'Verifier cannot renew or change the original trial deadline'
    check_deadline()


def service_pids():
    result = set()
    target = os.fsencode(root / 'service.py')
    for process in Path('/proc').iterdir():
        check_deadline()
        if not process.name.isdigit():
            continue
        try:
            arguments = (process / 'cmdline').read_bytes().split(b'\0')
            if target in arguments or (
                any(argument in (b'service.py', b'./service.py') for argument in arguments)
                and (process / 'cwd').resolve(strict=True) == root
            ):
                result.add(int(process.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
    check_deadline()
    return result


def check_pid(phase):
    actual = subprocess.check_output(['systemctl', 'show', 'dgx-thermal.service', '-p', 'MainPID', '--value'],
                                     timeout=timeout(8)).strip()
    check_deadline()
    assert actual == pid, 'Replacement PID changed during ' + phase
    assert service_pids() == {args.expected_pid}, 'Expected exactly one service writer during ' + phase


def age(now, stamp, limit, message):
    assert finite(now) and finite(stamp) and 0 <= now - stamp <= limit, message


@dataclass(frozen=True)
class Window:
    pid: int
    start_ns: int

    @property
    def end_ns(self):
        return self.start_ns + 30_000_000_000


def bind_window():
    check_pid('before native startup binding')
    stamp = subprocess.check_output(
        ['systemctl', 'show', 'dgx-thermal.service', '-p', 'ExecMainStartTimestampMonotonic', '--value'],
        timeout=timeout(8)).strip()
    check_deadline()
    check_pid('after native startup binding')
    assert re.fullmatch(rb'[1-9][0-9]*', stamp), 'Invalid native unit startup timestamp'
    start_ns = int(stamp) * 1000
    assert start_ns <= time.monotonic_ns(), 'Future native unit startup timestamp'
    return Window(args.expected_pid, start_ns)


def snapshot(status, hosts=None, historical=False, incomplete=False, initializing=False):
    check_deadline()
    if historical:
        wall = status['snapshot_at']
        age(wall, status['ts'], 6, 'Invalid historical snapshot age')
        mono = status['learning']['mono']
        assert finite(mono), 'Invalid historical monotonic timestamp'
        numerator, denominator = mono.as_integer_ratio()
        thermal_ns = numerator * 1_000_000_000
        local_wall, local_mono = time.time(), time.monotonic()
        age(local_mono - (local_wall - wall), mono, 6, 'Historical monotonic clock does not match the server clock')
        assert (status['learning']['healthy'] is True and type(status['learning']['dropped_records']) is int
                and status['learning']['dropped_records'] == 0), 'Historical storage failure'
        assert status['control']['manual_level'] is None, 'Historical manual override'
        stamp = status['ts']
    else:
        wall = status['server_time']
        age(time.time(), wall, 6, 'Invalid server clock age')
        mono = time.monotonic()
        thermal_ns, denominator = time.monotonic_ns(), 1
        assert finite(status['controller_age_seconds']) and 0 <= status['controller_age_seconds'] <= 6, 'Stale controller'
        assert not status['storage_error'] and not status['settings_error'] and not status['learning']['error'], 'Storage or settings failure'
        assert status['settings'] == {'retention_days': 7, 'max_log_mb': 4096, 'manual_level': None}, 'Protected settings changed'
        assert type(status['dropped_records']) is int and status['dropped_records'] == 0, 'Dropped records'
        stamp = wall
    fan, nodes = status['fan'], status['nodes']
    assert isinstance(nodes, list) and len(nodes) <= 4, 'Invalid node inventory'
    assert all(isinstance(node, dict) for node in nodes), 'Invalid node record'
    identities = [node.get('host') for node in nodes]
    assert all(isinstance(host, str) and host for host in identities) and len(set(identities)) == len(identities), 'Duplicate or invalid node identities'
    if not incomplete:
        assert len(nodes) == 4 and all(node['online'] is True for node in nodes), 'Incomplete required telemetry'
    if hosts is not None and not incomplete:
        assert set(identities) == hosts, 'Node identities changed'
    for node in nodes:
        assert all(key not in node or node[key] is False for key in ('thermal_throttle', 'ambiguous_sensors')), 'Available node thermal or sensor-identity fault'
        age(wall, node.get('source_ts'), 6, 'Stale or future sensor source timestamp')
        age(wall, node.get('sampled_at'), 6, 'Stale or future sampled timestamp')
        age(mono, node.get('requested_mono'), 6, 'Stale or future requested monotonic timestamp')
        assert all(finite(node.get(key)) and node[key] >= 0
                   for key in ('cpu_temp', 'gpu_temp', 'board_temp')), 'Supervised low-load temperature boundary exceeded'
        assert node['board_temp'] < 65, 'Supervised low-load temperature boundary exceeded'
        assert finite(node.get('power_w')) and 0 <= node['power_w'] < 80, 'Power guard or unknown power'
        assert isinstance(node.get('power_source'), str) and node['power_source'] and node['power_source'] != '不可用', 'Missing power source'
        assert all(finite(node.get(key)) and 0 <= node[key] <= 100 for key in ('cpu_load', 'gpu_load')), 'Load telemetry is missing or invalid'
    requested = status['requested_level']
    assert type(requested) is int and 0 <= requested <= 16, 'Invalid requested gear'
    if initializing:
        assert all(node[key] < 55 for node in nodes for key in ('cpu_temp', 'gpu_temp')), 'Supervised low-load temperature boundary exceeded'
        assert requested == 16 and fan.get('configured') is False and fan.get('fault') is False, 'Unsafe initial motor state'
        assert fan.get('confirmed') is False and fan.get('level') is None and fan.get('rpm') is None, 'Unsafe initial motor state'
        decision = status['decision']
        initial = decision == {'level': 16, 'reasons': ['启动全风保护']}
        first_feedback = (decision.get('scenario') == 'protect' and decision.get('scenario_detail') in
                          ('风机反馈中断，重新建立全风保护基线', '服务或执行条件异常，重新建立全风保护基线'))
        assert initial or first_feedback, 'Controller protection during quiet trial'
        assert fan.get('error') in (None, '', '尚未绑定本地执行凭据，无法执行风力控制'), 'Motor is unavailable or faulted'
        return None
    assert fan.get('configured') is True and fan.get('fault') is False, 'Motor is unavailable or faulted'
    assert fan.get('power') == 'on' and fan.get('mode') == 'favorite', 'Motor power or mode changed'
    assert type(fan.get('level')) is int and 0 <= fan['level'] <= 16, 'Invalid physical gear'
    assert finite(fan.get('rpm')) and fan['rpm'] > 0, 'Motor is not turning'
    age(wall, fan.get('verified_at'), 9, 'Stale or future motor readback')
    readback_mono = fan.get('observed_mono')
    age(mono, readback_mono, 9, 'Stale or future monotonic motor readback')
    assert fan.get('execution') in ('matched', 'pending'), 'Invalid motor execution state'
    assert type(fan.get('confirmed')) is bool, 'Invalid motor confirmation'
    if fan['execution'] == 'matched':
        assert fan['confirmed'] is True and fan['level'] == requested, 'Matched motor differs from request or is unconfirmed'
    else:
        assert fan['confirmed'] is False, 'Pending motor incorrectly claims confirmation'
    if 'attempts' in fan:
        assert type(fan['attempts']) is int and 0 <= fan['attempts'] <= 2, 'Invalid motor attempt budget'
    decision = status['decision']
    assert decision['retry_blocked'] is False, 'Historical veto remains'
    assert isinstance(decision['scenario'], str), 'Missing controller scenario'
    assert isinstance(decision['reasons'], list) and all(isinstance(reason, str) for reason in decision['reasons']), 'Invalid reasons'
    trial = decision.get('execution_trial', decision.get('trial'))
    execution_deadline = None
    destination_requested_at = None
    requested_at = None
    if trial is not None:
        assert isinstance(trial, dict), 'Invalid public trial'
        assert {'from', 'to', 'kind', 'status', 'observation_elapsed', 'outcome', 'deadline', 'elapsed'} <= trial.keys(), 'Incomplete public trial'
        assert all(type(trial[key]) is int and 0 <= trial[key] <= 16 for key in ('from', 'to')), 'Invalid public trial gears'
        assert all(finite(trial[key]) and trial[key] >= 0 for key in ('deadline', 'elapsed')), 'Invalid public trial timing'
        assert (finite(trial['observation_elapsed']) and trial['observation_elapsed'] >= 0
                or trial['status'] == 'pending' and trial['observation_elapsed'] is None), 'Invalid public trial observation timing'
        assert all(finite(trial.get(key)) for key in ('requested_at', 'original_deadline', 'destination_requested_at')), 'Missing finite exact public trial request timestamps; candidate gate closed'
        requested_at = trial['requested_at']
        execution_deadline = trial['original_deadline']
        destination_requested_at = trial['destination_requested_at']
        assert 0 <= requested_at <= destination_requested_at <= mono, 'Invalid exact destination request timestamp'
        assert execution_deadline == requested_at + 60, 'Invalid original60-second trial deadline'
        if fan['execution'] == 'matched':
            assert readback_mono > destination_requested_at, 'Matched readback does not postdate the exact destination request'
    readback_numerator, readback_denominator = readback_mono.as_integer_ratio()
    startup_fullwind = (len(nodes) == 4 and all(node['online'] is True for node in nodes)
                        and fan['execution'] == 'matched' and fan['confirmed'] is True
                        and fan['level'] == requested == 16
                        and readback_numerator * 1_000_000_000 > _window.start_ns * readback_denominator
                        and _window.start_ns * denominator <= thermal_ns < _window.end_ns * denominator)
    cpu_gpu_limit = 65 if startup_fullwind else 55
    assert all(node[key] < cpu_gpu_limit for node in nodes for key in ('cpu_temp', 'gpu_temp')), 'Supervised low-load temperature boundary exceeded'
    value = fan.get('ambient_c')
    ambient_fresh = (finite(value) and fan.get('ambient_source') == 'purifier:70632677:temp_dec'
                     and finite(fan.get('ambient_verified_at')) and 0 <= wall - fan['ambient_verified_at'] <= 9)
    check_deadline()
    return {'ts': stamp, 'snapshot_at': wall, 'mono': mono, 'readback_mono': readback_mono,
            'execution_deadline': execution_deadline,
            'destination_requested_at': destination_requested_at, 'requested_at': requested_at,
            'ambient_c': value, 'ambient_fresh': ambient_fresh, 'ambient_verified_at': fan.get('ambient_verified_at'),
            'gear': fan['level'], 'requested': requested, 'rpm': fan['rpm'], 'confirmed': fan['confirmed'],
            'configured': fan['configured'], 'power': fan['power'], 'mode': fan['mode'], 'execution': fan['execution'],
            'verified_at': fan['verified_at'], 'fault': fan['fault'], 'scenario': decision['scenario'],
            'scenario_detail': decision.get('scenario_detail', ''), 'reasons': decision['reasons'],
            'service_handoff': (decision.get('scenario') == 'protect'
                                and decision.get('scenario_detail') == '执行期间触发保护，读回恢复后仍需重新建立安全基线'
                                and fan.get('protection_interrupted') is True
                                and decision.get('trial') is None and decision.get('execution_trial') is None
                                and not any(decision.get('trend_complete', {}).values())),
            'nodes': nodes, 'telemetry_complete': len(nodes) == 4 and all(node['online'] is True for node in nodes),
            'thermal_complete': (isinstance(decision.get('trend_complete'), dict)
                                 and set(decision['trend_complete']) == set(identities)
                                 and len(identities) == 4 and all(value is True for value in decision['trend_complete'].values()))}


class StartupPhase(Enum):
    WAITING = 'waiting'
    MATCHED16_QUALIFICATION = 'matched16qualification'
    CLOSED = 'closed'


@dataclass(frozen=True)
class Startup:
    phase: StartupPhase
    started: float
    deadline: float
    initial_readback: float
    waited: bool
    earned: float = 0
    last_credit: object = None


@dataclass(frozen=True)
class ColdProgress:
    started: float
    gear: int


class Timeline:
    def __init__(self):
        self.previous = None
        self.sources = {}
        self.pending_since = None
        self.pending_deadline = None
        self.request_after = None
        self.last_decrease = None
        self.startup = None

    @property
    def startup_open(self):
        return self.startup is not None and self.startup.phase != StartupPhase.CLOSED

    def add(self, row):
        previous = self.previous
        now = row['mono']
        matched = row['execution'] == 'matched'
        if previous is None:
            started = row['requested_at'] if not matched and row['requested_at'] is not None else now
            phase = (StartupPhase.MATCHED16_QUALIFICATION if matched and row['gear'] == 16
                     else StartupPhase.WAITING) if row['requested'] == 16 else StartupPhase.CLOSED
            self.startup = Startup(phase, started, now + 90, row['readback_mono'], not matched)
            if not matched:
                self.pending_since = now
                self.request_after = row['readback_mono']
        else:
            assert row['snapshot_at'] >= previous['snapshot_at'] and now >= previous['mono'], 'Snapshot clock went backwards'
            assert row['readback_mono'] >= previous['readback_mono'], 'Motor readback went backwards'
            if row['destination_requested_at'] is not None and previous['destination_requested_at'] is not None:
                assert row['destination_requested_at'] >= previous['destination_requested_at'], 'Exact destination request timestamp went backwards'
            if row['requested'] < previous['requested']:
                assert row['requested'] == previous['requested'] - 1, 'Release skipped an adjacent request across pending rows'
                assert self.pending_since is None, 'Second descent before the pending request was confirmed'
            if row['gear'] < previous['gear']:
                assert row['gear'] == previous['gear'] - 1, 'Physical descent skipped an adjacent gear'
                assert self.last_decrease is None or row['readback_mono'] - self.last_decrease >= 30, 'Physical downsteps less than30 seconds apart'
                self.last_decrease = row['readback_mono']
            changed = row['requested'] != previous['requested']
            if changed:
                assert row['destination_requested_at'] is not None, 'Exact destination request timestamp missing for transition; candidate gate closed'
                self.request_after = row['destination_requested_at']
                if previous['destination_requested_at'] is not None:
                    assert self.request_after > previous['destination_requested_at'], 'Superseded destination request timestamp did not advance'
                if self.pending_since is None:
                    self.pending_since = row['requested_at']
            elif not matched and self.pending_since is None:
                self.pending_since = row['requested_at'] if row['requested_at'] is not None else now
                self.request_after = row['destination_requested_at'] if row['destination_requested_at'] is not None else previous['readback_mono']
            if matched and (changed or self.pending_since is not None):
                assert row['readback_mono'] > previous['readback_mono'] and row['readback_mono'] > self.request_after, 'Transition readback must advance and postdate the request'
        if self.pending_since is not None:
            assert row['execution_deadline'] is not None, 'Exact public trial deadline missing for pending execution; candidate gate closed'
            original = row['execution_deadline']
            if self.pending_deadline is None:
                self.pending_deadline = original
            else:
                assert original == self.pending_deadline, 'Original60-second pending execution deadline changed during supersession'
            assert now < self.pending_deadline, 'Original60-second pending execution deadline expired'
            if matched:
                self.pending_since = None
                self.pending_deadline = None
        for node in row['nodes']:
            prior = self.sources.get(node['host'])
            source = node['source_ts']
            if prior is not None:
                assert source >= prior[0], 'Sensor source timestamp went backwards'
                assert source > prior[0] or now - prior[1] <= 6, 'Sensor source timestamp stopped advancing'
            if prior is None or source > prior[0]:
                self.sources[node['host']] = (source, now)
        startup = self.startup
        if self.startup_open:
            assert now < startup.deadline, 'Startup qualification exceeded its fixed90-second observer budget'
            if startup.phase == StartupPhase.WAITING and matched and row['gear'] == 16 and row['readback_mono'] > startup.initial_readback:
                startup = replace(startup, phase=StartupPhase.MATCHED16_QUALIFICATION)
            if startup.phase == StartupPhase.MATCHED16_QUALIFICATION:
                assert matched and row['gear'] == row['requested'] or row['requested'] != 16, 'Controller protection during quiet trial'
                credit = (row['telemetry_complete'] and row['thermal_complete'] and row['gear'] == 16
                          and (matched or row['requested'] == 15)
                          and previous is not None and previous['telemetry_complete'] and previous['thermal_complete']
                          and previous['execution'] == 'matched' and previous['gear'] == previous['requested'] == 16
                          and row['readback_mono'] > previous['readback_mono']
                          and all(node['source_ts'] > next(prior['source_ts'] for prior in previous['nodes']
                                                         if prior['host'] == node['host']) for node in row['nodes']))
                earned = startup.earned
                stamp = row['readback_mono']
                if credit and startup.last_credit is not None and 0 < stamp - startup.last_credit <= 6:
                    earned += stamp - startup.last_credit
                ready = row['telemetry_complete'] and row['thermal_complete'] and row['gear'] == row['requested'] == 16 and matched
                startup = replace(startup, earned=earned, last_credit=stamp if credit or ready else None)
            if row['requested'] != 16:
                assert startup.phase == StartupPhase.MATCHED16_QUALIFICATION and startup.earned >= 30 - 1e-6, 'Startup descent before30 seconds of complete matched16 baseline'
                startup = replace(startup, phase=StartupPhase.CLOSED, last_credit=None)
            self.startup = startup
        detail = row['scenario_detail']
        waiting = detail in ('启动或故障恢复，等待实际16档确认', '启动或故障恢复，等待实际 16 档确认')
        baseline = re.fullmatch(r'启动新基线 earned=([0-9]+(?:\.[0-9]+)?)/30s，完整匹配确认后才允许下降', detail)
        baseline = baseline is not None and finite(float(baseline.group(1))) and float(baseline.group(1)) < 30
        allowed = (self.startup_open and row['requested'] == 16 and now < startup.deadline
                   and (waiting and startup.phase == StartupPhase.WAITING
                        or matched and row['gear'] == 16 and startup.phase == StartupPhase.MATCHED16_QUALIFICATION
                        and (baseline or row['service_handoff'] and now - startup.started < 15)))
        assert row['scenario'] != 'protect' or allowed, 'Controller protection during quiet trial'
        row['startup'] = self.startup_open
        row['startup_phase'] = self.startup.phase.value
        self.previous = row


def cold_behavior(records):
    from statistics import median

    @dataclass(frozen=True)
    class EdgeContext:
        source: str
        cpu_min: float
        cpu_max: float
        gpu_min: float
        gpu_max: float
        power_min: float
        power_max: float

    @dataclass(frozen=True)
    class EdgeArmed:
        gear: int
        reference: dict
        context: dict

    @dataclass(frozen=True)
    class EdgeHolding:
        edge: EdgeArmed
        detected: float
        deadline: float
        points: dict
        earned: float = 0
        last_credit: object = None
        invalidated: bool = False

    @dataclass(frozen=True)
    class EdgeSpent:
        gear: int

    def settled(points, now):
        if len(points) != 12:
            return False
        for (_, key), samples in points.items():
            if (len(samples) < 5 or samples[-1][0] != now
                    or samples[-1][0] - samples[0][0] < 30
                    or any(not 0 < b[0] - a[0] <= 6 for a, b in zip(samples, samples[1:]))):
                return False
            slopes = [(b[1] - a[1]) / (b[0] - a[0])
                      for index, a in enumerate(samples) for b in samples[index + 1:]
                      if b[0] - a[0] >= 15]
            if not slopes:
                return False
            slope = median(slopes)
            if slope >= .02:
                return False
            recent = samples[-3:]
            coherent = 0
            if all(b[0] > a[0] and b[1] > a[1] for a, b in zip(recent, recent[1:])):
                coherent = min((b[1] - a[1]) / (b[0] - a[0])
                               for a, b in zip(recent, recent[1:]))
            ceiling = 62 if key == 'board_temp' else 52
            if samples[-1][1] + 30 * max(coherent, slope, 0) > ceiling:
                return False
        return True

    runs, current = [], []
    noncold, environmental_noncold = set(), set()
    previous = None
    thermal = {}
    edge = None
    obligation = None
    confirmed = None
    cold_entered = False
    for row in records:
        environmental = True
        advancing = previous is None
        if previous is not None:
            advancing = all(node['source_ts'] > next(prior['source_ts'] for prior in previous['nodes'] if prior['host'] == node['host']) for node in row['nodes'])
        measured = (previous is not None and advancing and row['mono'] > previous['mono']
                    and row['telemetry_complete'])
        qualified = (measured and row['execution'] == 'matched' and row['confirmed'] is True
                     and row['gear'] == row['requested']
                     and row['readback_mono'] > previous['readback_mono']
                     and row['verified_at'] > previous['verified_at']
                     and (row['destination_requested_at'] is None
                          or row['readback_mono'] > row['destination_requested_at']))
        descending = previous is not None and (row['requested'] < previous['requested']
                                               or row['gear'] < previous['gear'])
        if isinstance(edge, EdgeHolding) and row['mono'] < edge.deadline:
            assert not descending, 'New descent during independently observed safety hold'
        entry = all(node['cpu_temp'] <= 45 and node['gpu_temp'] <= 45 and node['board_temp'] <= 55 for node in row['nodes'])
        if entry:
            cold_entered = True
        if any(node['cpu_temp'] > 47 or node['gpu_temp'] > 47 or node['board_temp'] > 55 for node in row['nodes']):
            cold_entered = False
        if not cold_entered:
            environmental = False
            environmental_noncold.add('temperature outside cold entry band')
        for node in row['nodes']:
            if not all(0 <= node[key] < 10 for key in ('cpu_load', 'gpu_load')):
                environmental = False
                environmental_noncold.add('load outside cold entry band')
            if previous is not None:
                prior = next(item for item in previous['nodes'] if item['host'] == node['host'])
                if abs(node['power_w'] - prior['power_w']) > 10 or node['power_source'] != prior['power_source']:
                    environmental = False
                    environmental_noncold.add('power changed by more than10W between observations or source changed')
        credible = False
        for node in row['nodes']:
            for key in ('cpu_temp', 'gpu_temp', 'board_temp'):
                identity = node['host'], key
                points = thermal.setdefault(identity, [])
                if not advancing or previous is not None and row['ts'] - previous['ts'] > 6:
                    points.clear()
                if advancing:
                    points.append((row['ts'], node[key]))
                points[:] = [point for point in points if row['ts'] - point[0] <= 30]
                if len(points) >= 3:
                    recent = points[-3:]
                    ongoing = all(b[0] > a[0] and b[1] > a[1] for a, b in zip(recent, recent[1:]))
                    gain = points[-1][1] - points[0][1]
                    span = points[-1][0] - points[0][0]
                    rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(recent, recent[1:])]
                    credible = credible or ongoing and ((span >= 27 and gain >= .2 and gain / span >= .005)
                                                        or recent[-1][1] - recent[0][1] >= .5 and min(rates) >= .05)
        basis = edge.edge if isinstance(edge, EdgeHolding) else edge
        if isinstance(basis, EdgeArmed):
            if row['gear'] > basis.gear or row['requested'] > basis.gear:
                edge = EdgeSpent(basis.gear)
            elif measured:
                context = {}
                valid = environmental and {node['host'] for node in row['nodes']} == set(basis.context)
                deltas = []
                for node in row['nodes']:
                    prior = basis.context.get(node['host'])
                    if prior is None:
                        valid = False
                        continue
                    bounds = EdgeContext(node['power_source'],
                        min(prior.cpu_min, node['cpu_load']), max(prior.cpu_max, node['cpu_load']),
                        min(prior.gpu_min, node['gpu_load']), max(prior.gpu_max, node['gpu_load']),
                        min(prior.power_min, node['power_w']), max(prior.power_max, node['power_w']))
                    context[node['host']] = bounds
                    valid = (valid and bounds.source == prior.source
                             and bounds.cpu_max - bounds.cpu_min <= 5
                             and bounds.gpu_max - bounds.gpu_min <= 5
                             and bounds.power_max - bounds.power_min <= 10)
                    deltas.extend(node[key] - basis.reference[(node['host'], key)]
                                  for key in ('cpu_temp', 'gpu_temp', 'board_temp'))
                spend = not valid or max(deltas, default=0) > 2
                if isinstance(edge, EdgeHolding):
                    edge = replace(edge, invalidated=edge.invalidated or spend)
                    spend = edge.invalidated and qualified and not descending
                if spend:
                    edge = EdgeSpent(basis.gear)
                else:
                    basis = replace(basis, context=context)
                    if isinstance(edge, EdgeHolding):
                        edge = replace(edge, edge=basis)
                    else:
                        edge = basis
                        if qualified and row['gear'] == row['requested'] == basis.gear and max(deltas) > 0:
                            points = {(node['host'], key): [(row['mono'], node[key])]
                                      for node in row['nodes'] for key in ('cpu_temp', 'gpu_temp', 'board_temp')}
                            edge = EdgeHolding(basis, row['mono'], row['mono'] + 90, points,
                                               last_credit=row['mono'])
        if isinstance(edge, EdgeHolding):
            if row['mono'] >= edge.deadline:
                edge = EdgeSpent(edge.edge.gear)
            elif qualified and row['gear'] == row['requested'] == edge.edge.gear and not descending:
                earned = edge.earned
                stamp = row['mono']
                if (edge.last_credit is not None and 0 < stamp - edge.last_credit <= 6
                        and 0 < row['mono'] - previous['mono'] <= 6
                        and 0 < row['ts'] - previous['ts'] <= 6):
                    earned += stamp - edge.last_credit
                for node in row['nodes']:
                    for key in ('cpu_temp', 'gpu_temp', 'board_temp'):
                        points = edge.points[(node['host'], key)]
                        if points[-1][0] < row['mono']:
                            points.append((row['mono'], node[key]))
                        while len(points) > 1 and points[1][0] <= row['mono'] - 30:
                            points.pop(0)
                edge = replace(edge, earned=earned, last_credit=stamp)
                if earned >= 60 and not credible and settled(edge.points, row['mono']):
                    edge = EdgeSpent(edge.edge.gear)
            else:
                edge = replace(edge, last_credit=None)
        holding = credible or isinstance(edge, EdgeHolding)
        if credible:
            environmental_noncold.add('credible continuing temperature rise')
            noncold.add('credible continuing temperature rise')
        elif holding:
            environmental_noncold.add('bounded post-descent rebound observation')
            noncold.add('bounded post-descent rebound observation')
        if previous is not None and environmental and not holding and not row['startup'] and not previous['startup']:
            assert row['requested'] <= previous['requested'], 'Cold release increased while continuously eligible, including pending or zero gear'
        if previous is not None and holding and previous.get('safety_hold'):
            assert not descending, 'New descent during independently observed safety hold'
        if (qualified and confirmed is not None and row['gear'] == confirmed - 1
                and row['gear'] == previous['gear'] - 1):
            reference = {(node['host'], key): node[key] for node in row['nodes']
                         for key in ('cpu_temp', 'gpu_temp', 'board_temp')}
            context = {node['host']: EdgeContext(node['power_source'],
                       node['cpu_load'], node['cpu_load'], node['gpu_load'], node['gpu_load'],
                       node['power_w'], node['power_w']) for node in row['nodes']}
            edge = EdgeArmed(row['gear'], reference, context)
        if row['execution'] == 'matched':
            confirmed = row['gear']
        row['safety_hold'] = holding
        eligible = (environmental and not holding and advancing and row['execution'] == 'matched'
                    and (previous is None or qualified))
        if row['startup'] or advancing and (not environmental or holding):
            obligation = None
        elif obligation is None and eligible:
            obligation = ColdProgress(row['ts'], row['gear'])
        elif obligation is not None:
            progressed = (eligible and previous is not None
                          and row['readback_mono'] > previous['readback_mono']
                          and row['gear'] < obligation.gear)
            assert obligation.gear == 0 or row['ts'] - obligation.started <= 60, 'Cold release did not progress within60 seconds; continuously eligible cold trial did not reach actual5 or lower'
            if progressed:
                obligation = ColdProgress(row['ts'], row['gear'])
        if not advancing:
            noncold.add('sensor observation did not advance')
        if row['execution'] != 'matched':
            noncold.add('motor transition pending')
        if eligible:
            current.append(row)
        elif current:
            runs.append(current)
            current = []
        previous = row
    if current:
        runs.append(current)
    noncold.update(environmental_noncold)
    for run in runs:
        if run[-1]['ts'] - run[0]['ts'] < 90:
            continue
        assert any('[cold-release]' in ' '.join([row['scenario_detail']] + row['reasons']) for row in run), 'Missing actual cold-release mode evidence'
        anchor = run[0]
        for row in run[1:]:
            if row['requested'] < anchor['requested']:
                assert row['ts'] - anchor['ts'] <= 60, 'Cold release did not progress within60 seconds'
                anchor = row
            elif anchor['requested'] > 0 and not row['startup']:
                assert row['ts'] - anchor['ts'] <= 60, 'Cold release did not progress within60 seconds'
    final_run = runs[-1] if runs and runs[-1][-1] is records[-1] else []
    continuous = bool(final_run and final_run[-1]['ts'] - final_run[0]['ts'] >= 360)
    if continuous or not environmental_noncold:
        assert any('[cold-release]' in ' '.join([row['scenario_detail']] + row['reasons']) for row in records), 'Missing actual cold-release mode evidence'
        assert records[-1]['gear'] <= 5, 'Continuously eligible cold trial did not reach actual5 or lower'
    return {'continuously_eligible': continuous or not noncold,
            'cold_environment_throughout': not environmental_noncold,
            'eligible_run_seconds': [run[-1]['ts'] - run[0]['ts'] for run in runs],
            'noncold_evidence': sorted(noncold)}


evidence = EvidenceRun.create(stage / 'startup-evidence-private', args.deadline,
                              snapshot.__code__, Timeline.add.__code__)
evidence.install_cleanup()
check_deadline()
check_original_deadline()
manifest = json.loads((stage / 'graded-cooling-release-manifest.json').read_text())
for name, digest in manifest['new'].items():
    check_deadline()
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
context = ssl.create_default_context(cafile=str(root / 'state/tls.crt'))
check_deadline()


def request(route):
    check_pid('before ' + route)
    with urlopen('https://192.168.3.180:8910' + route, context=context, timeout=timeout(8)) as response:
        result = response.read()
    check_deadline()
    check_pid('after ' + route)
    return result


_window = bind_window()
timeline = Timeline()
hosts = None
initial_motor_seen = False
initializing_seen = False
readiness_started = time.monotonic()
for attempt in range(15):
    check_deadline()
    try:
        ready = json.loads(request('/api/status'))
        with evidence.observe(Stage.READINESS, ready, timeline) as observation:
            initializing = (not initial_motor_seen and ready.get('fan', {}).get('configured') is False
                            and ready['fan'].get('level') is None and ready['fan'].get('rpm') is None)
            row = snapshot(ready, hosts, incomplete=True, initializing=initializing)
            observation.derived(row)
            if row is None:
                initializing_seen = True
                assert time.monotonic() - readiness_started < 15, 'Initial motor discovery exceeded fixed15 seconds'
                time.sleep(timeout(1))
                continue
            if initializing_seen and not initial_motor_seen:
                assert time.monotonic() - readiness_started < 15, 'Initial motor discovery exceeded fixed15 seconds'
                assert row['requested'] == 16, 'Initial fullwind intent disappeared before physical qualification'
            initial_motor_seen = True
            timeline.add(row)
            if len(ready['nodes']) == 4 and all(node['online'] is True for node in ready['nodes']) and not row['service_handoff']:
                if hosts is None:
                    hosts = {node['host'] for node in ready['nodes']}
                assert {node['host'] for node in ready['nodes']} == hosts, 'Node identities changed during readiness'
                if json.loads((root / 'state/learning/checkpoint.json').read_text())['controller'] == 14:
                    check_deadline()
                    break
    except OSError:
        check_deadline()
    time.sleep(timeout(1))
    check_deadline()
else:
    raise AssertionError('Service did not become ready within the bounded startup window')

for route, name in (('/', 'ui.html'), ('/ui.js', 'ui.js'), ('/ui.css', 'ui.css')):
    assert hashlib.sha256(request(route)).digest() == hashlib.sha256((root / name).read_bytes()).digest()
assert json.loads((root / 'state/learning/checkpoint.json').read_text())['controller'] == 14
check_deadline()
records = []
if duration.minimum_seconds != 480:
    assert args.deadline - time.time() > duration.minimum_launch_remaining, 'Insufficient original budget for observation acquisition'
started = time.monotonic()
for index in range(duration.status_samples):
    check_deadline()
    status = json.loads(request('/api/status'))
    with evidence.observe(Stage.LIVE, status, timeline) as observation:
        row = snapshot(status, hosts)
        observation.derived(row)
        timeline.add(row)
        records.append(row)
    if index < duration.status_samples - 1:
        time.sleep(timeout(3))
observed_seconds = time.monotonic() - started
assert observed_seconds >= duration.minimum_seconds, 'Observation window shorter than' + str(duration.minimum_seconds) + ' seconds'
status_intervals = [b['ts'] - a['ts'] for a, b in zip(records, records[1:])]
assert all(2.5 <= interval <= 3.5 for interval in status_intervals), 'Status records are not at3-second cadence'
assert records[-1]['ts'] - records[0]['ts'] >= duration.minimum_seconds, 'Status record coverage shorter than' + str(duration.minimum_seconds) + ' seconds'
check_deadline()
history_route = '/api/history?hours=0.2'
if duration.minimum_seconds != 480:
    until, since = records[-1]['snapshot_at'], records[0]['ts'] - 3.5
    assert finite(until) and finite(since) and until > since, 'Invalid observation history query bounds'
    hours = (until - since) / 3600
    assert finite(hours) and hours > 0, 'Invalid observation history query duration'
    history_route = '/api/history?hours=' + repr(hours) + '&until=' + repr(until)
history = json.loads(request(history_route))
assert history.get('samples_truncated', False) is False, 'Historical observation window truncated'
assert isinstance(history['samples'], list) and all(finite(row.get('ts')) for row in history['samples']), 'Invalid history timestamps'
assert len(history['samples']) <= 200, 'Historical observation window exceeds raw200-row cap'
rows = sorted((row for row in history['samples'] if row['ts'] >= records[0]['ts']), key=lambda row: row['ts'])
assert len(rows) >= duration.history_min_samples and rows[-1]['ts'] - rows[0]['ts'] >= duration.minimum_seconds, 'History coverage shorter than' + str(duration.minimum_seconds) + ' seconds'
assert rows[0]['ts'] <= records[0]['ts'] + 3.5 and rows[-1]['ts'] >= records[-1]['ts'] - 3.5, 'History does not cover observed window'
intervals = [b['ts'] - a['ts'] for a, b in zip(rows, rows[1:])]
assert all(2.5 <= interval <= 3.5 for interval in intervals), 'History records are not at3-second cadence'
history_timeline = Timeline()
history_records = []
for stored in rows:
    with evidence.observe(Stage.HISTORY, stored, history_timeline) as observation:
        assert stored['snapshot_at'] <= time.time(), 'Future historical snapshot'
        row = snapshot(stored, hosts, historical=True)
        observation.derived(row)
        history_timeline.add(row)
        history_records.append(row)
fresh_rows = [row for row in history_records if row['ambient_fresh']]
assert len(fresh_rows) >= len(rows) * .9, 'Too many missing environmental readings'
assert sum(row['ambient_fresh'] for row in records) >= len(records) * .9
assert records[-1]['confirmed'] is True and records[-1]['execution'] == 'matched', 'Final motor readback is not confirmed'
assert records[-1]['gear'] == records[-1]['requested'], 'Final gear does not match current request'
assert records[-1]['gear'] <= 10, 'Quiet trial did not reach the normal band'
cold_evidence = cold_behavior(records)
history_cold_evidence = cold_behavior(history_records)
check_pid('completion')
assert json.loads((root / 'state/learning/checkpoint.json').read_text())['controller'] == 14
check_deadline()
report = {'controller': 14, 'deadline': args.deadline, 'pid': int(pid), 'observed_seconds': observed_seconds,
          'cold_evidence': cold_evidence, 'history_cold_evidence': history_cold_evidence, 'records': records,
          'history_samples': rows, 'history_intervals': intervals}
report_text = json.dumps(report, ensure_ascii=False, allow_nan=False)
check_deadline()
(stage / 'graded-cooling-live-observation.json').write_text(report_text, encoding='utf-8')
check_deadline()
values = [row['ambient_c'] for row in records if row['ambient_fresh']]
summary = json.dumps({'verified': True, 'controller': 14, 'deadline': args.deadline, 'pid': int(pid),
                      'cold_evidence': cold_evidence, 'observed_seconds': observed_seconds,
                      'status_samples': len(records), 'fresh_ambient_samples': len(values),
                      'ambient_min': min(values), 'ambient_max': max(values),
                      'history_samples': len(rows), 'history_with_fresh_ambient': len(fresh_rows),
                      'interval_min': min(intervals), 'interval_max': max(intervals),
                      'last_gear': records[-1]['gear'], 'last_rpm': records[-1]['rpm'],
                      'motor_window_verified': True, 'last_confirmed': records[-1]['confirmed'],
                      'artifact': str(stage / 'graded-cooling-live-observation.json')}, ensure_ascii=False)
check_deadline()
evidence.accept()
check_deadline()
evidence.finish()
print(summary, flush=True)
