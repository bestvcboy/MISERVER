import json
import math
from copy import deepcopy
from dataclasses import dataclass, field
from statistics import median


POLICY_VERSION = 'closed-loop-r1'
OBSERVE_SECONDS = 180
PREPARE_SECONDS = 60
DRIFT_SECONDS = 180
DRIFT_RATE = .01
WARMING_BUDGET_C = 2
HIGH_EXIT_TAG = 'high_exit_v1'
HIGH_EXIT_MARGIN_C = 5
HIGH_EXIT_RESTORE_MARGIN_C = 4
HIGH_EXIT_MEASURED_C = 71
HIGH_EXIT_REFERENCE_RISE_C = 1
HIGH_EXIT_RESPONSE_SECONDS = 30
MAX_SOURCES = 2048
MAX_FAILURES = 256
MAX_BYTES = 2_000_000
SOURCE_FIELD_LIMITS = (128, 80, 80, 256, 32)
MAX_SOURCE_JSON_BYTES = 256
SOURCE_CLOCK_BUDGET = .5
SOURCE_TTL = 6


@dataclass(frozen=True)
class SourceTiming:
    observed_mono: float

    @property
    def expires_mono(self):
        return self.observed_mono + SOURCE_TTL

    def valid_at(self, now):
        return (all(type(v) in (int, float) and math.isfinite(v) for v in (now, self.observed_mono))
            and self.observed_mono <= now < self.expires_mono)


def source_timing(node, now):
    requested, sampled, source = (node.get(k) for k in ('requested_mono', 'sampled_at', 'source_ts'))
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in (requested, sampled, source, now))
            or not 0 <= now - requested < SOURCE_TTL or sampled - source < -SOURCE_CLOCK_BUDGET):
        return None
    timing = SourceTiming(requested - max(0, sampled - source + SOURCE_CLOCK_BUDGET))
    return timing if timing.valid_at(now) else None


def policy_spec():
    return dict(version='enhanced80-r4', memory_policy_version=POLICY_VERSION,
        enhanced_admission_c=80, minimum_level=0, ordinary_ceiling=10,
        maximum_level=16, targets_c=dict(cpu=60, gpu=60, board=65), critical_c=75,
        descent_step=1, ascent_step=2, observe_seconds=OBSERVE_SECONDS,
        preparation_seconds=PREPARE_SECONDS, execution_seconds=60,
        decision_seconds=3, drift_horizon_seconds=DRIFT_SECONDS, ceiling_requalification_seconds=60,
        source_timing=dict(clock_budget_seconds=SOURCE_CLOCK_BUDGET, ttl_seconds=SOURCE_TTL,
            anchor='original_request_start', expiry='strict',
            assurance='provisional_engineering_bound_not_clock_synchronization_proof'),
        drift_rate_c_per_second=DRIFT_RATE, warming_budget_c=WARMING_BUDGET_C,
        idle_margin_c=5, loaded_margin_c=8, plateau_seconds=180,
        high_exit=dict(tag=HIGH_EXIT_TAG, margin_c=HIGH_EXIT_MARGIN_C,
            restore_margin_c=HIGH_EXIT_RESTORE_MARGIN_C, measured_restore_c=HIGH_EXIT_MEASURED_C,
            reference_rise_c=HIGH_EXIT_REFERENCE_RISE_C, response_seconds=HIGH_EXIT_RESPONSE_SECONDS,
            trend_seconds=60, observation_seconds=OBSERVE_SECONDS, drift_rate_c_per_second=DRIFT_RATE,
            warming_budget_c=WARMING_BUDGET_C),
        parameters='provisional_engineering_values_not_hardware_calibration',
        ambient_quality='uncalibrated_near_purifier',
        memory_limits=dict(sources=MAX_SOURCES, failures=MAX_FAILURES, bytes=MAX_BYTES))


def robust_slope(points):
    pairs = [(b[1] - a[1]) / (b[0] - a[0]) for index, a in enumerate(points)
             for b in points[index + 1:] if b[0] - a[0] >= 15]
    return median(pairs) if pairs else math.inf


def source_key(node, key):
    return (node['host'], node['hostname'], node['power_source'], key[1], key[2])


def valid_source(value):
    return (isinstance(value, (tuple, list)) and len(value) == 5
        and all(type(v) is str and v.strip() and v.isprintable() and len(v) <= limit
            for v, limit in zip(value, SOURCE_FIELD_LIMITS))
        and len(json.dumps(value, ensure_ascii=True).encode('utf-8')) <= MAX_SOURCE_JSON_BYTES)


def host_baselines(nodes):
    return {node['host']: [node['cpu_load'], node['gpu_load'], node['power_w']]
            for node in nodes if node.get('online') and all(type(node.get(k)) in (int, float)
                and math.isfinite(node[k]) for k in ('cpu_load', 'gpu_load', 'power_w'))}


def increased(baseline, current):
    return (current is None or any(current[i] >= baseline[i] + 15 for i in (0, 1))
            or current[2] >= baseline[2] * 1.2 and current[2] > baseline[2])


@dataclass
class WorkloadCycle:
    phase: str = 'idle'
    start_level: int | None = None
    context: dict | None = None
    credit: float = 0
    idle_credit: float = 0
    qualified: bool = False
    idle_qualified: bool = False


@dataclass
class ThermalMemory:
    anchors: dict = field(default_factory=dict)
    failures: dict = field(default_factory=dict)
    open_edge: dict | None = None
    intent: dict | None = None
    revision: int = 0
    saturated: bool = False
    ceiling_dangers: dict = field(default_factory=dict)

    def add_anchor(self, key, anchor, peak):
        if key not in self.anchors and len(self.anchors) >= MAX_SOURCES:
            self.saturated = True
            return
        self.anchors[key] = dict(anchor_c=anchor, peak_c=max(anchor, peak))

    def add_failure(self, key, failure):
        slot = tuple(key) + (failure['failed_level'],)
        if slot not in self.failures and len(self.failures) >= MAX_FAILURES:
            self.saturated = True
            return
        previous = self.failures.get(slot)
        same_slot_thermal_memory_conflict = (previous is not None and previous['failure_class'] == 'unresolved_descent'
            and failure.get('failure_class', 'thermal_failure') == 'thermal_failure')
        if same_slot_thermal_memory_conflict:
            self.saturated = True
        if slot not in self.failures:
            self.failures[slot] = dict(failure, failure_class=failure.get('failure_class', 'thermal_failure'))

    def remember_ceiling_danger(self, key, *, safe_level, failed_level, baseline):
        slot = tuple(key) + (failed_level,)
        failure = self.failures.get(slot)
        if (failure is None or failure['failure_class'] != 'thermal_failure'
                or not 11 <= safe_level <= 16 or failed_level != safe_level - 1
                or slot not in self.ceiling_dangers and len(self.ceiling_dangers) >= MAX_FAILURES):
            self.saturated = True
            return
        self.ceiling_dangers[slot] = dict(safe_level=safe_level, baseline=list(baseline))

    def export(self):
        payload = dict(schema=1, policy_version=POLICY_VERSION, revision=self.revision,
            saturated=self.saturated,
            anchors=[dict(source=list(key), **value) for key, value in sorted(self.anchors.items())],
            failures=[dict(source=list(key[:5]), **{k: value[k] for k in (
                'safe_level', 'failed_level', 'baseline', 'automatic', 'failure_class')})
                for key, value in sorted(self.failures.items())], open_edge=self.open_edge, intent=self.intent)
        if self.ceiling_dangers:
            payload['ceiling_dangers'] = [dict(source=list(key[:5]), safe_level=value['safe_level'],
                failed_level=key[5], baseline=value['baseline']) for key, value in sorted(self.ceiling_dangers.items())]
        return deepcopy(payload)

    @classmethod
    def restore(cls, payload):
        def fields(value, keys):
            if type(value) is not dict or set(value) != set(keys):
                raise ValueError('invalid thermal memory fields')

        def real(value, low, high):
            if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
                raise ValueError('invalid thermal memory number')

        def integer(value, low, high):
            if type(value) is not int or not low <= value <= high:
                raise ValueError('invalid thermal memory integer')

        def source(value):
            if type(value) is not list or not valid_source(value):
                raise ValueError('invalid thermal memory source')
            return tuple(value)

        def baseline(value):
            if type(value) is not list or len(value) != 3:
                raise ValueError('invalid thermal memory workload')
            for val, upper in zip(value, (100, 100, 1000)):
                real(val, 0, upper)

        extended = type(payload) is dict and 'ceiling_dangers' in payload
        fields(payload, ('schema', 'policy_version', 'revision', 'saturated', 'anchors', 'failures', 'open_edge', 'intent')
            + (('ceiling_dangers',) if extended else ()))
        if type(payload['schema']) is not int or payload['schema'] != 1 or payload['policy_version'] != POLICY_VERSION:
            raise ValueError('unsupported thermal memory version')
        integer(payload['revision'], 0, 2**53 - 1)
        if type(payload['saturated']) is not bool:
            raise ValueError('invalid thermal memory latch')
        for name, limit in (('anchors', MAX_SOURCES), ('failures', MAX_FAILURES)) + (
                (('ceiling_dangers', MAX_FAILURES),) if extended else ()):
            if type(payload[name]) is not list or len(payload[name]) > limit:
                raise ValueError('thermal memory capacity exceeded')
        memory = cls(revision=payload['revision'], saturated=payload['saturated'])
        intent = payload['intent']
        if intent is not None:
            fields(intent, ('from_level', 'to_level', 'purpose', 'state'))
            integer(intent['from_level'], 0, 16)
            integer(intent['to_level'], 0, 16)
            if intent['purpose'] not in ('release', 'reserve', 'rollback', 'protect', 'probe', 'manual', 'ceiling_exit') or intent['state'] not in (
                    'pending', 'confirmed', 'invalidated', 'failed', 'execution_timeout'):
                raise ValueError('invalid thermal memory intent')
            if intent['purpose'] == 'ceiling_exit' and not (
                    11 <= intent['from_level'] <= 16 and intent['to_level'] == intent['from_level'] - 1):
                raise ValueError('invalid ceiling exit intent')
            memory.intent = deepcopy(intent)
        for row in payload['anchors']:
            fields(row, ('source', 'anchor_c', 'peak_c'))
            key = source(row['source'])
            real(row['anchor_c'], 0, 150)
            real(row['peak_c'], row['anchor_c'], 150)
            if key in memory.anchors:
                raise ValueError('duplicate thermal anchor')
            memory.add_anchor(key, row['anchor_c'], row['peak_c'])
        for row in payload['failures']:
            fields(row, ('source', 'safe_level', 'failed_level', 'baseline', 'automatic', 'failure_class'))
            key = source(row['source'])
            integer(row['safe_level'], 1, 16)
            integer(row['failed_level'], 0, row['safe_level'] - 1)
            baseline(row['baseline'])
            slot = key + (row['failed_level'],)
            if (type(row['automatic']) is not bool or slot in memory.failures
                    or row['failure_class'] not in ('thermal_failure', 'unresolved_descent')):
                raise ValueError('invalid or duplicate thermal failure')
            memory.failures[slot] = {k: deepcopy(v) for k, v in row.items() if k != 'source'}
        for row in payload.get('ceiling_dangers', ()):
            fields(row, ('source', 'safe_level', 'failed_level', 'baseline'))
            key = source(row['source'])
            integer(row['safe_level'], 11, 16)
            integer(row['failed_level'], row['safe_level'] - 1, row['safe_level'] - 1)
            baseline(row['baseline'])
            slot = key + (row['failed_level'],)
            if (slot in memory.ceiling_dangers or slot not in memory.failures
                    or memory.failures[slot]['failure_class'] != 'thermal_failure'):
                raise ValueError('invalid or duplicate ceiling danger')
            memory.remember_ceiling_danger(key, safe_level=row['safe_level'],
                failed_level=row['failed_level'], baseline=row['baseline'])
        edge = payload['open_edge']
        if edge is not None:
            tagged = type(edge) is dict and 'release_basis' in edge
            fields(edge, ('from_level', 'to_level', 'automatic', 'sources', 'stage')
                + (('release_basis',) if tagged else ()))
            if tagged and edge['release_basis'] != HIGH_EXIT_TAG:
                raise ValueError('unsupported thermal edge basis')
            integer(edge['from_level'], 1, 16)
            integer(edge['to_level'], 0, edge['from_level'] - 1)
            if tagged and (edge['from_level'] <= 10 or edge['to_level'] != edge['from_level'] - 1
                    or edge['automatic'] is not True):
                raise ValueError('invalid high exit edge')
            if type(edge['automatic']) is not bool or edge['stage'] not in ('pending', 'confirmed', 'unresolved'):
                raise ValueError('invalid thermal edge')
            if type(edge['sources']) is not list or not 1 <= len(edge['sources']) <= MAX_SOURCES:
                raise ValueError('invalid thermal edge sources')
            seen, hosts = set(), {}
            for row in edge['sources']:
                fields(row, ('source', 'baseline', 'temp_c', 'attributable')
                    + (('exit_origin_c',) if tagged else ()))
                key = source(row['source'])
                baseline(row['baseline'])
                real(row['temp_c'], 0, 150)
                if tagged:
                    real(row['exit_origin_c'], 0, 150)
                if type(row['attributable']) is not bool:
                    raise ValueError('invalid thermal edge attribution')
                if key in seen:
                    raise ValueError('duplicate thermal edge source')
                seen.add(key)
                host_context = (key[1:3], row['baseline'])
                if key[0] in hosts and hosts[key[0]] != host_context:
                    raise ValueError('inconsistent thermal edge host context')
                hosts[key[0]] = host_context
            if tagged and (not seen.issubset(memory.anchors)
                    or intent is None or intent['purpose'] == 'release' and (
                        intent['from_level'], intent['to_level']) != (edge['from_level'], edge['to_level'])):
                raise ValueError('mismatched high exit sources or intent')
            memory.open_edge = deepcopy(edge)
        try:
            encoded = json.dumps(payload, allow_nan=False, ensure_ascii=True)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError('thermal memory must be strict JSON') from exc
        if len(encoded.encode('utf-8')) > MAX_BYTES:
            raise ValueError('thermal memory exceeds byte limit')
        return memory
