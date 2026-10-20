import json
import math
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from statistics import median

from thermal_policy import (DRIFT_RATE, DRIFT_SECONDS, MAX_SOURCES, OBSERVE_SECONDS, PREPARE_SECONDS,
    HIGH_EXIT_TAG, HIGH_EXIT_MARGIN_C, HIGH_EXIT_RESTORE_MARGIN_C, HIGH_EXIT_MEASURED_C,
    HIGH_EXIT_REFERENCE_RISE_C, HIGH_EXIT_RESPONSE_SECONDS,
    WARMING_BUDGET_C, ThermalMemory, WorkloadCycle, host_baselines, increased,
    SOURCE_TTL, policy_spec, robust_slope, source_key, source_timing, valid_source)

GROUPS = ("acpi", "cpu", "nvme", "nic", "mlx", "wifi", "spbm", "other")
LIMITS = {group: (target, policy_spec()['critical_c']) for group, target in policy_spec()['targets_c'].items()}
MAX_LEVEL, MIN_LEVEL = policy_spec()['maximum_level'], policy_spec()['ordinary_ceiling']
HIGH_WIND_TTL = SOURCE_TTL
POWER_HOSTS = ("192.168.3.180", "192.168.3.181")
POWER_SOURCE = "spbm"
CONTROLLER_VERSION = 14


def _source_observed(nodes, now):
    timings = [source_timing(node, now) for node in nodes]
    if not timings or any(timing is None for timing in timings):
        return None
    return min(timing.observed_mono for timing in timings)


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("数值无效")
    return value


def normalize(host, data, requested_mono, sampled_at):
    if not isinstance(data, dict) or data.get("ok") is False:
        raise ValueError("节点接口异常")
    if data.get("external_ip", host) != host:
        raise ValueError("节点身份不匹配")
    temperatures = data.get("temperatures")
    if not isinstance(temperatures, dict):
        raise ValueError("缺少温度数据")
    sensors, seen_pairs = [], set()
    sources = {}
    hwmon = data.get("hwmon_sensors", [])
    if not isinstance(hwmon, list) or len(hwmon) > 512:
        raise ValueError("硬件传感器列表无效")
    for item in hwmon:
        if isinstance(item, dict) and item.get("kind") == "temp" and item.get("device"):
            label = item.get("label") or f"temp{item.get('index')}"
            sources.setdefault((item.get("chip"), label), []).append(item)
    for group in GROUPS:
        entries = temperatures.get(group, [])
        if not isinstance(entries, list) or len(entries) > 128:
            raise ValueError("传感器列表无效")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("传感器格式无效")
            value = entry.get("temp")
            if value is None:
                continue
            temp = number(value, 0, 150)
            label = str(entry.get("label") or entry.get("type") or group)[:80]
            matches = sources.get((entry.get("chip"), label))
            pair = (group, entry.get("chip"), label)
            if "zone" not in entry and matches:
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                for source in matches:
                    if source.get("value") is None:
                        continue
                    sensors.append({"id": f"{group}:{source['device']}:{source.get('index')}:{label}",
                                    "group": group, "label": label,
                                    "temp": number(source["value"], 0, 150), "calibrated": group != "spbm"})
                continue
            seen_pairs.add(pair)
            identity = str(entry.get("zone")) if "zone" in entry else (
                f"{entry.get('chip', group)}:{label}")
            sensors.append({"id": f"{group}:{identity}:{label}", "group": group,
                            "label": label, "temp": temp, "calibrated": group != "spbm"})
    cpu_values = [s["temp"] for s in sensors if s["group"] in ("cpu", "acpi")]
    board_values = [s["temp"] for s in sensors if s["calibrated"]]
    gpu = temperatures.get("gpu", data.get("gpu_temperature"))
    if gpu is not None:
        gpu = number(gpu, 0, 150)
        sensors.append({"id": "gpu:nvidia-smi", "group": "gpu", "label": "NVIDIA GPU",
                        "temp": gpu, "calibrated": True})
    def optional(key, ceiling):
        value = data.get(key)
        return None if value is None else number(value, 0, ceiling)
    throttles = data.get("gpu_throttle") or []
    if not isinstance(throttles, list):
        raise ValueError("GPU 节流数据无效")
    thermal_throttle = any("thermal" in str(reason).lower() for item in throttles
                           if isinstance(item, dict) for reason in (item.get("active_reasons") or []))
    credible_ids = [s["id"] for s in sensors if s["calibrated"]]
    hostname = data.get("hostname")
    return {"host": host, "hostname": str(data.get("hostname", host))[:80],
            "hostname_verified": (isinstance(hostname, str) and len(hostname) <= 80
                                  and bool(hostname.strip()) and hostname.isprintable()),
            "online": True, "sampled_at": sampled_at, "requested_mono": requested_mono,
            "source_ts": optional("timestamp", 1e12), "sensors": sensors,
            "cpu_temp": max(cpu_values, default=None),
            "cpu_proxy": not any(s["group"] == "cpu" for s in sensors),
            "gpu_temp": gpu, "board_temp": max(board_values, default=None),
            "cpu_load": optional("cpu_percent", 100), "gpu_load": optional("gpu_percent", 100),
            "power_w": optional("system_power_w", 1000),
            "power_source": str(data.get("system_power_source") or "不可用")[:80],
            "thermal_throttle": thermal_throttle,
            "ambiguous_sensors": len(set(credible_ids)) != len(credible_ids)}



class _PolicyField:
    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        return self if instance is None else instance._policy[self.name]

    def __set__(self, instance, value):
        instance._policy[self.name] = value


@dataclass(frozen=True)
class _Episode:
    original_from: int
    original_to: int
    original_requested_at: float
    original_deadline: float
    planned_step: int = 1


@dataclass
class _CeilingWindow:
    endpoint: int
    identity: tuple
    context: dict
    context_kind: str
    failures: tuple
    points: dict
    source_times: dict
    feedback_at: float
    at: float
    credit: float = 0


@dataclass(frozen=True)
class MeasuredCritical:
    hard_c: float = 75


@dataclass(frozen=True)
class Measured80:
    source: tuple
    generation: int


class SourceFault(Enum):
    MISSING = 'required_source_missing'
    EXPIRED = 'required_source_expired'
    REJECTED = 'required_source_rejected'
    IDENTITY = 'identity_ambiguous'
    TIME = 'source_time_invalid'


@dataclass(frozen=True)
class SourceFaultProtection:
    source: tuple
    generation: int
    fault: SourceFault
    demand: int


@dataclass(frozen=True)
class FailedDescentRestoration:
    from_level: int
    destination: int
    original_deadline: float
    source: tuple
    edge: tuple
    diagnosed_mono: float
    generation: int
    failure_class: str = 'thermal_failure'


@dataclass(frozen=True)
class ThermalView:
    generation: int = 0
    inventory: tuple = ()
    observations: tuple = ()
    faults: tuple = ()
    complete: bool = False
    maximum_c: float | None = None
    expires_mono: float | None = None


@dataclass(frozen=True)
class TenLevelNonRecovery:
    working_c: float
    window_seconds: float
    episode_deadline: float
    from_level: int
    ceiling: int


@dataclass(frozen=True)
class NoAscent:
    from_level: int
    ceiling: int
    destination: int | None = None
    original_deadline: float | None = None
    generation: int | None = None
    operation: _Episode | None = None


@dataclass(frozen=True)
class SafeEndpointRestoration:
    from_level: int
    destination: int
    original_deadline: float


def _finite(*values):
    return all(type(value) in (int, float) and math.isfinite(value) for value in values)


@dataclass(frozen=True)
class HighWindAuthority:
    allowed: bool = False
    witness: tuple | None = None
    observed_mono: float | None = None
    actual_c: float | None = None
    proof: Measured80 | SourceFaultProtection | FailedDescentRestoration | MeasuredCritical | TenLevelNonRecovery | NoAscent | SafeEndpointRestoration = MeasuredCritical()

    def valid_at(self, now):
        common = (self.allowed is True and isinstance(self.witness, tuple) and len(self.witness) == 3
                and all(isinstance(part, str) and part.strip() for part in self.witness)
                and _finite(now, self.observed_mono)
                and 0 <= now - self.observed_mono < HIGH_WIND_TTL)
        if not common:
            return False
        proof = self.proof
        if isinstance(proof, SourceFaultProtection):
            return (valid_source(proof.source) and type(proof.generation) is int
                and proof.generation > 0 and isinstance(proof.fault, SourceFault)
                and type(proof.demand) is int and 10 <= proof.demand <= 16 and self.actual_c is None)
        if not _finite(self.actual_c) or not 0 <= self.actual_c <= 150:
            return False
        if isinstance(proof, Measured80):
            return valid_source(proof.source) and type(proof.generation) is int and proof.generation > 0 and self.actual_c >= 80
        if isinstance(proof, MeasuredCritical):
            hard = proof.hard_c
            return _finite(hard) and hard == 75 and self.actual_c >= 80
        if isinstance(proof, TenLevelNonRecovery):
            return (_finite(proof.working_c, proof.window_seconds, proof.episode_deadline)
                and self.actual_c >= 80 and proof.working_c in (60, 65) and proof.window_seconds >= 180
                and 0 < proof.episode_deadline - now <= 600
                and type(proof.from_level) is int and type(proof.ceiling) is int
                and 10 <= proof.from_level <= proof.ceiling <= min(16, proof.from_level + 1))
        if isinstance(proof, NoAscent):
            return (type(proof.from_level) is int and type(proof.ceiling) is int
                and 10 < proof.from_level <= 16 and 10 <= proof.ceiling <= proof.from_level
                and (proof.generation is None or self.actual_c < 80 and type(proof.generation) is int
                    and isinstance(proof.operation, _Episode)
                    and (proof.operation.original_from, proof.operation.original_to, proof.operation.original_deadline)
                        == (proof.from_level, proof.destination, proof.original_deadline))
                and (proof.destination is None and proof.generation is None or type(proof.destination) is int
                    and proof.destination == proof.from_level - 1 and proof.destination <= proof.ceiling
                    and _finite(proof.original_deadline) and 0 < proof.original_deadline - now <= 60))
        if isinstance(proof, FailedDescentRestoration):
            return (valid_source(proof.source) and proof.failure_class == 'thermal_failure'
                and type(proof.generation) is int and proof.generation > 0
                and proof.edge == (proof.destination, proof.from_level)
                and type(proof.from_level) is int and type(proof.destination) is int
                and 0 <= proof.from_level < proof.destination <= 16
                and _finite(proof.original_deadline, proof.diagnosed_mono)
                and 0 <= now - proof.diagnosed_mono < 6 and 0 < proof.original_deadline - now <= 60)
        if isinstance(proof, SafeEndpointRestoration):
            return (self.actual_c >= 80 and type(proof.from_level) is int and type(proof.destination) is int
                and 0 <= proof.from_level < proof.destination <= 16
                and _finite(proof.original_deadline) and 0 < proof.original_deadline - now <= 60)
        return False

    def ceiling_at(self, now):
        if not self.valid_at(now):
            return 10
        if isinstance(self.proof, (TenLevelNonRecovery, NoAscent)):
            return self.proof.ceiling
        if isinstance(self.proof, (SafeEndpointRestoration, FailedDescentRestoration)):
            return self.proof.destination
        if isinstance(self.proof, SourceFaultProtection):
            return self.proof.demand
        return MAX_LEVEL

    def normal_ceiling_at(self, now):
        return (self.ceiling_at(now) if self.valid_at(now)
            and isinstance(self.proof, (Measured80, MeasuredCritical, TenLevelNonRecovery)) else 10)

    @property
    def expires_mono(self):
        if not self.allowed or not _finite(self.observed_mono):
            return None
        deadline = (self.proof.episode_deadline if isinstance(self.proof, TenLevelNonRecovery)
            else self.proof.original_deadline if isinstance(self.proof, (NoAscent, SafeEndpointRestoration, FailedDescentRestoration)) else None)
        return min(self.observed_mono + HIGH_WIND_TTL, deadline) if _finite(deadline) else self.observed_mono + HIGH_WIND_TTL

    def permits(self, now, actual, destination):
        if type(actual) is not int or type(destination) is not int or not 0 <= actual <= 16 or not 0 <= destination <= 16:
            return False
        if actual == destination:
            return True
        if not self.valid_at(now):
            return max(actual, destination) <= 10
        proof = self.proof
        if isinstance(proof, NoAscent):
            return (actual == proof.from_level and destination == proof.destination
                and destination <= min(actual, proof.ceiling))
        if isinstance(proof, (SafeEndpointRestoration, FailedDescentRestoration)):
            return actual == proof.from_level and destination == proof.destination
        if isinstance(proof, TenLevelNonRecovery):
            return (actual == proof.from_level and actual < destination <= min(proof.ceiling, actual + 1))
        return actual < destination <= self.ceiling_at(now)

    def permits_activation(self, now, actual, destination):
        return (max(actual, destination) <= 10 or self.valid_at(now)
            and isinstance(self.proof, (Measured80, MeasuredCritical, SourceFaultProtection))
            and max(actual, destination) <= self.ceiling_at(now))


@dataclass
class _EmpiricalLedger:
    identity: tuple | None = None
    context: dict | None = None
    workload_class: tuple | None = None
    level: int | None = None
    points: dict = field(default_factory=dict)
    credit: float = 0
    last: float | None = None
    deadline: float | None = None
    ceiling: int = 10
    witness: tuple | None = None
    window_seconds: float = 0
    recovery_credit: float = 0
    recovery_ready: bool = False
    phase: str = "normal"
    detail: str = "尚无实际10档容量证据"

    def clear_window(self):
        self.points.clear()
        self.credit = self.recovery_credit = 0
        self.recovery_ready = False
        self.last = None
        self.level = None

    def retire(self, phase, detail):
        self.deadline = None
        self.ceiling = 10
        self.witness = None
        self.clear_window()
        self.phase, self.detail = phase, detail


@dataclass(frozen=True)
class MotionAuthority:
    observed_mono: float | None = None
    source: tuple | None = None
    actual_c: float | None = None
    critical_c: float | None = None
    restore_level: int | None = None
    restore_from: int | None = None
    deadline: float | None = None


@dataclass
class OperatingState:
    phase: str = "hold"
    confirmed_level: int | None = None
    identity: tuple | None = None
    observe_credit: float = 0
    last_evidence_at: float | None = None
    last_feedback: float = -math.inf
    evidence_qualified: bool = False
    settle_required: float = 0
    last_increase: float = -math.inf
    workload_epoch: tuple | None = None
    idle_credit: float = 0
    failed_floor: dict | None = None
    failure_credit: float = 0
    failure_qualification: str | None = None
    last_transition: dict = field(default_factory=dict)
    release_basis: str = 'ordinary'


@dataclass
class _HighExitWindow:
    level: int
    sources: tuple
    context: dict
    since: float
    last: float
    peaks: dict = field(default_factory=dict)
    origins: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _HighExitQualification:
    reason: str
    source: tuple | None = None
    margin: float | None = None
    measured_margin: float | None = None
    trend: float | None = None
    warming: float | None = None
    complete: bool = False

    @property
    def qualified(self):
        return self.reason == 'high_exit_observation'


@dataclass
class _FixedEpoch:
    identity: tuple
    context: dict
    since: float
    last: float
    response_since: float


@dataclass(frozen=True)
class _ReleaseRisk:
    kind: str
    margin_c: float
    reason: str = "bounded"


@dataclass(frozen=True)
class _CycleProof:
    binding: tuple
    last: tuple
    returns: tuple = ()
    opening: tuple | None = None
    issued_at: float | None = None
    ceiling: float | None = None

    @property
    def floor_rate(self):
        recent = [r for r in self.returns if r[1] >= self.last[0] - DRIFT_SECONDS]
        return max(robust_slope([(r[0], r[2]) for r in recent]),
            robust_slope([(r[1], r[4]) for r in recent]))

    @property
    def growing(self):
        large = [r for r in self.returns if r[3] - r[2] >= 2][-6:]
        if len(large) < 3:
            return False
        def increases(a, b):
            floor = max(0, b[2] - a[2], b[4] - a[4])
            return round(b[3] - a[3] - floor, 12) > 0
        alternating = len(large) == 6 and all(increases(a, b) for a, b in zip(large, large[2:]))
        large = large[-3:]
        pairs = [(a, b) for i, a in enumerate(large) for b in large[i + 1:]]
        peak_rate = median((b[3] - a[3]) / (b[1] - a[1]) for a, b in pairs)
        floor_rate = max(median((b[2] - a[2]) / (b[0] - a[0]) for a, b in pairs),
            median((b[4] - a[4]) / (b[1] - a[1]) for a, b in pairs))
        return alternating or round(peak_rate - max(0, floor_rate), 12) > 0


class Controller:
    level = _PolicyField()
    phase = _PolicyField()
    trial = _PolicyField()
    baseline = _PolicyField()
    mode = _PolicyField()
    last_feedback = _PolicyField()
    last_drop = _PolicyField()
    cool_since = _PolicyField()
    recovery_since = _PolicyField()
    search_anchor = _PolicyField()
    search_context = _PolicyField()
    search_reference = _PolicyField()
    search_since = _PolicyField()
    gear_window = _PolicyField()

    def __init__(self, hosts, *, rules_only=False, high_wind_gate=False):
        self._rules_only = bool(rules_only)
        self.high_wind_gate = high_wind_gate
        self.high_wind = HighWindAuthority()
        self.thermal_view = ThermalView()
        self._admission_inventory = {}
        self._admission_sources = {}
        self._actuation_endpoint = None
        self.motion = MotionAuthority()
        self._empirical = _EmpiricalLedger()
        self.operating = OperatingState()
        self._fixed_epochs = {}
        self._cycle_proofs = {}
        self._thermal_memory = ThermalMemory()
        self._anchor_clock = {}
        self._plateau_since = {}
        self._failure_credits = {}
        self._workload_cycle = WorkloadCycle()
        self._restored_edge = False
        self._memory_restored = False
        self._control_status = None
        self._high_exit_window = None
        self._ceiling_window = None
        self._ceiling_recovered = set()
        self._storage_safe_at = None
        self.hosts = set(hosts)
        self._policy = dict(level=MAX_LEVEL, phase="protect", trial=None, baseline=None,
                            mode=None, operation=None, last_feedback=-math.inf, last_drop=-math.inf,
                            cool_since=None, recovery_since=None, search_anchor=None,
                            search_context=None, search_reference=None, search_since=None,
                            gear_window=None, state=dict(tag="protect"), startup_required=True,
                            protect_cause="startup", thermal_witnesses=set(), preparation=False,
                            cold_band=False, last_downward=None, rebound=None,
                            learning_window=deque(maxlen=512), learning_key=None, node_transport={}, fan_transport_safe=None)
        self.history = {}
        self.positive_since = {}
        self.expected = {}
        self.sources = {}
        self.last_evaluation = None
        self.load_history = {}
        self.workload_history = {}
        self.workload_context = None
        self.power_exposure = {}

    def export_thermal_memory(self):
        return self._thermal_memory.export()

    def restore_thermal_memory(self, payload, now):
        try:
            valid_now = _finite(now) and now >= 0
        except OverflowError:
            valid_now = False
        if not valid_now or self.last_evaluation is not None or self._memory_restored:
            raise ValueError('thermal memory restore is startup-only')
        memory = ThermalMemory.restore(payload)
        if any(key[0] not in self.hosts for key in (*memory.anchors, *memory.failures)):
            raise ValueError('thermal memory host mismatch')
        if memory.open_edge and any(row['source'][0] not in self.hosts for row in memory.open_edge['sources']):
            raise ValueError('thermal memory edge host mismatch')
        for key, failure in memory.failures.items():
            failure.update(load=max(failure['baseline'][:2]), power=failure['baseline'][2],
                failed_at=now, identity=None, witness=dict(source=(key[0], key[3], key[4])))
        self._thermal_memory = memory
        self._memory_restored = True
        self._restored_edge = memory.open_edge is not None
        self.operating = OperatingState()
        self.high_wind = HighWindAuthority()
        self.motion = MotionAuthority()
        self._empirical = _EmpiricalLedger()
        self._failure_credits.clear()
        self._anchor_clock.clear()
        self._plateau_since.clear()
        self._workload_cycle = WorkloadCycle()
        self._high_exit_window = None
        self._ceiling_window = None
        self._ceiling_recovered.clear()
        if memory.open_edge and memory.open_edge.get('release_basis') == HIGH_EXIT_TAG:
            self._quarantine_high_exit(now)

    @staticmethod
    def policy_spec():
        return policy_spec()

    def _source_memory_key(self, node, key):
        value = source_key(node, key)
        if not valid_source(value):
            self._thermal_memory.saturated = True
            return None
        return value

    def _remember_descent(self, operation, frame):
        memory = self._thermal_memory
        nodes = {n['host']: n for n in frame['nodes']}
        baselines = host_baselines(frame['nodes'])
        sources = []
        for key, raw in sorted(frame['evidence'].items()):
            identity = self._source_memory_key(nodes[key[0]], key)
            if identity is not None:
                sources.append(dict(source=list(identity), baseline=baselines[key[0]], temp_c=raw['temp'], attributable=True))
        if not sources or len(sources) > MAX_SOURCES:
            memory.saturated = True
            return
        memory.open_edge = dict(from_level=operation['from'], to_level=operation['destination'],
            automatic=operation['automatic'], sources=sources, stage='pending')
        if operation.get('release_basis') == HIGH_EXIT_TAG:
            memory.open_edge['release_basis'] = HIGH_EXIT_TAG
            for row in sources:
                row['exit_origin_c'] = self._high_exit_window.origins[tuple(row['source'])]

    def _remember_intent(self, operation, *, new=False, state=None):
        if not (self.rules_only and self.high_wind_gate):
            return
        memory = self._thermal_memory
        if new:
            if memory.revision >= 2**53 - 1:
                memory.saturated = True
            else:
                memory.revision += 1
        memory.intent = dict(from_level=operation['from'], to_level=operation['destination'],
            purpose=operation['purpose'], state=state or operation['status'])

    def _reconcile_restored_edge(self, frame, actual, identity, context, ready):
        if not self._restored_edge or not ready:
            return
        saved = self._thermal_memory.open_edge
        if saved.get('release_basis') == HIGH_EXIT_TAG:
            self._quarantine_high_exit(frame['now'], identity)
            self._restored_edge = False
            return
        nodes = {n['host']: n for n in frame['nodes']}
        if any(row['source'][0] not in nodes or tuple(row['source']) != source_key(
                nodes[row['source'][0]], (row['source'][0], row['source'][3], row['source'][4]))
                or (row['source'][0], row['source'][3], row['source'][4]) not in frame['evidence']
                for row in saved['sources']):
            return
        if actual != saved['to_level']:
            for row in saved['sources']:
                key, baseline = tuple(row['source']), list(row['baseline'])
                self._thermal_memory.add_failure(key, dict(safe_level=saved['from_level'],
                    failed_level=saved['to_level'], baseline=baseline, load=max(baseline[:2]),
                    power=baseline[2], automatic=saved['automatic'], failed_at=frame['now'],
                    identity=identity, witness=dict(source=(key[0], key[3], key[4])),
                    failure_class='unresolved_descent'))
            self._thermal_memory.open_edge = None
        elif actual == saved['to_level']:
            self._policy['last_downward'] = dict(edge=(saved['from_level'], actual),
                reference={(row['source'][0], row['source'][3]): row['temp_c'] for row in saved['sources']},
                identity=identity, context=context,
                baselines={row['source'][0]: row['baseline'] for row in saved['sources']},
                confounded_hosts={row['source'][0] for row in saved['sources'] if not row['attributable']},
                automatic=saved['automatic'])
            saved['stage'] = 'confirmed'
        self._restored_edge = False
        self.operating.observe_credit = 0
        self.operating.evidence_qualified = False

    def _workload_policy(self, frame, actual, context, ready, advancing, continuous, elapsed, *, idle_ready):
        cycle = self._workload_cycle
        baselines = host_baselines(frame['nodes'])
        active = any(max(v[:2]) >= 30 or v[2] >= 80 for v in baselines.values())
        idle = bool(baselines) and all(max(v[:2]) <= 15 and v[2] < 80 for v in baselines.values())
        idle_comparable = self._extend_context(cycle.context, context, strict=False) is not None
        extended = self._extend_context(cycle.context, context, anchored_power=True) if context is not None else None
        if context is not None and (active and cycle.phase == 'idle' or cycle.phase != 'idle' and extended is None):
            if cycle.phase == 'idle' or cycle.phase == 'stable_loaded' and active:
                cycle.phase, cycle.start_level = 'preparing', actual
            cycle.context = context
            self.operating.observe_credit = 0
            self.operating.evidence_qualified = False
            cycle.credit = 0
            cycle.qualified = False
            if not idle_comparable:
                cycle.idle_credit = 0
                cycle.idle_qualified = False
            extended = context
        if cycle.phase != 'idle':
            if extended is not None:
                cycle.context = extended
            safe = ready and extended is not None and advancing and continuous
            safe_idle = idle and idle_ready and idle_comparable and advancing and continuous
            cycle.credit = cycle.credit + elapsed if safe and cycle.qualified else 0
            cycle.idle_credit = cycle.idle_credit + elapsed if safe_idle and cycle.idle_qualified else 0
            cycle.qualified = safe
            cycle.idle_qualified = safe_idle
            if cycle.idle_credit >= PREPARE_SECONDS:
                self._workload_cycle = cycle = WorkloadCycle()
                self.operating.observe_credit = 0
                self.operating.evidence_qualified = False
            elif cycle.phase == 'preparing' and cycle.credit >= PREPARE_SECONDS:
                cycle.phase = 'stable_loaded'
                self.operating.observe_credit = max(self.operating.observe_credit, cycle.credit - elapsed)
                self.operating.evidence_qualified = True
        self.operating.workload_epoch = ((frame['now'], cycle.start_level) if cycle.phase == 'preparing' else None)
        return cycle.phase

    def _failure_policy(self, frame, identity, identity_known, actual, ready, advancing, continuous, elapsed, warming,
                        *, ceiling=None, ceiling_diagnostic=None):
        memory, state = self._thermal_memory, self.operating
        if ceiling is not None:
            return self._ceiling_requalification(frame, identity, actual, ready, advancing, ceiling,
                diagnostic=ceiling_diagnostic)
        if not identity_known:
            self._failure_credits.clear()
            return False
        nodes = {n['host']: n for n in frame['nodes']}
        current = host_baselines(frame['nodes'])
        relevant, cleared = [], False
        for key, failure in list(memory.failures.items()):
            node = nodes.get(key[0])
            matches = (node is not None and source_key(node, (key[0], key[3], key[4])) == key[:5]
                and (key[0], key[3], key[4]) in frame['evidence'])
            if not matches:
                self._failure_credits.pop(key, None)
                continue
            retained = failure['safe_level'] > 10
            if retained and (key not in memory.ceiling_dangers or key in self._ceiling_recovered):
                continue
            base = memory.ceiling_dangers.get(key, failure)['baseline']
            values = current.get(key[0])
            low_load = (values is not None and max(values[:2]) <= max(base[:2]) - 15
                and all(v <= b for v, b in zip(values[:2], base[:2])) and values[2] <= base[2] * .8)
            low_power = (values is not None and max(base[:2]) < 15 and base[2] > 0
                and all(v <= b for v, b in zip(values[:2], base[:2])) and values[2] <= base[2] * .8)
            automatic = failure.get('automatic', True)
            cool = (not retained and not automatic and failure.get('failure_class') != 'unresolved_descent'
                and frame['margin'] >= 8 and not warming and frame['now'] - failure['failed_at'] >= 300)
            clean = (ready and not warming and not self._pending()
                and (actual == failure['safe_level'] or retained and actual <= failure['safe_level']
                    or failure.get('failure_class') == 'unresolved_descent'
                    and actual < failure['safe_level'])
                and frame['margin'] >= 8 and not frame['thermal_risk'])
            qualification = 'lower_load' if low_load and ready and (not automatic or not warming) else (
                'lower_power' if low_power and clean and advancing and self._workload_cycle.phase != 'preparing' else 'cool' if cool else None)
            old_kind, earned = self._failure_credits.get(key, (None, 0))
            earned = (earned + (elapsed if advancing else 0) if qualification is not None and qualification == old_kind
                and ready and continuous else 0)
            self._failure_credits[key] = (qualification, earned)
            if qualification is not None and earned >= (180 if qualification == 'cool' else 60):
                if retained:
                    self._ceiling_recovered.add(key)
                else:
                    del memory.failures[key]
                self._failure_credits.pop(key, None)
                cleared = True
            else:
                relevant.append(failure)
        state.failed_floor = max(relevant, key=lambda f: f['failed_level'], default=None)
        selected = next((key for key, value in memory.failures.items() if value is state.failed_floor), None)
        state.failure_qualification, state.failure_credit = self._failure_credits.get(selected, (None, 0))
        return cleared

    def _ceiling_requalification(self, frame, identity, actual, ready, advancing, blockers, *, diagnostic=None):
        state, now = self.operating, frame['now']
        feedback, view = frame['feedback'], self.thermal_view
        dangers = self._thermal_memory.ceiling_dangers
        fingerprint = tuple((key, f['safe_level'], f['failed_level'], tuple(f['baseline']),
            f['automatic'], f['failure_class'], (dangers[key]['safe_level'], tuple(dangers[key]['baseline']))
                if key in dangers else None) for key, f in blockers)
        observations = {key: (temp, stamp) for key, temp, stamp in view.observations}
        context = self._context(frame['nodes'])
        current = host_baselines(frame['nodes'])
        complete_baselines = current.keys() == self.hosts
        context_kind = ('idle' if complete_baselines and all(max(v[:2]) <= 15 and v[2] < 80
            for v in current.values()) else 'strict')
        window = self._ceiling_window
        state.failed_floor = max((f for key, f in blockers), key=lambda f: f['failed_level'], default=None)
        state.failure_credit = 0

        def condition(kind, source=None):
            state.failure_qualification = 'ceiling_requalification_' + kind
            if diagnostic is not None:
                diagnostic.update(blocking_source=source,
                    context_kind=context_kind if complete_baselines and context is not None else None)
            return False

        eligible = (ready and context is not None and complete_baselines and view.complete and now < view.expires_mono
            and bool(observations) and not frame['telemetry_fault'] and not frame['wifi_gap']
            and not feedback.get('motor_fault') and feedback.get('motor_attempts', feedback.get('attempts', 0)) < 2)
        if not eligible:
            self._ceiling_window = None
            return condition('fresh_evidence')
        for key, raw in frame['evidence'].items():
            if not (raw['temp'] < 80 and raw['guard_projected'] < 80):
                self._ceiling_window = None
                return condition('temperature_window', source_key(
                    next(n for n in frame['nodes'] if n['host'] == key[0]), key))
        for key, failure in blockers:
            danger, values = dangers.get(key), current.get(key[0])
            if danger is None:
                continue
            base = danger['baseline']
            lower_load = (values is not None and max(values[:2]) <= max(base[:2]) - 15
                and all(v <= b for v, b in zip(values[:2], base[:2])) and values[2] <= base[2] * .8)
            lower_power = (values is not None and max(base[:2]) < 15 and base[2] > 0
                and all(v <= b for v, b in zip(values[:2], base[:2])) and values[2] <= base[2] * .8
                and frame['margin'] >= 8 and not frame['thermal_risk']
                and self._workload_cycle.phase != 'preparing')
            if not (lower_load or lower_power):
                self._ceiling_window = None
                return condition('material_recovery', key[:5])
        if any(key in dangers for key, failure in blockers):
            for key, raw in frame['evidence'].items():
                if raw['persistent_heat']:
                    self._ceiling_window = None
                    return condition('persistent_heat', source_key(
                        next(n for n in frame['nodes'] if n['host'] == key[0]), key))
        extended = (self._extend_context(window.context, context, strict=context_kind == 'strict',
            anchored_power=True) if window is not None else None)
        compatible = (window is not None and window.endpoint == actual and window.identity == identity
            and window.failures == fingerprint and window.source_times.keys() == observations.keys()
            and window.context_kind == context_kind and extended is not None)
        if window is not None and not compatible:
            self._ceiling_window = None
            return condition('context_changed')
        if self._pending():
            if window is None:
                return condition('fresh_evidence')
            state.failure_credit = window.credit
            condition('ready' if window.credit >= 60 else 'collecting')
            return window.credit >= 60
        if not compatible:
            self._ceiling_window = _CeilingWindow(actual, identity, context, context_kind, fingerprint,
                {key: deque([(now, temp)], maxlen=512) for key, (temp, stamp) in observations.items()},
                {key: stamp for key, (temp, stamp) in observations.items()}, feedback['observed_mono'], now)
            return condition('collecting')
        dt = now - window.at
        if not 0 <= dt < 6 or not 0 <= feedback['observed_mono'] - window.feedback_at < 6:
            self._ceiling_window = None
            return condition('fresh_evidence')
        for key, (temp, stamp) in observations.items():
            if not 0 <= stamp - window.source_times[key] < 6:
                self._ceiling_window = None
                return condition('fresh_evidence', key)
        if not advancing or feedback['observed_mono'] == window.feedback_at:
            state.failure_credit = window.credit
            return condition('advancing_evidence')
        for key, (temp, stamp) in observations.items():
            if stamp == window.source_times[key]:
                state.failure_credit = window.credit
                return condition('advancing_evidence', key)
        window.context = extended
        window.credit = min(60, window.credit + min(3, dt, feedback['observed_mono'] - window.feedback_at,
            *(stamp - window.source_times[key] for key, (temp, stamp) in observations.items())))
        window.at, window.feedback_at = now, feedback['observed_mono']
        for key, (temp, stamp) in observations.items():
            window.source_times[key] = stamp
            points = window.points[key]
            points.append((now, temp))
            while len(points) > 1 and points[1][0] <= now - 60:
                points.popleft()
        state.failure_credit = window.credit
        if window.credit < 60:
            return condition('collecting')
        for key, points in window.points.items():
            if now - points[0][0] < 60:
                return condition('temperature_window', key)
            values = [temp for at, temp in points]
            first = [temp for at, temp in points if at <= points[0][0] + 15]
            last = [temp for at, temp in points if at >= now - 15]
            if not robust_slope(list(points)) <= .01:
                return condition('temperature_slope', key)
            if max(values) - min(values) > 2:
                return condition('temperature_range', key)
            if median(last) > median(first) + .3:
                return condition('temperature_median', key)
        self._ceiling_recovered.update(key for key, failure in blockers if key in dangers)
        condition('ready')
        return True

    def _ceiling_response(self, edge, frame, advancing):
        live, now = [], frame['now']
        observations = {key: (temp, stamp) for key, temp, stamp in self.thermal_view.observations}
        for row in edge['sources']:
            key = tuple(row['source'])
            raw = frame['evidence'].get((key[0], key[3], key[4]))
            if not row['attributable'] or raw is None or key not in observations:
                continue
            temp, stamp = observations[key]
            points = edge['response_points'].setdefault(key, deque(maxlen=512))
            if points and not 0 <= stamp - points[-1][0] < 6:
                points.clear()
            if advancing and (not points or stamp > points[-1][0]):
                points.append((stamp, temp))
                while len(points) > 1 and points[1][0] <= stamp - 30:
                    points.popleft()
            crossing = row['temp_c'] < 80 <= temp
            covered = (bool(points) and stamp - points[0][0] >= 30
                and points[0][0] >= edge['confirmed_source_times'][key]
                and all(0 < b[0] - a[0] < 6 for a, b in zip(points, list(points)[1:])))
            blocks = [[v for t, v in points if stamp - 30 + i * 10 <= t < stamp - 20 + i * 10]
                for i in range(3)]
            sustained = (covered and temp >= 73 and temp - row['temp_c'] > 2
                and robust_slope(list(points)) > 0 and raw['guard_projected'] >= 80
                and all(blocks) and all(median(a) < median(b) for a, b in zip(blocks, blocks[1:])))
            if crossing or sustained:
                live.append((key, row, raw))
        return live

    def effective_limit(self, now):
        return MAX_LEVEL if not self.high_wind_gate else self.high_wind.normal_ceiling_at(now)

    def _capacity_policy(self, frame, actual, context, identity, healthy, matched, advancing):
        ledger, now = self._empirical, frame["now"]
        evidence = frame["evidence"]
        workload_class = tuple(sorted((n["host"],
            "active" if max(n.get("cpu_load") or 0, n.get("gpu_load") or 0) >= 30 or (n.get("power_w") or 0) >= 80
            else "idle" if max(n.get("cpu_load") or 0, n.get("gpu_load") or 0) <= 15 else "middle")
            for n in frame["nodes"]))
        attribution = (healthy and matched and self.feedback_matches_request(actual,
            frame["feedback"]["observed_mono"], now))
        operation = self._policy["operation"]
        pending_endpoint = (ledger.deadline is not None and healthy and self._pending()
            and actual == self.operating.confirmed_level == operation["episode"].original_from
            and operation["purpose"] == "reserve" and actual < operation["destination"] <= ledger.ceiling
            and now < operation["episode"].original_deadline)
        trusted = (healthy and context is not None and (attribution or pending_endpoint)
            and all(n.get("hostname_verified") is True for n in frame["nodes"])
            and all(any(key[0] == n["host"] for key in evidence) for n in frame["nodes"])
            and not frame["telemetry_fault"] and not frame["wifi_gap"] and not frame["uncertain"]
            and bool(evidence))
        comparable = (ledger.identity == identity and ledger.workload_class == workload_class
            and self._extend_context(ledger.context, context) is not None)
        continuous = ledger.last is not None and 0 < now - ledger.last <= 6
        if ledger.deadline is not None and now >= ledger.deadline:
            ledger.retire("expired", "实测增强周期600秒截止，须重新取得实际10档证据")
        if ledger.deadline is not None and (not trusted or not comparable or not continuous):
            ledger.retire("degraded", "来源、反馈或可比工况中断，实测证据已退役")
        if not trusted:
            ledger.clear_window()
            ledger.phase, ledger.detail = "degraded", "完整新鲜温度、实际正转速或可比负载证据不足"
            return
        if not comparable:
            ledger.clear_window()
            ledger.identity, ledger.context = identity, context
            ledger.workload_class = workload_class
        else:
            ledger.context = self._extend_context(ledger.context, context)
        if not continuous:
            ledger.clear_window()
        if actual != ledger.level:
            ledger.clear_window()
            ledger.level = actual
        recovered = all(s["temp"] < s["target"] - 2 for s in evidence.values())
        if not recovered:
            ledger.recovery_credit = 0
            ledger.recovery_ready = False
        for key, sensor in evidence.items():
            if ledger.deadline is not None and key != ledger.witness:
                continue
            ledger.points.setdefault(key, deque(maxlen=256)).append((now, sensor["temp"]))
        if advancing:
            dt = min(3, now - ledger.last) if ledger.last is not None else 0
            ledger.credit += dt
            ledger.recovery_credit = ledger.recovery_credit + dt if recovered and ledger.recovery_ready else 0
            ledger.recovery_ready = recovered
            ledger.last = now
        if ledger.deadline is not None and ledger.recovery_credit >= 60:
            ledger.retire("recovering", "低于工作目标减2°C的完整恢复持续60秒，结束增强需求")
            return
        original = evidence.get(ledger.witness)
        cooling = original is not None and (original["fall"] or original["temp"] <= original["target"] + 1)
        if ledger.deadline is not None and cooling and ledger.ceiling > actual:
            ledger.ceiling = actual
            ledger.points.clear()
            ledger.credit = 0
            ledger.last = now
        seconds = 180 if ledger.deadline is None or actual == 10 else 60
        witness = None
        if advancing and not cooling and ledger.credit >= seconds:
            for key, sensor in evidence.items():
                if ledger.deadline is not None and key != ledger.witness:
                    continue
                points = list(ledger.points.get(key, ()))
                boundary = [index for index, (stamp, _) in enumerate(points) if stamp <= now - seconds]
                points = points[boundary[-1]:] if boundary else []
                block = min(30, seconds / 4)
                early = [v for t, v in points if t <= now - seconds + block]
                late = [v for t, v in points if t > now - block]
                threshold = sensor["target"] + 1
                if (self._thermal_covered(points, now, seconds, False) and early and late
                        and all(v > threshold for _, v in points)
                        and median(early) - median(late) < .5):
                    witness = key
                    break
        if ledger.deadline is None and actual == 10 and witness is not None:
            ledger.deadline, ledger.ceiling, ledger.witness = now + 600, 11, witness
            ledger.window_seconds = 180
            ledger.clear_window()
            ledger.level, ledger.last = actual, now
        elif (ledger.deadline is not None and actual >= ledger.ceiling and witness is not None
                and actual < MAX_LEVEL):
            ledger.ceiling = actual + 1
            ledger.clear_window()
            ledger.level, ledger.last = actual, now
        if ledger.deadline is not None:
            ledger.phase = "empirical"
            ledger.detail = "原见证持续高于工作目标加1°C且无0.5°C恢复；当前高档按60秒响应逐档观察"
            if advancing and not self.high_wind.valid_at(now):
                key = ledger.witness
                sensor = evidence.get(key)
                if sensor is not None:
                    node = next(n for n in frame["nodes"] if n["host"] == key[0])
                    observed = _source_observed(frame['nodes'], now)
                    if observed is None:
                        return
                    observed = min(frame['feedback']['observed_mono'], observed)
                    ceiling = min(ledger.ceiling, actual + 1)
                    if sensor["fall"]:
                        ceiling = min(ceiling, actual)
                    self.high_wind = HighWindAuthority(True, (key[0], node["hostname"], key[1]),
                        observed, sensor["temp"], TenLevelNonRecovery(sensor["target"],
                            ledger.window_seconds, ledger.deadline, actual, ceiling))
        elif actual == 10 and ledger.phase not in ("expired", "recovering"):
            ledger.phase, ledger.detail = "observing", f"实际10档同源观察 {ledger.credit:g}/180秒，尚未证明容量不足"
        elif actual < 10:
            ledger.phase, ledger.detail = "normal", "尚无实际10档容量证据"

    def _safe_exit_authority(self, frame, actual, *, destination=None, deadline=None):
        evidence, now = frame["evidence"], frame["now"]
        operation = self._policy["operation"]
        endpoint = (actual == self.operating.confirmed_level or self._pending()
            and operation["purpose"] == "release" and operation["episode"].original_from == self.operating.confirmed_level
            and actual == operation["destination"] == operation["episode"].original_to
            and operation["destination_requested_at"] < frame["feedback"].get("observed_mono", -math.inf)
            and now < operation["episode"].original_deadline)
        if (actual is None or actual <= 10 or frame["telemetry_fault"] or frame["wifi_gap"]
                or frame["uncertain"] or not evidence
                or not self._feedback_valid(frame["feedback"], now)
                or not endpoint
                or not all(s["temp"] < s["target"] - 2 for s in evidence.values())
                or not all(any(key[0] == n["host"] for key in evidence) for n in frame["nodes"])
                or not all(n.get("online") and n.get("hostname_verified") for n in frame["nodes"])):
            return
        key, sensor = next(iter(evidence.items()))
        node = next(n for n in frame["nodes"] if n["host"] == key[0])
        observed = _source_observed(frame['nodes'], now)
        if observed is None:
            return
        observed = min(frame['feedback']['observed_mono'], observed)
        self.high_wind = HighWindAuthority(True, (key[0], node["hostname"], key[1]), observed,
            sensor["temp"], NoAscent(actual, actual, destination, deadline))

    def _quarantine_high_exit(self, now, identity=None):
        saved = self._thermal_memory.open_edge
        if saved is None or saved.get('release_basis') != HIGH_EXIT_TAG:
            return
        saved['stage'] = 'unresolved'
        for row in saved['sources']:
            key, base = tuple(row['source']), list(row['baseline'])
            row['attributable'] = False
            self._thermal_memory.add_failure(key, dict(safe_level=saved['from_level'],
                failed_level=saved['to_level'], baseline=base, load=max(base[:2]), power=base[2],
                automatic=saved['automatic'], failed_at=now, identity=identity,
                witness=dict(source=(key[0], key[3], key[4])), failure_class='unresolved_descent'))
        self.operating.observe_credit = 0
        self.operating.evidence_qualified = False

    def _qualify_high_exit(self, frame, actual, context, healthy, endpoint):
        now, evidence = frame['now'], frame['evidence']
        saved, window = self._thermal_memory.open_edge, self._high_exit_window
        tagged = saved is not None and saved.get('release_basis') == HIGH_EXIT_TAG
        if not healthy or not endpoint or actual is None or not 10 <= actual <= 16:
            self._high_exit_window = None
            return _HighExitQualification('high_exit_endpoint')
        nodes = {n['host']: n for n in frame['nodes']}
        expected = {(host, sensor, row['group']) for host, rows in self.expected.items()
            for sensor, row in rows.items()}
        if (frame['telemetry_fault'] or frame['wifi_gap'] or not evidence or set(evidence) != expected
                or context is None or set(nodes) != self.hosts
                or any(not n.get('online') or not n.get('hostname_verified') for n in nodes.values())):
            self._high_exit_window = None
            return _HighExitQualification('high_exit_source', next((k for k in expected if k not in evidence), None))
        identities = tuple(sorted(source_key(nodes[k[0]], k) for k in evidence))
        if any(not valid_source(k) for k in identities):
            return _HighExitQualification('high_exit_source')
        if tagged and (identities != tuple(sorted(tuple(row['source']) for row in saved['sources']))
                or saved['stage'] == 'unresolved' and (self._restored_edge or self._thermal_memory.failures)):
            if identities != tuple(sorted(tuple(row['source']) for row in saved['sources'])):
                self._quarantine_high_exit(now)
            self._high_exit_window = None
            return _HighExitQualification('high_exit_source')
        extended = self._extend_context(window.context, context, anchored_power=True) if window else None
        if window and (window.sources != identities or extended is None):
            if tagged:
                self._quarantine_high_exit(now)
            self._high_exit_window = None
            return _HighExitQualification('high_exit_source' if window.sources != identities else 'high_exit_context')
        feedback = frame['feedback'] or {}
        if (frame.get('node_hold') or feedback.get('readback_hold') or feedback.get('protection_interrupted')
                or feedback.get('readback_failure')
                or frame['manual'] is not None and not (tagged and saved['automatic'])):
            self._high_exit_window = None
            return _HighExitQualification('high_exit_endpoint')
        if frame['power_warning']:
            if window is not None:
                window.since = window.last = now
            return _HighExitQualification('high_exit_power')
        if window is None or window.level != actual or not 0 < now - window.last <= 6:
            origins = ({tuple(row['source']): row['exit_origin_c'] for row in saved['sources']} if tagged
                else dict(window.origins) if window else {})
            self._high_exit_window = window = _HighExitWindow(actual, identities,
                extended or context, now, now, origins=origins)
        else:
            window.context, window.last = extended, now
        metrics, blockers, complete = [], [], True
        for key, raw in sorted(evidence.items()):
            source = source_key(nodes[key[0]], key)
            points = [p for p in self.history[key[0]][key[1]] if p[0] >= window.since]
            window.peaks[source] = max((value for _, value in points), default=raw['temp'])
            sixty = self._thermal_window(points, now, 60)
            long = self._thermal_window(points, now, DRIFT_SECONDS)
            covered60 = self._thermal_covered(sixty, now, 60, key[2] == 'wifi')
            covered180 = self._thermal_covered(long, now, DRIFT_SECONDS, key[2] == 'wifi')
            complete = complete and covered180
            slopes = [robust_slope(p) for p, covered in ((sixty, covered60), (long, covered180)) if covered]
            rate = round(max(0, raw['rise'], *slopes), 12)
            margin = 75 - max(raw['temp'], raw['guard_projected'], window.peaks[source] + DRIFT_SECONDS * rate)
            origin = window.origins.get(source)
            warming = max(0, max(raw['temp'], window.peaks[source]) - origin) if origin is not None else 0
            metrics.append((key, margin, 75 - raw['temp'], rate, warming))
            reason = ('high_exit_warming_budget' if round(warming, 12) > WARMING_BUDGET_C else
                'high_exit_recent_jump' if raw['step_recent'] else
                'high_exit_trend' if raw['rapid_rise'] or raw['persistent_heat'] or rate > DRIFT_RATE else
                'high_exit_margin' if round(margin, 12) < HIGH_EXIT_MARGIN_C else
                'high_exit_warmup' if not covered60 or not raw['covered'] else None)
            if reason:
                blockers.append((key, reason))
        worst = min(metrics, key=lambda row: row[1])
        blocking = blockers[0] if blockers else None
        reason = blocking[1] if blocking else 'high_exit_observation'
        if not blockers and not window.origins:
            window.origins = {source_key(nodes[k[0]], k): median(v for t, v in self.history[k[0]][k[1]]
                if now - 30 < t <= now) for k in evidence}
        return _HighExitQualification(reason, blocking[0] if blocking else worst[0],
            min(row[1] for row in metrics), min(row[2] for row in metrics), max(row[3] for row in metrics),
            max(row[4] for row in metrics), complete)

    def _high_exit_authority(self, frame, actual, qualification, *, destination=None, deadline=None):
        if (not qualification.qualified or self.high_wind.valid_at(frame['now'])
                and not isinstance(self.high_wind.proof, NoAscent)):
            return
        key = qualification.source
        node = next(n for n in frame['nodes'] if n['host'] == key[0])
        observed = _source_observed(frame['nodes'], frame['now'])
        if observed is None:
            return
        observed = min(frame['feedback']['observed_mono'], observed)
        self.high_wind = HighWindAuthority(True, (key[0], node['hostname'], key[1]), observed,
            frame['evidence'][key]['temp'], NoAscent(actual, actual, destination, deadline))

    def _high_exit_response(self, edge, frame):
        now, window = frame['now'], self._high_exit_window
        saved = self._thermal_memory.open_edge
        if saved is None or saved['stage'] == 'unresolved':
            return [], False
        origins = {tuple(row['source']): row['exit_origin_c'] for row in saved['sources']}
        witnesses, margins = [], []
        rising = edge.setdefault('rising_since', {})
        for key, raw in frame['evidence'].items():
            node = next(n for n in frame['nodes'] if n['host'] == key[0])
            source = source_key(node, key)
            if source not in origins:
                continue
            points = self.history[key[0]][key[1]]
            thirty = self._thermal_window(points, now, HIGH_EXIT_RESPONSE_SECONDS)
            covered = self._thermal_covered(thirty, now, HIGH_EXIT_RESPONSE_SECONDS, key[2] == 'wifi')
            rise = raw['temp'] - edge['reference'][(key[0], key[1])]
            rate = max(raw['rise'], robust_slope(thirty) if covered else 0)
            if round(rate, 12) >= DRIFT_RATE:
                rising.setdefault(key, now)
            else:
                rising.pop(key, None)
            jump = any(0 < b[0] - a[0] <= 6 and b[1] - a[1] >= 2
                for b in list(points)[-1:] for a in points if 0 <= now - a[0] <= 6)
            if (raw['temp'] >= HIGH_EXIT_MEASURED_C or jump
                    or round(raw['temp'] - origins[source], 12) > WARMING_BUDGET_C
                    or rise >= HIGH_EXIT_REFERENCE_RISE_C and covered
                    and now - rising.get(key, now) >= HIGH_EXIT_RESPONSE_SECONDS):
                witnesses.append((key, raw))
            rates = [raw['rise'], 0]
            for seconds in (60, DRIFT_SECONDS) if window is not None else ():
                recent = self._thermal_window([p for p in points if p[0] >= window.since], now, seconds)
                if self._thermal_covered(recent, now, seconds, key[2] == 'wifi'):
                    rates.append(robust_slope(recent))
            if covered:
                margins.append(75 - max(raw['temp'], raw['guard_projected'],
                    window.peaks.get(source, raw['temp']) + DRIFT_SECONDS * max(rates)
                    if window is not None else raw['temp']))
        return witnesses, bool(margins) and round(min(margins), 12) < HIGH_EXIT_RESTORE_MARGIN_C

    @property
    def rules_only(self):
        return self._rules_only

    @property
    def upward(self):
        trial, state = self._policy["operation"], self._policy["state"]
        if self._pending() and trial["purpose"] in ("graded", "reserve"):
            return dict(level=self.level, requested_at=trial["episode"].original_requested_at,
                        confirmed_at=None, witness=None, elapsed=0)
        if state["tag"] != "graded":
            return None
        return dict(level=self.level, requested_at=state["since"],
                    confirmed_at=state["confirmed_at"], witness=state["witness"],
                    elapsed=state["earned"], last=state["last"], context=state["context"])

    def _pending(self):
        return self._policy["operation"] is not None and self._policy["operation"].get("episode") is not None

    def feedback_matches_request(self, requested_level, observed_mono, now):
        def timestamp(value):
            return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= now

        if (type(now) not in (int, float) or not math.isfinite(now) or now < 0
                or type(requested_level) is not int or not 0 <= requested_level <= MAX_LEVEL
                or not timestamp(observed_mono)):
            return False
        operation = self._policy["operation"]
        if operation is None:
            return True
        if not isinstance(operation, dict) or "episode" not in operation:
            return False
        destination, stamp = operation.get("destination"), operation.get("destination_requested_at")
        status, outcome = operation.get("status"), operation.get("outcome", "unknown")
        ended = ("invalidated", "failed", "execution_timeout", "recovery_released",
                 "success", "manual_complete", "inconclusive_timeout")
        completed = (status == "confirmed" and outcome == "confirmed"
                     and operation["episode"] is None)
        if (type(destination) is not int or not 0 <= destination <= MAX_LEVEL or not timestamp(stamp)
                or status not in ("pending", "confirmed", "observing") + ended
                or outcome not in (None,) + ended and not completed
                or status in ended and outcome != status):
            return False
        episode = operation["episode"]
        if episode is not None:
            if (not isinstance(episode, _Episode) or not timestamp(episode.original_requested_at)
                    or type(episode.original_deadline) not in (int, float)
                    or not math.isfinite(episode.original_deadline)
                    or not episode.original_requested_at <= stamp < episode.original_deadline
                    or now >= episode.original_deadline):
                return False
        elif status in ("pending", "confirmed", "observing"):
            confirmed = operation.get("confirmed_at")
            if not timestamp(confirmed) or confirmed <= stamp:
                return False
        return ((episode is None and destination != requested_level)
                or destination == requested_level and stamp < observed_mono)

    def _finish_trial(self, outcome):
        if self.trial is not None and (self.trial["outcome"] is None or
                                      outcome in ("invalidated", "failed") and self.trial["outcome"] in
                                      ("recovery_released", "success", "manual_complete")):
            self.trial["outcome"] = self.trial["status"] = outcome

    def _retire_execution(self, outcome, now, *, cause):
        self._ceiling_window = None
        operation = self._policy["operation"]
        if operation is None or (operation.get("episode") is None
                and operation.get("outcome") in ("invalidated", "failed", "execution_timeout")):
            return
        if (self.rules_only and self.high_wind_gate and self.trial is operation
                and operation.get("outcome") is not None):
            self.trial = dict(operation, episode=None,
                transition=dict(operation.get("transition") or {}))
        operation.update(episode=None, status=outcome, outcome=outcome,
            transition=dict(outcome=outcome, at=now, cause=cause, witness=None))
        self._remember_intent(operation, state=outcome)
        if operation.get('release_basis') == HIGH_EXIT_TAG:
            self._quarantine_high_exit(now)
        if self._thermal_memory.open_edge is not None and operation.get('purpose') != 'ceiling_exit':
            self._thermal_memory.open_edge['stage'] = 'unresolved'
            for row in self._thermal_memory.open_edge['sources']:
                row['attributable'] = False
        self.operating.observe_credit = self.operating.failure_credit = 0
        self.operating.last_evidence_at = None
        self.operating.evidence_qualified = False
        self._policy["last_downward"] = None
        if self.motion.source is None:
            self.motion = MotionAuthority()
        operation.pop("motion", None)

    def _retire_search(self):
        self.cool_since = self.baseline = self.search_since = None
        self.search_anchor = self.search_context = self.search_reference = None
        self.gear_window = None

    def _retire_guards(self):
        for expected in self.expected.values():
            for observation in expected.values():
                observation["guard_safe"] = False
                observation.pop("noise_pending", None)

    def _request(self, destination, actual, now, context, *, purpose, profile, reference=None, descent_limit=1):
        action_limit = (self.high_wind.ceiling_at(now) if self.high_wind_gate
            and purpose in ('ceiling_exit', 'rollback', 'protect')
            and isinstance(self.high_wind.proof, (NoAscent, FailedDescentRestoration, SourceFaultProtection))
            else self.effective_limit(now))
        destination = min(destination, action_limit)
        if (self.rules_only and self.high_wind_gate and actual is not None
                and max(actual, destination) > 10 and not self.high_wind.permits(now, actual, destination)):
            self.level = actual
            return
        operation = self._policy["operation"]
        if (self._policy["startup_required"] and operation is not None
                and operation["outcome"] == "execution_timeout"):
            self.level = self.effective_limit(now)
            return
        if self._pending():
            trial = self._policy["operation"]
            if destination != trial["destination"]:
                transition = dict(outcome="invalidated", at=now, cause="执行目的档位改变",
                                  old_destination=trial["destination"], destination=destination, witness=None)
                self._finish_trial("invalidated")
                if self.trial is trial:
                    trial["transition"] = transition
                    if self.rules_only and self.high_wind_gate:
                        self.trial = dict(trial, episode=None, transition=dict(transition))
                trial["destination"] = destination
                trial["destination_requested_at"] = now
                trial["attribution"] = "confounded"
                trial["purpose"], trial["profile"] = purpose, profile
                self._remember_intent(trial, new=True, state='pending')
                trial["transition"] = transition
                trial.pop("motion", None)
                if self.motion.source is None:
                    self.motion = MotionAuthority()
                self.operating.observe_credit = 0
                self.operating.last_evidence_at = None
                self.operating.evidence_qualified = False
            self.level = destination
            return
        if actual is None or destination == actual:
            self.level = destination
            return
        episode = _Episode(actual, destination, now, now + 60, descent_limit)
        operation = dict(from_=actual, to=destination, kind="probe" if purpose == "probe" else "release",
                          purpose=purpose, profile=profile, planned_step=episode.planned_step, requested_at=now, confirmed_at=None,
                          status="pending", outcome=None, observation_elapsed=None, elapsed=0,
                          deadline=episode.original_deadline, original_deadline=episode.original_deadline,
                          episode=episode, destination=destination,
                          destination_requested_at=now, baseline=context, attribution="comparable",
                          reference=reference, temperatures=self._temperatures())
        operation["from"] = operation.pop("from_")
        self._policy["operation"] = operation
        self._remember_intent(operation, new=True)
        if destination < actual:
            self.trial = operation
        self.level = destination
        self._policy["state"] = dict(tag="pending")
        self.phase = "wait" if destination < actual else "hold"
        if destination < actual:
            self.last_drop = now

    def _temperatures(self):
        return {(host, identity): observation["temp"]
                for host, expected in self.expected.items() for identity, observation in expected.items()}

    def _credit(self, tag, frame, eligible, *, witness=None):
        state = self._policy["state"]
        key = (tag, frame["actual"], frame["identity"], frame["caller"], witness)
        extended = self._extend_context(state.get("context"), frame["context"],
                                        strict=tag in ("cold_release", "graded"))
        reset_reason = None
        if state.get("key") != key or extended is None:
            reset_reason = "建立新的同源观察基线"
            state = dict(tag=tag, key=key, context=frame["context"], earned=0, last=None,
                         since=frame["now"], witness=witness, confirmed_at=frame["now"])
            self._policy["state"] = state
        else:
            state["context"] = extended
        if tag == "rules_release":
            means = self._workload_mean(frame["now"], 30)
            reference = state.get("workload_reference")
            increase = self._workload_increase(reference, means)
            if means is None:
                reset_reason = "30秒负载覆盖缺失，重置观察信用"
                state.update(earned=0, last=None, workload_reference=None)
                eligible = False
            elif reference is None:
                state["workload_reference"] = means
            elif increase is not None:
                reset_reason = increase + "，重置观察信用"
                state.update(earned=0, last=None, workload_reference=means)
                eligible = False
            state["release_reset"] = reset_reason
        if eligible:
            previous = state["last"]
            if previous is not None and 0 < frame["now"] - previous <= 6:
                state["earned"] += frame["now"] - previous
            state["last"] = frame["now"]
        else:
            state["last"] = None
        return state

    @staticmethod
    def _trial_view(diagnostic, *, execution=False):
        trial = None if diagnostic is None else {key: diagnostic[key] for key in
            ("from", "to", "kind", "status", "observation_elapsed", "outcome", "deadline", "elapsed")}
        if trial is not None:
            trial["planned_step"] = diagnostic.get("planned_step", 1)
            trial["profile"] = diagnostic.get("profile")
            trial["purpose"] = diagnostic.get("purpose")
            trial["transition"] = diagnostic.get("transition")
            for key in ("requested_at", "original_deadline", "destination_requested_at"):
                value = diagnostic.get("requested_at" if key == "destination_requested_at" and not execution else key)
                if type(value) in (int, float) and math.isfinite(value):
                    trial[key] = value
            if execution:
                trial.update(original_from=diagnostic["from"], original_to=diagnostic["to"],
                             to=diagnostic["destination"])
                if diagnostic.get("episode") is not None:
                    trial.update(status="pending", outcome=None)
                elif diagnostic.get("outcome") not in ("invalidated", "failed", "execution_timeout",
                        "inconclusive_timeout") and diagnostic.get("confirmed_at") is not None:
                    trial["status"] = "observing" if diagnostic["status"] == "observing" else "confirmed"
        return trial

    def _decision(self, scenario, detail, reasons=(), slopes=None, complete=None, eligible=False, *, now=None):
        now = (self.last_evaluation or 0) if now is None else now
        trial = self._trial_view(self.trial)
        execution = self._trial_view(self._policy["operation"], execution=True)
        grouped = {}
        for reason in dict.fromkeys(reasons):
            category = reason.split("]", 1)[0] if reason.startswith("[") else reason
            grouped.setdefault(category, []).append(reason)
        summaries = ["; ".join(items[:3]) + (f"; 同类另 {len(items) - 3} 项" if len(items) > 3 else "")
                     for items in grouped.values()]
        if not (self.rules_only and self.high_wind_gate):
            self.level = min(self.level, self.effective_limit(now))
        ledger = self._empirical
        valid = self.high_wind.valid_at(now)
        high_reason = "critical" if valid and (
            isinstance(self.high_wind.proof, (MeasuredCritical, Measured80))) else 'normal'
        high_detail = ledger.detail
        if high_reason == "critical":
            high_detail = "可信实温达到80°C增强准入；60/65°C目标与75°C风险线独立保留"
        if valid and isinstance(self.high_wind.proof, SourceFaultProtection):
            high_reason, high_detail = 'source_fault', self.high_wind.proof.fault.value
        if valid and isinstance(self.high_wind.proof, (NoAscent, SafeEndpointRestoration, FailedDescentRestoration)):
            high_reason = "recovering"
            high_detail = ("当前物理正转速安全端点允许禁止上升的单步退出" if isinstance(self.high_wind.proof, NoAscent)
                else "可归因失败下降，仅恢复原已确认安全端点，保留原执行截止")
        if self.high_wind.allowed and not valid:
            expiry = self.high_wind.expires_mono
            high_reason = "expired" if _finite(expiry) and now >= expiry else "degraded"
            high_detail = "高风动作权限已失效，等待新鲜同源温度与设备反馈；失效本身不授权降档"
        return {"level": self.level, "reasons": [detail] + summaries[:15],
                "policy_spec": policy_spec(),
                "control_status": dict(self._control_status) if self._control_status is not None else dict(
                    phase=high_reason if high_reason in ('critical', 'expired', 'degraded') else scenario,
                    primary_reason=scenario, blocking_source=None, margin_c=None,
                    observe_seconds=0, required_seconds=None, next_condition=None, workload_phase=None),
                "rise_c_per_min": slopes or {}, "trend_complete": complete or {}, "limits": LIMITS,
                "scenario": scenario, "scenario_detail": detail, "learning_eligible": eligible,
                "trial": trial, "execution_trial": execution,
                "retry_blocked": self.operating.failed_floor is not None if self.high_wind_gate and self.rules_only else False,
                "demand": (getattr(self, "demand", self.level) if valid and isinstance(self.high_wind.proof,
                    (SourceFaultProtection, FailedDescentRestoration)) else
                    min(getattr(self, "demand", self.level), self.effective_limit(now))),
                "normal_ceiling": self.effective_limit(now), "next_command": self.level,
                "actual_level": self._actuation_endpoint[2] if self._actuation_endpoint is not None
                    and 0 <= now - self._actuation_endpoint[4] < 6 else None,
                "transition_target": 10 if self.operating.release_basis == 'ceiling80' else None,
                "operating": dict(phase=self.operating.phase, observe_credit=self.operating.observe_credit,
                    failure_credit=self.operating.failure_credit,
                    failure_qualification=self.operating.failure_qualification,
                    last_transition=self.operating.last_transition, failed_floor=self.operating.failed_floor)
                    if self.high_wind_gate and self.rules_only else None,
                "high_wind_gate": self.high_wind_gate,
                "high_wind": dict(reason=high_reason, detail=high_detail[:240],
                    source=(self.high_wind.proof.source if valid and isinstance(self.high_wind.proof,
                        (SourceFaultProtection, FailedDescentRestoration)) else
                        tuple(str(v)[:160] for v in (self.high_wind.witness or ledger.witness)))
                        if self.high_wind.witness or ledger.witness else None,
                    actual_c=self.high_wind.actual_c, measured_max_c=self.thermal_view.maximum_c,
                    sources_complete=self.thermal_view.complete,
                    exception=type(self.high_wind.proof).__name__ if valid and isinstance(self.high_wind.proof,
                        (SourceFaultProtection, FailedDescentRestoration)) else None,
                    source_faults=[dict(source=k, fault=f.value, diagnosed_mono=t) for k, f, t in self.thermal_view.faults],
                    policy=policy_spec()['version'],
                    observation_seconds=min(180, ledger.credit), episode_deadline=ledger.deadline,
                    working_c=self.high_wind.proof.working_c if isinstance(self.high_wind.proof, TenLevelNonRecovery) else None,
                    window_seconds=ledger.window_seconds if ledger.deadline is not None else None,
                    prediction="unavailable", ceiling=self.effective_limit(now)),
                "high_wind_limit": self.effective_limit(now),
                "high_wind_witness": self.high_wind.witness if valid else None,
                "high_wind_expires_mono": self.high_wind.expires_mono}

    def pause_release(self, reason, now):
        self._ceiling_window = None
        self._high_exit_window = None
        self._quarantine_high_exit(now)
        self._control_status = dict(phase='hold', primary_reason='storage_hold', blocking_source=None,
            margin_c=None, observe_seconds=0, required_seconds=OBSERVE_SECONDS,
            next_condition='reliable_storage_and_fresh_evidence', workload_phase=self._workload_cycle.phase)
        self._failure_credits.clear()
        self._fixed_epochs.clear()
        self._cycle_proofs.clear()
        self.operating.observe_credit = 0
        self.operating.last_evidence_at = None
        self.operating.evidence_qualified = False
        self.operating.failure_credit = 0
        self.operating.settle_required = max(60, self.operating.settle_required)
        self._policy["last_downward"] = None
        if self._pending():
            operation = self._policy["operation"]
            if operation["destination"] < operation["episode"].original_from:
                self._retire_execution("invalidated", now, cause=reason)
                if self.operating.confirmed_level is not None:
                    self.level = self.operating.confirmed_level
        self.operating.last_transition = dict(outcome="invalidated", at=now, cause=reason, witness=None)
        if self.trial is not None and self.trial.get("outcome") == "invalidated":
            self.trial["transition"] = dict(self.operating.last_transition)
        return self._decision("storage_hold", reason, now=now)

    def protect(self, reason, now=None):
        self._ceiling_window = None
        self._control_status = dict(phase='hold', primary_reason='protection', blocking_source=None,
            margin_c=None, observe_seconds=0, required_seconds=OBSERVE_SECONDS,
            next_condition='fresh_complete_safe_evidence', workload_phase=self._workload_cycle.phase)
        self._failure_credits.clear()
        self._fixed_epochs.clear()
        self._cycle_proofs.clear()
        now = (self.last_evaluation if self.last_evaluation is not None else 0) if now is None else now
        self._high_exit_window = None
        self._quarantine_high_exit(now)
        if self.rules_only and self.high_wind_gate:
            if self.motion.source is None or now - self.motion.observed_mono >= HIGH_WIND_TTL:
                self.motion = MotionAuthority()
            self.operating.observe_credit = 0
            self.operating.failure_credit = 0
            self.operating.failure_qualification = None
            self.operating.last_evidence_at = None
            self.operating.evidence_qualified = False
            self.operating.settle_required = max(60, self.operating.settle_required)
            self.operating.last_transition = dict(outcome="invalidated", at=now, cause=str(reason), witness=None)
            self._finish_trial("invalidated")
            if self.trial is not None and self.trial.get("outcome") == "invalidated" and not self.trial.get("transition"):
                self.trial["transition"] = dict(self.operating.last_transition)
            self._policy["last_downward"] = None
            self._policy["fan_transport_safe"] = None
            self.demand = self.effective_limit(now)
            actual = self.operating.confirmed_level
            self.level = self.demand if actual is None else max(actual, min(self.demand, actual + 2))
            operation = self._policy["operation"]
            if self._pending() and now >= operation["episode"].original_deadline:
                self._policy["startup_required"] = True
                self._retire_execution("execution_timeout", now, cause="保护期间原执行截止到期")
            elif operation is not None and operation["destination"] != self.level:
                self._retire_execution("invalidated", now, cause=str(reason))
            self.operating.phase = "hold"
            return self._decision("protect", str(reason), now=now)
        actual = self.level
        self._finish_trial("invalidated")
        self._retire_search()
        self._retire_guards()
        self._request(MAX_LEVEL, actual, now, None, purpose="protect", profile="protect")
        self._policy.update(startup_required=True, protect_cause="fault", cold_band=False,
                            state=dict(tag="protect"), rebound=None, last_downward=None)
        self.phase = "protect"
        self.history.clear()
        self.positive_since.clear()
        self.load_history.clear()
        self.power_exposure.clear()
        self.workload_history.clear()
        self.workload_context = None
        self._policy["learning_window"].clear()
        self._policy["learning_key"] = None
        decision = self._decision("protect", str(reason), now=now)
        decision["high_wind_limit"] = self.effective_limit(now)
        return decision

    @staticmethod
    def _feedback_valid(feedback, now):
        if not isinstance(feedback, dict):
            return False
        stamp, rpm, level = (feedback.get(key) for key in ("observed_mono", "rpm", "level"))
        return (feedback.get("configured") is True and feedback.get("fault") is False
                and feedback.get("power") == "on" and feedback.get("mode") == "favorite"
                and type(level) is int and 0 <= level <= MAX_LEVEL
                and type(rpm) in (int, float) and math.isfinite(rpm) and rpm > 0
                and type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= now - stamp <= 9
                and (feedback.get("execution") == "pending" or
                     feedback.get("execution") == "matched" and feedback.get("confirmed") is True))

    def _context(self, nodes):
        context = {}
        for node in nodes:
            if not node.get("online"):
                return None
            power = node.get("power_w")
            source = node.get("power_source")
            if (type(power) not in (int, float) or not math.isfinite(power) or power < 0
                    or not source or source == "不可用"):
                return None
            host = node["host"]
            context[host] = (source, frozenset(self.expected[host]),
                             tuple((node[key], node[key]) for key in ("cpu_load", "gpu_load", "power_w")))
        return context

    @staticmethod
    def _extend_context(baseline, current, *, strict=True, anchored_power=False):
        if current is None or baseline is None or baseline.keys() != current.keys():
            return None
        extended = {}
        for host, (source, ids, ranges) in current.items():
            old_source, old_ids, old_ranges = baseline[host]
            if (source, ids) != (old_source, old_ids):
                return None
            combined = tuple((min(a[0], b[0]), max(a[1], b[1])) for a, b in zip(ranges, old_ranges))
            if strict and any(high - low > (5 if index < 2 else max(10,
                    .1 * (old_ranges[index][0] if anchored_power else (high + low) / 2)))
                   for index, (low, high) in enumerate(combined)):
                return None
            extended[host] = (source, ids, combined)
        return extended

    def _workload_mean(self, now, seconds):
        means = {}
        for host in self.hosts:
            points = list(self.workload_history.get(host, ()))
            boundary = [index for index, point in enumerate(points) if point[0] <= now - seconds]
            if not boundary or points[-1][0] != now:
                return None
            points = points[boundary[-1]:]
            if any(not 0 < b[0] - a[0] <= 6 for a, b in zip(points, points[1:])):
                return None
            means[host] = tuple(sum(b[1][index] * (b[0] - max(a[0], now - seconds))
                                   for a, b in zip(points, points[1:])) / seconds for index in range(3))
        return means

    @staticmethod
    def _workload_increase(reference, current):
        if reference is None or current is None or reference.keys() != current.keys():
            return "30秒负载参考或覆盖缺失"
        for host, values in current.items():
            for index, value in enumerate(values):
                delta, threshold = value - reference[host][index], 15 if index < 2 else 10
                if delta >= threshold:
                    metric = ("CPU", "GPU", "功率")[index]
                    unit = "个百分点" if index < 2 else "W"
                    return f"{host} 30秒{metric}均值增加{delta:.2f}/{threshold}{unit}"
        return None

    @staticmethod
    def _thermal_window(points, now, seconds):
        points = list(points)
        boundary = [index for index, point in enumerate(points) if point[0] <= now - seconds]
        return points[boundary[-1]:] if boundary else []

    @staticmethod
    def _thermal_covered(points, now, seconds, wifi):
        boundary = [index for index, point in enumerate(points) if point[0] <= now - seconds]
        if not boundary or points[-1][0] != now:
            return False
        points = list(points)[boundary[-1]:]
        return (all(0 < b[0] - a[0] <= (12 if wifi else 6) for a, b in zip(points, points[1:]))
                and all(sum(index * 30 <= now - stamp < (index + 1) * 30 for stamp, _ in points)
                        >= (3 if wifi else 5) for index in range(int(seconds // 30))))

    @staticmethod
    def _settled_step(history, now, target, wifi):
        if not Controller._thermal_covered(history, now, 180, wifi):
            return False
        points = list(history)
        changes = [index for index in range(1, len(points)) if points[index][1] != points[index - 1][1]]
        if len(changes) != 1:
            return False
        pre, post = points[:changes[0]], points[changes[0]:]
        return (0 < post[0][1] - pre[-1][1] <= 1 and post[-1][1] <= target - 3
                and Controller._thermal_covered(pre, pre[-1][0], 30, wifi)
                and Controller._thermal_covered(post, now, 30, wifi))

    def _noise_evidence(self, history, previous, now, target, wifi, slope):
        points = list(history)
        temp = points[-1][1]
        if temp > target - 3 or not self._thermal_covered(points, now, 30, wifi):
            return None, None
        increments, rise = 0, 0
        for a, b in zip(points, points[1:]):
            if b[1] < a[1]:
                increments, rise = 0, 0
            elif b[1] > a[1]:
                increments += 1
                rise += b[1] - a[1]
        if increments >= 2 and rise >= 1:
            return None, None
        pending = (previous or {}).get("noise_pending")
        if pending is not None:
            started, low, high = pending
            if now - started < 30 and low <= temp <= high + 1:
                if temp > high:
                    return "pending", pending
            else:
                return None, None
        prior = points[:-1]
        if not prior or not self._thermal_covered(prior, prior[-1][0], 30, wifi):
            return None, None
        boundary = max(index for index, point in enumerate(prior) if point[0] <= prior[-1][0] - 30)
        prior = prior[boundary:]
        low, high = min(value for _, value in prior), max(value for _, value in prior)
        pairs = [(b[1] - a[1]) / (b[0] - a[0]) for index, a in enumerate(prior)
                 for b in prior[index + 1:] if b[0] - a[0] >= 15]
        changes = [b[1] - a[1] for a, b in zip(prior, prior[1:])]
        if (high - low > 1 or abs(median(pairs)) >= .01
                or high != low and (sum(change > 0 for change in changes) < 2
                                   or sum(change < 0 for change in changes) < 2)):
            return None, None
        if low <= temp <= high and abs(slope) < .01:
            return "stationary", None
        if target - 5 < temp and high < temp <= high + 1:
            return "pending", (now, low, high)
        return None, None

    def _qualification(self, now, manual, cold, margin):
        samples = {(host, identity): self.history.get(host, {}).get(identity, ())
                   for host, expected in self.expected.items() for identity in expected}
        identities = self.search_anchor or samples
        boundary = self.gear_window["since"] if self.gear_window else math.inf
        settlement = self._settlement(samples, identities, now, not_before=boundary,
                                      symmetric=manual is None)
        settlement["safe"] = margin >= 3 and (manual is None or cold)
        settlement["qualified"] = settlement["safe"] and settlement["settled"]
        return settlement

    def _settlement(self, samples, identities, now, *, not_before=-math.inf, symmetric=True):
        covered, nonsettled, settled_sensors, blockers = bool(identities), set(), set(), []
        for key in identities:
            points = [point for point in samples.get(key, ()) if point[0] >= not_before]
            span = now - points[0][0] if points else 0
            wifi = self.expected[key[0]][key[1]]["group"] == "wifi"
            coverage = self._thermal_covered(points, now, 90, wifi)
            recent = [(stamp, value) for stamp, value in points if now - stamp <= 90]
            pairs = [(b[1] - a[1]) / (b[0] - a[0]) for index, a in enumerate(recent)
                     for b in recent[index + 1:] if b[0] - a[0] >= 15]
            slope = median(pairs) if pairs else None
            bins = [[value for stamp, value in recent if index * 30 <= now - stamp < (index + 1) * 30]
                    for index in range(3)]
            shift = median(bins[0]) - median(bins[2]) if bins[0] and bins[2] else None
            shape_ok = (slope is not None and shift is not None
                        and (abs(slope) if symmetric else slope) < .02
                        and (abs(shift) if symmetric else shift) <= (2 if wifi else 1))
            covered = covered and coverage
            if coverage and not shape_ok:
                nonsettled.add(key)
            if coverage and shape_ok:
                settled_sensors.add(key)
            else:
                blockers.append(f"{key[0]} {key[1].rsplit('/', 1)[-1]} coverage={span:g}/90s complete={coverage} "
                                f"bins={[len(values) for values in bins]} slope={slope}/0.02C/s median_delta={shift}/{2 if wifi else 1}C")
        return {"covered": covered, "nonsettled": nonsettled, "settled_sensors": settled_sensors,
                "settled": covered and not blockers,
                "detail": "; ".join(blockers[:2]) or "90s coverage/slope/median 均通过"}

    def _validate_admission(self, nodes, now):
        observations, faults, inventory = [], [], []
        previous_sources = dict(self._admission_sources)
        generation = self.thermal_view.generation + 1
        diagnosis = max((n.get('requested_mono', -math.inf) for n in nodes
            if _finite(n.get('requested_mono')) and n['requested_mono'] <= now), default=-math.inf)
        for host in sorted(self.hosts):
            entries = [n for n in nodes if n.get('host') == host]
            fallback = (host, host, 'unknown', 'required', 'other')
            if len(entries) != 1:
                faults.append((fallback, SourceFault.MISSING if not entries else SourceFault.IDENTITY, diagnosis))
                continue
            node = entries[0]
            requested, source = (node.get(k) for k in ('requested_mono', 'source_ts'))
            sensors = [s for s in node.get('sensors', ()) if s.get('calibrated') is True
                and s.get('group') in (*GROUPS, 'gpu') and s.get('group') != 'spbm']
            ids = tuple(sorted(((s.get('id', ''), s.get('group', '')) for s in sensors), key=str))
            fingerprint = (node.get('hostname'), node.get('power_source'), ids)
            old = self._admission_inventory.get(host)
            identity_ok = (node.get('hostname_verified') is True and not node.get('ambiguous_sensors')
                and len(ids) == len(set(ids)) and len({k[0] for k in ids}) == len(ids))
            timing = source_timing(node, now)
            advancing = timing is not None and source > self._admission_sources.get(host, -math.inf)
            source_keys = []
            for sensor in sensors:
                key = (host, sensor.get('id'), sensor.get('group'))
                full = (host, node.get('hostname'), node.get('power_source'), key[1], key[2])
                if (not node.get('online') or not identity_ok or not advancing or not valid_source(full)
                        or not _finite(sensor.get('temp')) or not 0 <= sensor['temp'] <= 150):
                    continue
                source_keys.append(full)
                observations.append((full, sensor['temp'], timing.observed_mono))
            groups = {s.get('group') for s in sensors}
            complete = (node.get('online') and identity_ok and advancing and len(source_keys) == len(sensors)
                and bool(groups & {'cpu', 'acpi'}) and 'gpu' in groups
                and bool(groups - {'cpu', 'acpi', 'gpu', 'spbm'}))
            missing_old = old is not None and (old[:2] != fingerprint[:2] or not set(old[2]).issubset(ids))
            if not complete or missing_old:
                fault = (SourceFault.MISSING if not node.get('online') or not sensors else
                    SourceFault.IDENTITY if not identity_ok or missing_old else
                    SourceFault.TIME if not advancing else SourceFault.REJECTED)
                faults.append((source_keys[0] if source_keys else fallback, fault,
                    requested if _finite(requested) else diagnosis))
            if complete and not missing_old:
                self._admission_inventory[host] = fingerprint
            elif complete and old is not None and old[:2] != fingerprint[:2]:
                self._admission_inventory[host] = fingerprint
            if _finite(source):
                self._admission_sources[host] = max(source, self._admission_sources.get(host, -math.inf))
            inventory.append((host, fingerprint))
        previous_faults = {key[0]: (key, fault, stamp) for key, fault, stamp in self.thermal_view.faults}
        for index, (key, fault, stamp) in enumerate(faults):
            previous = previous_faults.get(key[0])
            entry = next((n for n in nodes if n.get('host') == key[0]), {})
            source = entry.get('source_ts')
            advancing_fault = (any(k[0] == key[0] for k, temp, observed in observations)
                or _finite(source) and source > previous_sources.get(key[0], -math.inf))
            if previous is not None and not advancing_fault:
                faults[index] = (key, fault, previous[2])
        complete = not faults and bool(observations)
        self.thermal_view = ThermalView(generation, tuple(inventory), tuple(observations), tuple(faults),
            complete, max((r[1] for r in observations), default=None),
            min((r[2] + 6 for r in observations), default=None) if complete else None)
        hot = max((r for r in observations if r[1] >= 80), key=lambda r: r[1], default=None)
        if hot is not None:
            key, temp, stamp = hot
            self.high_wind = HighWindAuthority(True, (key[0], key[1], key[3]), stamp,
                temp, Measured80(key, generation))
        elif faults:
            key, fault, stamp = faults[0]
            self.high_wind = HighWindAuthority(True, (key[0], key[1], key[3]), stamp,
                None, SourceFaultProtection(key, generation, fault, 16))

    def decide(self, nodes, now, *, minimum_level=MIN_LEVEL, power_thresholds=None,
               feedback=None, manual_level=None):
        if not (self.rules_only and self.high_wind_gate and manual_level is None):
            self._fixed_epochs.clear()
            self._cycle_proofs.clear()
        self.high_wind = HighWindAuthority()
        self._control_status = None
        if self.high_wind_gate:
            self._validate_admission(nodes, now)
        self._actuation_endpoint = (tuple(feedback.get(k) for k in ('power', 'mode', 'level', 'rpm', 'observed_mono'))
            if self._feedback_valid(feedback, now) and 0 <= now - feedback['observed_mono'] < 6 else None)
        for value in (minimum_level, manual_level):
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_LEVEL):
                raise ValueError("档位必须为 0 到 16 的整数")
        if minimum_level is None:
            raise ValueError("最低档位不能为空")
        reasons, slopes, complete = [], {}, {}
        demand, quiet = 0, True
        warming, target_risk, wifi_gap = False, False, False
        thermal_risk, telemetry_fault = False, False
        soft_margin, qualified_event, cold = math.inf, False, True
        thermal_uncertain = False
        persistent_heat = False
        deficits = set()
        evidence = {}
        power_guard, power_warning = False, False
        load_event, low_load = None, True
        evaluation_elapsed = 0 if self.last_evaluation is None else max(0, now - self.last_evaluation)
        if self.last_evaluation is not None and (now <= self.last_evaluation or now - self.last_evaluation > 6):
            self.cool_since = None
            self.history.clear()
            self.positive_since.clear()
            reasons.append(f"[fault] 采样间隔异常 elapsed={evaluation_elapsed:g}，要求 0<dt<=6s，重新建立保护风力基线")
            demand, quiet = MAX_LEVEL, False
            telemetry_fault = True
        self.last_evaluation = now
        present = {node["host"] for node in nodes}
        for missing in self.hosts - present:
            self._policy["node_transport"].pop(missing, None)
        if {n["host"] for n in nodes} != self.hosts or len(nodes) != len(self.hosts):
            reasons.append(f"[fault] 节点缺失 expected={sorted(self.hosts)} actual={[n['host'] for n in nodes]}，保护风力请求")
            demand, quiet = MAX_LEVEL, False
            telemetry_fault = True
        node_records = self._policy["node_transport"]
        for node in nodes:
            host = node["host"]
            if (not node.get("online") or node.get("ambiguous_sensors")
                    or any(node.get(k) is None for k in ("cpu_temp", "gpu_temp", "board_temp"))):
                record = node_records.get(host)
                if (not node.get("online") and node.get("failure_kind") == "transport"
                        and record is not None and now >= record["last_good"]
                        and not self._policy["startup_required"]):
                    record.update(interrupted=True, recovering=None)
                    complete[host] = False
                    self.history.pop(host, None)
                    self.positive_since.pop(host, None)
                    reasons.append(f"[communication] {host} 节点通信中断，实际温度未知，暂停降档")
                    continue
                node_records.pop(host, None)
                reasons.append(f"[fault] {host} 温度缺失或身份不明确，保护风力请求")
                demand, quiet = MAX_LEVEL, False
                telemetry_fault = True
                self.history.pop(host, None)
                self.positive_since.pop(host, None)
                continue
            source_ts = node.get("source_ts")
            timing = source_timing(node, now)
            if timing is None:
                node_records.pop(host, None)
                reasons.append(f"[fault] {host} 来源时间无效、过期或超出0.5s时钟预算，严格6s有效期，保护风力请求")
                demand, quiet, telemetry_fault = MAX_LEVEL, False, True
                self.history.pop(host, None)
                self.positive_since.pop(host, None)
                continue
            if source_ts is None or source_ts <= self.sources.get(host, -math.inf):
                node_records.pop(host, None)
                reasons.append(f"[fault] {host} 采集时间未推进 source={source_ts} previous={self.sources.get(host)}，保护风力请求")
                demand, quiet = MAX_LEVEL, False
                telemetry_fault = True
                self.history.pop(host, None)
                self.positive_since.pop(host, None)
                continue
            else:
                self.sources[host] = source_ts
            identity = (node.get("hostname"), tuple(sorted((sensor["id"], sensor["group"]) for sensor in node["sensors"])))
            record = node_records.get(host)
            if record is not None and record["identity"] != identity:
                node_records.pop(host, None)
                if record.get("interrupted"):
                    telemetry_fault, demand = True, MAX_LEVEL
                    reasons.append(f"[fault] {host} 节点或传感器身份改变，保护风力请求")
            if (not self.rules_only and host in self.hosts and sum(n["host"] == host for n in nodes) == 1
                    and not node.get("ambiguous_sensors")):
                for sensor in node["sensors"]:
                    if not sensor["calibrated"]:
                        continue
                    hard = LIMITS["cpu" if sensor["group"] in ("cpu", "acpi") else
                                  "gpu" if sensor["group"] == "gpu" else "board"][1]
                    authority = HighWindAuthority(True, (host, node.get("hostname"), sensor["id"]),
                        timing.observed_mono, sensor["temp"], MeasuredCritical(hard))
                    if authority.valid_at(now):
                        self.high_wind = authority
            credible = [s for s in node["sensors"] if s["calibrated"]]
            if node.get("thermal_throttle") or node.get("ambiguous_sensors"):
                node_records.pop(host, None)
                reasons.append(f"[fault] {host} 热节流或传感器身份不明确 throttle={node.get('thermal_throttle')} ambiguous={node.get('ambiguous_sensors')}，保护风力请求")
                demand, quiet = MAX_LEVEL, False
                thermal_risk = thermal_risk or bool(node.get("thermal_throttle"))
                telemetry_fault = telemetry_fault or bool(node.get("ambiguous_sensors"))
            ids = {s["id"] for s in credible}
            expected = self.expected.setdefault(host, {})
            for identity, observation in expected.items():
                if identity in ids:
                    continue
                observation.pop("noise_pending", None)
                age = now - observation["seen_at"]
                if (observation["group"] == "wifi" and observation["guard_safe"]
                        and (observation["rate"] < .3 or observation["cold_qualified"])
                        and 0 <= age <= 30
                        and observation["temp"] + observation["rate"] * (age + 30) <= LIMITS["board"][0] - 2):
                    reasons.insert(0, f"[wifi] {host} {identity.rsplit('/', 1)[-1]} Wi-Fi 温度短暂缺失 age={now - observation['seen_at']:g}/30s，保留连续性，冻结下降/成功")
                    wifi_gap = True
                else:
                    reasons.append(f"[fault] {host} 传感器消失，保护风力请求 sensor={identity.rsplit('/', 1)[-1]} age={age:g}/30s last_guard_safe={observation['guard_safe']}")
                    demand, quiet = MAX_LEVEL, False
                    telemetry_fault = True
            histories = self.history.setdefault(host, {})
            positives = self.positive_since.setdefault(host, {})
            old_context = (self.workload_context or {}).get(host)
            noise_context = (old_context is not None
                             and (node.get("power_source"), frozenset(expected) | ids) == old_context[:2]
                             and node.get("power_w") is not None)
            load = max(node.get("cpu_load") or 0, node.get("gpu_load") or 0)
            power = node.get("power_w")
            learned = (power_thresholds or {}).get(host)
            threshold = learned.get("threshold_w") if isinstance(learned, dict) else None
            earlier = (host in POWER_HOSTS and node.get("power_source") == POWER_SOURCE
                       and isinstance(learned, dict) and learned.get("source") == POWER_SOURCE
                       and type(threshold) in (int, float) and math.isfinite(threshold)
                       and 40 <= threshold <= 80 and power is not None and power >= threshold)
            if earlier:
                reasons.append(f"[power] {host} power={power:g}W source={node.get('power_source')} 已发布整机功耗阈值 {threshold:g} W，常规需求至少 10 档")
                demand = max(demand, MIN_LEVEL)
                power_guard = True
                self.power_exposure.pop(host, None)
            elif type(power) in (int, float) and math.isfinite(power) and power >= 80:
                key = (node.get("hostname"), node.get("power_source"), frozenset(ids))
                exposure = self.power_exposure.get(host)
                if (exposure is None or exposure["key"] != key
                        or not 0 < now - exposure["last"] <= 6
                        or source_ts <= exposure["source_ts"]):
                    exposure = dict(key=key, start=now, last=now, source_ts=source_ts, count=0)
                    self.power_exposure[host] = exposure
                exposure.update(last=now, source_ts=source_ts, count=min(3, exposure["count"] + 1))
                sustained = exposure["count"] >= 3 and now - exposure["start"] >= 6
                if sustained:
                    reasons.append(f"[power] {host} power={power:g}W >=80W，连续至少3样本跨度={now - exposure['start']:g}/6s，常规需求至少10档")
                    demand = max(demand, MIN_LEVEL)
                    power_guard = True
                else:
                    power_warning = True
                    reasons.append(f"[power-warning] {host} power={power:g}W >=80W，连续样本={exposure['count']}/3 跨度={now - exposure['start']:g}/6s，暂停下降和学习")
            else:
                self.power_exposure.pop(host, None)
            if load >= 10:
                low_load = False
            if load >= 30:
                load_event = load_event or "load_spike"
            load_points = self.load_history.setdefault(host, deque(maxlen=3))
            load_points.append((now, node.get("cpu_load"), node.get("gpu_load")))
            if (len(load_points) == 3 and load_points[-1][0] - load_points[0][0] >= 6
                    and any(all(point[index] is not None and point[index] >= 30 for point in load_points)
                            for index in (1, 2))):
                load_event = "unverified_load"
                demand = max(demand, MIN_LEVEL)
                reasons.append(f"[load] {host} 持续负载 >=30%，3 样本跨度={load_points[-1][0] - load_points[0][0]:g}/6s，较低档位能力未验证")
            if node.get("cpu_load") is None or node.get("gpu_load") is None:
                reasons.append(f"[fault] {host} 负载数据缺失 cpu={node.get('cpu_load')} gpu={node.get('gpu_load')}，保护风力请求")
                demand, quiet = MAX_LEVEL, False
                telemetry_fault = True
            complete[host] = bool(credible)
            for sensor in credible:
                sensor_name = sensor['id'].rsplit('/', 1)[-1]
                group = "cpu" if sensor["group"] in ("acpi", "cpu") else (
                    "gpu" if sensor["group"] == "gpu" else "board")
                target, hard = LIMITS[group]
                temp = sensor["temp"]
                history = histories.setdefault(sensor["id"], deque(maxlen=128))
                previous = expected.get(sensor["id"])
                if sensor["group"] == "wifi" and previous is not None and now - previous["seen_at"] > 30:
                    history.clear()
                    positives.pop(sensor["id"], None)
                while len(history) > 1 and now - history[1][0] >= 180:
                    history.popleft()
                history.append((now, temp))
                sensor_drift = False
                if self._thermal_covered(history, now, 180, sensor["group"] == "wifi"):
                    medians = [median(value for stamp, value in history
                                      if index * 60 <= now - stamp < (index + 1) * 60)
                               for index in range(3)]
                    increment = 1 if sensor["group"] == "wifi" else .5
                    if (all(medians[index] - medians[index + 1] >= increment for index in range(2))
                            and not self._settled_step(history, now, target, sensor["group"] == "wifi")):
                        sensor_drift = True
                        persistent_heat = True
                        warming = True
                        reasons.append(f"[drift] {host} {sensor_name} 三个 60s 温度中位数持续上行 "
                                       f"old/new={medians[::-1]}，持续升温趋势证据")
                boundary = [index for index, point in enumerate(history) if point[0] <= now - 30]
                points = list(history)[boundary[-1]:] if boundary else list(history)
                pairs = [(b[1] - a[1]) / (b[0] - a[0]) for index, a in enumerate(points)
                         for b in list(points)[index + 1:] if b[0] - a[0] >= 15]
                slope = median(pairs) if pairs else 0.0
                if slope >= 0.02:
                    positives.setdefault(sensor["id"], now)
                else:
                    positives.pop(sensor["id"], None)
                confirmed_warming = now - positives.get(sensor["id"], now) >= 30
                span = now - points[0][0]
                ready = span >= 30 and all(b[0] - a[0] <= (30 if sensor["group"] == "wifi" else 6)
                                          for a, b in zip(points, list(points)[1:]))
                complete[host] = complete[host] and ready
                endpoints = [(temp - value) / (now - stamp) for stamp, value in points
                             if 9 <= now - stamp <= 15]
                rapid = endpoints[-1] if endpoints else 0.0
                long = (temp - points[0][1]) / span if span else 0.0
                safety = max(0, slope, rapid, long)
                if span < 9 and len(points) > 1:
                    safety = max(safety, (temp - points[-2][1]) / (now - points[-2][0]))
                slopes[host] = max(slopes.get(host, 0), slope * 60)
                projected = temp + max(0, slope) * 30
                recent = list(points)[-3:]
                sustained_rise = (len(recent) == 3 and recent[-1][0] - recent[0][0] >= 6
                                  and all(b[0] > a[0] and (b[1] - a[1]) / (b[0] - a[0]) >= .3
                                          for a, b in zip(recent, recent[1:])))
                fast_rate = min((b[1] - a[1]) / (b[0] - a[0])
                                for a, b in zip(recent, recent[1:])) if sustained_rise else 0
                fast_until = (now + 30 if sustained_rise and temp + 30 * fast_rate >= target - 2
                              else (previous or {}).get("fast_until", -math.inf))
                recent_fast = sustained_rise
                qualified = (ready and temp <= target - 5
                             and projected <= target - 2 and not recent_fast)
                noise, pending = (self._noise_evidence(history, previous, now, target,
                                   sensor["group"] == "wifi", slope)
                                  if noise_context and not (recent_fast or sustained_rise or confirmed_warming or sensor_drift)
                                  else (None, None))
                expected[sensor["id"]] = {"group": sensor["group"], "seen_at": now,
                                          "temp": temp, "rate": safety,
                                          "cold_qualified": qualified, "guard_safe": not (sustained_rise or recent_fast),
                                          "fast_until": fast_until, "noise_pending": pending}
                soft = max(0, slope, long, min(rapid, .05)) if qualified else safety
                uncertain = qualified and temp + safety * 30 >= target - 2
                if noise is not None:
                    soft = max(0, slope) if noise == "stationary" else 0
                    projected = temp + soft * 30
                    uncertain = noise == "pending"
                    reasons.append(f"[noise] {host} {sensor_name} {noise} T={temp:g} "
                                   f"effective={soft:.4f}C/s pending={pending}")
                if uncertain:
                    thermal_uncertain = True
                    reasons.append(f"[uncertain] {host} {sensor_name} 短时升温待确认，保持风力并冻结下降/成功/学习 "
                                   f"T={temp:g} raw_projection={temp + 30 * safety:.3f}C robust={slope:.4f}C/s")
                if qualified and noise is None and soft < safety:
                    qualified_event = True
                    reasons.append(f"[cold] {host} {sensor_name} qualified derivative-only raw={safety:.4f} "
                                   f"soft={soft:.4f}C/s raw_projection={temp + 30 * safety:.3f}C hard={hard}C "
                                   f"soft_projection={temp + 30 * soft:.3f}C history={span:g}/30s")
                soft_margin = min(soft_margin, target - max(temp, temp + soft * 30))
                cold = cold and ready and temp <= (55 if group == "board" else
                         47 if self._policy["cold_band"] else 45) and temp + soft * 30 <= target - 3
                ongoing = (len(recent) == 3 and recent[-1][0] - recent[0][0] >= 6
                           and all(0 < b[0] - a[0] <= 6 and b[1] > a[1]
                                   for a, b in zip(recent, recent[1:])))
                coherent = min((b[1] - a[1]) / (b[0] - a[0])
                               for a, b in zip(recent, recent[1:])) if ongoing else 0
                if (ready and self._thermal_covered(points, now, 30, sensor["group"] == "wifi")
                        and len(recent) == 3 and recent[-1][1] > recent[-2][1] >= recent[-3][1]):
                    continuation = (recent[-1][1] - recent[-2][1]) / (recent[-1][0] - recent[-2][0])
                    coherent = max(coherent, min(slope, continuation))
                covered = self._thermal_covered(points, now, 30, sensor["group"] == "wifi")
                step_points = [p for p in history if now - p[0] <= 36]
                positive_step_at = max((b[0] for index, b in enumerate(step_points)
                    if index and b[1] > step_points[index - 1][1] for a in step_points[:index]
                    if 0 < b[0] - a[0] <= 6 and b[1] - a[1] >= 2), default=None)
                step_recent = positive_step_at is not None and not self._thermal_covered(
                    [p for p in history if p[0] >= positive_step_at], now, 30, sensor["group"] == "wifi")
                falling = (covered and slope <= -.01 and len(recent) == 3
                           and recent[-1][0] - recent[0][0] >= 6
                           and all(0 < b[0] - a[0] <= 6 and b[1] <= a[1]
                                   for a, b in zip(recent, recent[1:]))
                           and recent[-1][1] < recent[0][1])
                accepted = temp + max(0, coherent) * 30
                witness = (host, sensor["id"], sensor["group"])
                evidence[witness] = dict(temp=temp, target=target, hard=hard, rise=coherent,
                                         fall=falling, covered=covered, projected=accepted,
                                         guard_projected=temp + 30 * max(safety, coherent),
                                         rapid_rise=recent_fast, positive_step_at=positive_step_at, step_recent=step_recent,
                                         group=group, slope=slope, persistent_heat=sensor_drift or confirmed_warming,
                                         release_margin=target - max(temp, temp + soft * 30), uncertain=uncertain)
                if (temp >= hard - 2 or coherent > 0 and accepted >= hard
                        or group in ("cpu", "gpu") and temp >= 70 and coherent > 0):
                    thermal_risk = True
                    reasons.append(f"[hard] {host} {sensor_name} 实温/连续升温预测触发保护风力 "
                                   f"T={temp:g} raw_projection={temp + safety * 30:.3f}/{hard}C "
                                   f"raw={safety:.4f} soft={soft:.4f}/0.3C/s robust={slope:.4f} long={long:.4f} "
                                   f"short={rapid:.4f} qualified={qualified} sustained_rise={sustained_rise} "
                                   f"fast_hold={max(0, fast_until - now):g}s")
                    demand, quiet = MAX_LEVEL, False
                else:
                    if temp > target - 2 or coherent > 0 and accepted > target - 2:
                        target_risk = True
                        reasons.append(f"[target] {host} {sensor_name} 当前下降余量不足 T={temp:g} accepted_projection={accepted:.3f}/{target - 2}C")
                    if coherent > 0 and accepted > target - 2:
                        demand = max(demand, MIN_LEVEL)
                        reasons.append(f"[target] {host} {sensor_name} 当前连续升温预测提前准备至少10档")
                    if temp > target or coherent > 0 and accepted > target:
                        if not falling:
                            deficits.add(witness)
                        demand = max(demand, MIN_LEVEL)
                    if noise is None and (slope >= 0.05 or confirmed_warming):
                        reasons.append(f"[warming] {host} {sensor_name} slope={slope:.4f}C/s 升温趋势证据")
                        warming = True
                if temp > target - 2 or temp + soft * 30 > target - 2 or not ready:
                    quiet = False
                    reasons.append(f"[margin] {host} {sensor_name} release T={temp:g} "
                                   f"soft_projection={temp + soft * 30:.3f}<={target - 2}C "
                                   f"history={span:g}/30s complete={ready}，重置安全观察")
            if expected.keys() - ids:
                complete[host] = False
        frame = dict(nodes=nodes, now=now, minimum=minimum_level, manual=manual_level,
                     feedback=feedback, demand=demand, quiet=quiet, warming=warming,
                     target_risk=target_risk, wifi_gap=wifi_gap, low_load=low_load,
                     load_event=load_event, reasons=reasons, slopes=slopes, complete=complete,
                     thermal_risk=thermal_risk, telemetry_fault=telemetry_fault,
                     margin=soft_margin, uncertain=thermal_uncertain, persistent_heat=persistent_heat,
                     cold=cold, power_guard=power_guard, power_warning=power_warning,
                     evidence=evidence, deficits=deficits)
        low_risk = (not telemetry_fault and not thermal_risk and not wifi_gap and not thermal_uncertain
                    and not power_guard and not power_warning and not load_event and not target_risk
                    and self._feedback_valid(feedback, now) and feedback.get("confirmed") is True
                    and feedback.get("execution") == "matched" and not self._policy["startup_required"])
        safe_thermal = (not telemetry_fault and not thermal_risk and not wifi_gap and not thermal_uncertain
                        and not power_guard and not power_warning and not load_event and not target_risk
                        and soft_margin >= 5 and bool(complete) and all(complete.values())
                        and bool(evidence) and all(sensor["covered"] for sensor in evidence.values()))
        self._storage_safe_at = now if safe_thermal else None
        if not (self.high_wind_gate and self.rules_only):
            self._policy["fan_transport_safe"] = now if safe_thermal else None
        elif safe_thermal and self._feedback_valid(feedback, now) and feedback.get("confirmed") is True:
            if not feedback.get("readback_hold") and not any(r.get("interrupted") for r in node_records.values()):
                self._policy["fan_transport_safe"] = now
        elif not (self.high_wind_gate and self.rules_only and not telemetry_fault and not thermal_risk
                  and not target_risk and not power_warning and not power_guard and not load_event and not wifi_gap
                  and all(n.get("online") or n.get("failure_kind") == "transport" for n in nodes)
                  and (any(r.get("interrupted") for r in node_records.values())
                       or isinstance(feedback, dict) and (feedback.get("readback_hold")
                           or feedback.get("readback_failure") == "initial_transport"))):
            self._policy["fan_transport_safe"] = None
        for node in nodes:
            host = node["host"]
            record = node_records.get(host)
            own_sensors = [sensor for key, sensor in evidence.items() if key[0] == host]
            node_low_risk = (low_risk and complete.get(host) and bool(own_sensors)
                             and all(sensor["covered"] and sensor["target"] - sensor["projected"] >= (5 if self.high_wind_gate and self.rules_only else 10)
                                     for sensor in own_sensors))
            if record is not None and not record.get("interrupted") and node.get("online") and not node_low_risk:
                node_records.pop(host)
                record = None
            if record is not None and record.get("interrupted") and node.get("online"):
                if not telemetry_fault and not thermal_risk and feedback and self._feedback_valid(feedback, now):
                    if record["recovering"] is None:
                        record["recovering"] = node["requested_mono"]
                    elif (node["requested_mono"] - record["recovering"] >= 3
                          and feedback["level"] >= min(self.effective_limit(now), max(record["safe"], 16 if (record["recovering"] if self.high_wind_gate and self.rules_only else now) - record["last_good"] >= 18
                                                        else 10 if (record["recovering"] if self.high_wind_gate and self.rules_only else now) - record["last_good"] >= 9 else 0))
                          and feedback.get("confirmed") is True and feedback.get("execution") == "matched"):
                        node_records.pop(host)
                        record = None
            if node_low_risk and (record is None or not record.get("interrupted")):
                node_records[host] = dict(last_good=node["requested_mono"], safe=feedback["level"],
                    identity=(node.get("hostname"), tuple(sorted((sensor["id"], sensor["group"]) for sensor in node["sensors"]))),
                    interrupted=False, recovering=None)
        interrupted = [(host, record) for host, record in node_records.items() if record.get("interrupted")]
        if interrupted:
            floor = max(max(record["safe"], 16 if (record["recovering"] if self.high_wind_gate and self.rules_only and record["recovering"] is not None else now) - record["last_good"] >= 18
                            else 10 if (record["recovering"] if self.high_wind_gate and self.rules_only and record["recovering"] is not None else now) - record["last_good"] >= 9 else 0) for _, record in interrupted)
            frame["node_hold"] = dict(level=floor, until=now + 3, failures=len(interrupted),
                                      stage="node", recovering=any(record["recovering"] is not None for _, record in interrupted),
                                      hosts=[host for host, _ in interrupted])
        if telemetry_fault or thermal_risk or wifi_gap:
            self.power_exposure.clear()
        if self.rules_only and self.high_wind_gate:
            return self._advance_fixed(frame)
        return self._advance(frame)

    def storage_safe(self, now):
        return self._storage_safe_at is not None and 0 <= now - self._storage_safe_at <= 6

    @property
    def communication_anchor(self):
        return self._policy["fan_transport_safe"]

    def communication_safe(self, now):
        stamp = self.communication_anchor
        return stamp is not None and 0 <= now - stamp <= (30 if self.high_wind_gate and self.rules_only else 6)

    @staticmethod
    def _returned_cycles(points, now, wifi, previous=None, binding=()):
        if not points:
            return None
        tolerance = 1 if wifi else .5
        if previous is None or previous.binding != binding:
            previous = _CycleProof(binding, points[0])
        last, opening = previous.last, previous.opening
        returns = list(previous.returns)
        issued, ceiling = previous.issued_at, previous.ceiling
        for stamp, value in points:
            if stamp <= last[0]:
                continue
            if stamp - last[0] > (30 if wifi else 6):
                returns, opening, issued, ceiling = [], None, None, None
            elif opening is None:
                if value > last[1]:
                    opening = (last[0], last[1], value)
            else:
                start, baseline, peak = opening
                peak = max(peak, value)
                if value < peak and value <= baseline + tolerance:
                    returns.append((start, stamp, baseline, peak, value))
                    returns = returns[-128:]
                    opening = None
                else:
                    opening = (start, baseline, peak)
            if ceiling is not None and value > ceiling:
                issued, ceiling = None, None
            last = (stamp, value)
        result = _CycleProof(binding, last, tuple(returns), opening, issued, ceiling)
        window = Controller._thermal_window(points, now, 180)
        large = [r for r in returns if r[3] - r[2] >= 2]
        if issued is None and large and len(returns) >= 3 and Controller._thermal_covered(window, now, 180, wifi):
            bound = max(r[3] for r in returns[:-1])
            if (round(robust_slope(window), 12) <= DRIFT_RATE
                    and returns[-1][3] <= bound
                    and (opening is None or opening[2] <= bound)
                    and points[-1][1] <= bound):
                result = _CycleProof(binding, last, tuple(returns), opening, now, bound)
        if Controller._thermal_covered(window, now, 180, wifi):
            span = max(v for _, v in window) - min(v for _, v in window)
            obsolete = result.growing or result.issued_at is None
            if ((span <= tolerance or obsolete and span < WARMING_BUDGET_C)
                    and round(robust_slope(window), 12) <= 0
                    and median(v for t, v in window if t > now - 60)
                        <= median(v for t, v in window if t <= now - 120)):
                return _CycleProof(binding, last)
        return result

    @staticmethod
    def _stationary_envelope(points, now, wifi):
        window = Controller._thermal_window(points, now, 180)
        if not Controller._thermal_covered(window, now, 180, wifi):
            return ()
        tolerance = 1 if wifi else .5
        medians = [median(value for stamp, value in window
            if index * 60 <= now - stamp < (index + 1) * 60) for index in range(3)]
        if (robust_slope(window) > 0 or medians[0] > medians[-1]
                or max(medians) - min(medians) >= tolerance):
            return ()
        cycle = Controller._returned_cycles(window, now, wifi)
        peaks = [r[3] for r in cycle.returns]
        cooling = all(b[1] <= a[1] for a, b in zip(window, window[1:]))
        if not cooling and (len(peaks) < 2 or max(peaks[1:]) > peaks[0] or cycle.growing
                or cycle.opening is not None and cycle.opening[2] > max(peaks)):
            return ()
        return window

    @staticmethod
    def _fixed_release_risk(raw, source, points, now, wifi, *, pending=False, stationary_points=None,
                            cycle=None):
        tolerance = 1 if wifi else .5

        def window(seconds):
            boundary = [index for index, point in enumerate(points) if point[0] <= now - seconds]
            return points[boundary[-1]:] if boundary else ()

        def slope(samples):
            pairs = [(b[1] - a[1]) / (b[0] - a[0]) for index, a in enumerate(samples)
                     for b in samples[index + 1:] if b[0] - a[0] >= 15]
            return median(pairs) if pairs else math.inf

        sixty, long = window(60), window(180)
        covered_long = Controller._thermal_covered(long, now, 180, wifi)
        blocks = [[value for stamp, value in long if index * 60 <= now - stamp < (index + 1) * 60]
                  for index in range(3)] if covered_long else []
        medians = [median(block) for block in blocks]
        changes = [index for index in range(1, len(long)) if long[index][1] != long[index - 1][1]]
        plateau = False
        if len(changes) == 1:
            pre, post = long[:changes[0]], long[changes[0]:]
            plateau = (post[0][1] > pre[-1][1] and raw["rise"] <= 0 and raw["slope"] <= 0
                and Controller._thermal_covered(pre, pre[-1][0], 30, wifi)
                and Controller._thermal_covered(post, now, 30, wifi))
        drift = (covered_long and all(medians[index] - medians[index + 1] >= tolerance for index in range(2))
                 and not plateau)
        sustained = False
        if Controller._thermal_covered(sixty, now, 60, wifi):
            trailing = [point for point in sixty if point[0] >= now - 30]
            sustained = bool(trailing) and all(slope(tuple(p for p in sixty
                if stamp - 30 <= p[0] <= stamp)) >= .02 for stamp, _ in trailing)
        guard_margin = raw["target"] - max(raw["temp"], raw["guard_projected"])
        special = (source["uncertain"] or raw["uncertain"] or raw["persistent_heat"] or raw["step_recent"]
            or pending and not raw["persistent_heat"] and raw["guard_projected"] > raw["temp"])
        margin = min(source["release_margin"], guard_margin) if special else source["release_margin"]
        drift_rate = round(slope(sixty), 12) if Controller._thermal_covered(sixty, now, 60, wifi) else math.inf
        drift_margin = round(raw['target'] - max(raw['temp'], raw['guard_projected'],
            raw['temp'] + DRIFT_SECONDS * max(0, drift_rate)), 12)
        bounded_drift = drift_rate <= DRIFT_RATE and drift_margin >= 8 and not raw['step_recent']
        cycle = cycle or Controller._returned_cycles(points, now, wifi)
        cycle_rate = round(cycle.floor_rate, 12) if cycle is not None else math.inf
        certified = (cycle is not None and cycle.issued_at is not None and cycle.issued_at < now
            and covered_long and math.isfinite(cycle_rate) and raw['temp'] <= cycle.ceiling
            and (not raw['step_recent'] or cycle.issued_at < (raw.get('positive_step_at') or -math.inf)))
        if not certified or cycle.growing:
            cycle_rate = drift_rate
        cycle_margin = round(raw['target'] - max(raw['temp'], raw['guard_projected'],
            max(cycle.ceiling, max(v for _, v in long)) + DRIFT_SECONDS * max(0, cycle_rate)), 12) if certified else margin
        if certified and cycle_margin < 8:
            return _ReleaseRisk('refused', min(margin, cycle_margin), 'raw_projection_margin')
        if cycle is not None and cycle.growing and not certified:
            return _ReleaseRisk('refused', min(margin, cycle_margin), 'growing_return_cycles')
        returned_heat = (certified and cycle.opening is None and cycle.returns
            and now - cycle.returns[-1][1] <= 30 and raw['slope'] <= 0 and raw['rise'] <= 0
            and raw['temp'] <= cycle.returns[-1][2] + tolerance)
        stationary = (Controller._stationary_envelope(
            points if stationary_points is None else stationary_points, now, wifi)
            if drift_rate > DRIFT_RATE and not certified else ())
        if stationary and not raw['persistent_heat'] and not raw['step_recent']:
            envelope_margin = round(raw['target'] - max(raw['temp'], raw['guard_projected'],
                max(value for _, value in stationary) + DRIFT_SECONDS * max(0, drift_rate)), 12)
            if raw['covered'] and not raw['rapid_rise'] and not sustained and envelope_margin >= 8:
                return _ReleaseRisk('ready', min(margin, envelope_margin), 'stationary_envelope')
            return _ReleaseRisk('refused', min(margin, envelope_margin))
        if not raw["covered"] or raw["rapid_rise"] or sustained or drift and not (bounded_drift or certified):
            return _ReleaseRisk("refused", margin, "coverage_or_genuine_rise")
        returns = len(cycle.returns) if cycle is not None else 0
        excursions = [r[3] for r in cycle.returns if r[3] - r[2] >= 2] if cycle is not None else []
        peak = cycle.opening[2] if cycle is not None and cycle.opening is not None else None
        expanding = covered_long and len(excursions) >= 2 and not certified and (
            max(excursions[1:]) > excursions[0] or peak is not None and peak > max(excursions))
        recovered_return = False
        if expanding:
            recent = window(30)
            envelope_margin = round(raw['target'] - max(raw['guard_projected'], raw['temp'],
                max(value for _, value in long) + DRIFT_SECONDS * max(0, drift_rate)), 12)
            recovered_return = (Controller._thermal_covered(recent, now, 30, wifi)
                and not raw['persistent_heat'] and drift_rate <= DRIFT_RATE
                and slope(recent) <= DRIFT_RATE and envelope_margin >= 8
                and max(medians) - min(medians) < tolerance
                and median(value for _, value in recent) <= min(medians) + tolerance
                and raw['temp'] <= max(excursions)
                and (peak is None or peak <= max(excursions)))
            if not recovered_return:
                if (not pending and not raw['persistent_heat'] and not raw['step_recent'] and envelope_margin >= 8
                        and raw['temp'] <= max(excursions)
                        and (peak is None or peak <= max(excursions))):
                    return _ReleaseRisk('observe_only', min(margin, envelope_margin),
                        'historical_expansion_observation')
                return _ReleaseRisk("refused", margin, "expanding_return_range")
            margin = min(margin, envelope_margin)
        bounded_return = False
        if Controller._thermal_covered(sixty, now, 60, wifi):
            values = [value for _, value in sixty]
            prior = sixty[:-1]
            boundary = [index for index, point in enumerate(prior) if point[0] <= prior[-1][0] - 30] if prior else []
            prior = prior[boundary[-1]:] if boundary else ()
            changes = [b[1] - a[1] for a, b in zip(sixty, sixty[1:])]
            flat = (prior and max(value for _, value in prior) == min(value for _, value in prior) == raw["temp"]
                    and raw["rise"] <= 0 and raw["slope"] <= 0)
            bounded_return = (max(values) - min(values) <= 2 and abs(slope(sixty)) < .01
                and Controller._thermal_covered(prior, prior[-1][0], 30, wifi)
                and min(value for _, value in prior) <= raw["temp"] <= max(value for _, value in prior)
                and (sum(change > 0 for change in changes) >= 2 and sum(change < 0 for change in changes) >= 2 or flat))
        if not bounded_return and covered_long:
            values = [value for _, value in long]
            bounded_return = (max(values) - min(values) <= 2 and returns >= 2
                and max(medians) - min(medians) < tolerance
                and all(min(block) <= raw["temp"] <= max(block) for block in blocks))
        bounded_return = bounded_return or recovered_return or certified
        if certified:
            margin = min(margin, cycle_margin)
        if math.isfinite(drift_rate) and drift_rate > 0 and not bounded_return and not plateau and not raw['step_recent']:
            margin = min(margin, drift_margin)
            if drift_rate > DRIFT_RATE or drift_margin < 8:
                return _ReleaseRisk('refused', margin, 'drift_rate_or_projected_margin')
        if bounded_drift:
            margin = min(margin, drift_margin)
        if raw["persistent_heat"] and not (bounded_return or bounded_drift) or special and guard_margin < 8:
            return _ReleaseRisk("refused", margin,
                'raw_projection_margin' if special and guard_margin < 8 else 'persistent_heat')
        cycle_hold = certified and (cycle.growing or drift_rate > DRIFT_RATE and slope(long) > 0
            or raw['persistent_heat'] and not returned_heat)
        return _ReleaseRisk("observe_only" if cycle_hold or raw["step_recent"] and not bounded_return else "ready", margin,
            'returned_cycle_observation' if cycle_hold else 'returned_cycle_bound' if certified else
            'recovered_return_range' if recovered_return else
            'bounded_slow_drift' if bounded_drift and drift_rate > 0 else 'bounded')

    def _record_anchor(self, key, frame):
        now = frame['now']
        node = next(n for n in frame['nodes'] if n['host'] == key[0])
        identity = self._source_memory_key(node, key)
        if identity is None:
            return None
        points = list(self.history[key[0]][key[1]])
        sixty = self._thermal_window(points, now, 60)
        wifi = key[2] == 'wifi'
        memory = self._thermal_memory
        covered = self._thermal_covered(sixty, now, 60, wifi)
        if identity not in memory.anchors and covered:
            blocks = [median(v for t, v in sixty if now - (index + 1) * 30 < t <= now - index * 30)
                for index in range(2)]
            memory.add_anchor(identity, median(v for _, v in sixty), max(blocks))
            if identity in memory.anchors:
                self._anchor_clock[identity] = now
        anchor = memory.anchors.get(identity)
        if anchor is None:
            return None
        last = self._anchor_clock.setdefault(identity, now)
        if now - last >= 30:
            block = self._thermal_window(points, now, 30)
            if self._thermal_covered(block, now, 30, wifi):
                anchor['peak_c'] = max(anchor['peak_c'], median(v for t, v in block if t > now - 30))
            self._anchor_clock[identity] = now
        return identity

    def _anchor_risk(self, key, raw, risk, frame, *, pending=False, cycle=None):
        identity = self._record_anchor(key, frame)
        if identity is None:
            return risk
        now = frame['now']
        points = list(self.history[key[0]][key[1]])
        wifi = key[2] == 'wifi'
        anchor = self._thermal_memory.anchors[identity]
        epoch = self._fixed_epochs.get(key[0])
        node = next(n for n in frame['nodes'] if n['host'] == key[0])
        idle = max(node['cpu_load'], node['gpu_load']) <= 15 and node['power_w'] < 80
        recent = self._thermal_window(points, now, 60)
        bounded_step_history = (self._thermal_covered(recent, now, 60, wifi)
            and max(v for _, v in recent) - min(v for _, v in recent) <= WARMING_BUDGET_C)
        returned_step = (cycle is not None and cycle.issued_at is not None
            and cycle.issued_at < (raw.get('positive_step_at') or now)
            and not cycle.growing and raw['temp'] <= cycle.ceiling)
        recovery_interval_eligible = (raw['covered'] and not raw['rapid_rise']
            and raw['target'] - max(raw['temp'], raw['guard_projected']) >= 8
            and not frame['thermal_risk'] and not pending
            and (cycle is None or not cycle.growing)
            and (not raw['step_recent'] or bounded_step_history or returned_step))
        if not recovery_interval_eligible or epoch is None:
            self._plateau_since.pop(identity, None)
        else:
            previous = self._plateau_since.get(identity)
            if previous is None or previous[1] != idle:
                self._plateau_since[identity] = (now, idle)
            since = self._plateau_since[identity][0]
            boundary = max(since, epoch.response_since if idle else epoch.since)
            if raw['step_recent'] and not bounded_step_history and returned_step:
                boundary = max(boundary, cycle.issued_at)
            plateau = self._thermal_window([p for p in points if p[0] >= boundary], now, 180)
            if (not pending and anchor['peak_c'] - anchor['anchor_c'] > WARMING_BUDGET_C
                    and self._thermal_covered(plateau, now, 180, wifi)):
                rate = round(robust_slope(self._thermal_window(plateau, now, 60)), 12)
                margin = round(raw['target'] - max(raw['temp'], raw['guard_projected'],
                    max(v for _, v in plateau) + DRIFT_SECONDS * max(0, rate)), 12)
                earliest = median(v for t, v in plateau if t <= now - 120)
                latest = median(v for t, v in plateau if t > now - 60)
                if robust_slope(plateau) <= 0 and latest <= earliest and margin >= 8:
                    blocks = [median(v for t, v in plateau
                        if now - (index + 1) * 30 < t <= now - index * 30) for index in range(6)]
                    anchor.update(anchor_c=latest, peak_c=max(latest, *blocks))
        if risk.kind != 'refused' and anchor['peak_c'] - anchor['anchor_c'] > WARMING_BUDGET_C:
            return _ReleaseRisk('refused', risk.margin_c, 'warming_budget_exhausted')
        return risk

    def _fixed_release(self, frame, sources, *, pending=False):
        release_sources, classifications = dict(sources), {}
        cycles = {}
        self._cycle_proofs = {key: value for key, value in self._cycle_proofs.items()
            if key in sources and key[0] in self._fixed_epochs}
        for key in sources:
            epoch = self._fixed_epochs.get(key[0])
            if epoch is None:
                continue
            node = next(n for n in frame['nodes'] if n['host'] == key[0])
            idle = max(node['cpu_load'], node['gpu_load']) <= 15 and node['power_w'] < 80
            since = epoch.response_since if idle else epoch.since
            binding = (self._source_memory_key(node, key), epoch.identity, since, idle)
            cycles[key] = self._returned_cycles(tuple(p for p in self.history[key[0]][key[1]]
                if p[0] >= since), frame['now'], key[2] == 'wifi', self._cycle_proofs.get(key), binding)
            if cycles[key] is not None:
                self._cycle_proofs[key] = cycles[key]
        raw_risks = {key: self._fixed_release_risk(frame['evidence'][key], source,
            tuple(self.history[key[0]][key[1]]), frame['now'], key[2] == 'wifi', pending=pending,
            cycle=cycles.get(key),
            stationary_points=tuple(p for p in self.history[key[0]][key[1]]
                if key[0] in self._fixed_epochs and p[0] >= self._fixed_epochs[key[0]].since))
            for key, source in sources.items()}
        risks = {}
        for key, source in sources.items():
            raw = frame["evidence"][key]
            risk = raw_risks[key]
            node = next(n for n in frame['nodes'] if n['host'] == key[0])
            if (not pending and risk.kind == 'refused' and risk.reason == 'drift_rate_or_projected_margin'
                    and risk.margin_c >= 8 and not raw['persistent_heat'] and not raw['rapid_rise']
                    and max(node['cpu_load'], node['gpu_load']) <= 15 and node['power_w'] < 80):
                risk = _ReleaseRisk('observe_only', risk.margin_c, 'bounded_drift_observation')
                sixty = self._thermal_window(self.history[key[0]][key[1]], frame['now'], 60)
                recent = self._thermal_window(sixty, frame['now'], 30)
                if (self._thermal_covered(sixty, frame['now'], 60, key[2] == 'wifi')
                        and self._thermal_covered(recent, frame['now'], 30, key[2] == 'wifi')
                        and robust_slope(recent) <= DRIFT_RATE and raw['rise'] <= DRIFT_RATE
                        and not raw['step_recent']):
                    values = [v for _, v in sixty]
                    rate = round(robust_slope(sixty), 12)
                    margin = round(raw['target'] - max(raw['guard_projected'],
                        max(values) + DRIFT_SECONDS * max(0, rate)), 12)
                    if max(values) - min(values) <= WARMING_BUDGET_C and margin >= 8:
                        risk = _ReleaseRisk('ready', min(risk.margin_c, margin), 'bounded_deceleration')
            risk = self._anchor_risk(key, raw, risk, frame, pending=pending, cycle=cycles.get(key))
            risks[key] = risk
            bounded = risk.kind != "refused"
            release_sources[key] = dict(source, uncertain=not bounded, release_margin=risk.margin_c)
            tag = "release-safe" if bounded else "release-refused"
            priority = 0 if risk.kind == "refused" else 1 if risk.kind == "observe_only" else 2
            classifications[key] = (priority, f"[{tag}] {key[0]} {key[1]} group={key[2]} "
                f"T={raw['temp']:g} guard_projected={raw['guard_projected']:.3f} "
                f"release_margin={release_sources[key]['release_margin']:.3f} "
                f"covered={raw['covered']} persistent={raw['persistent_heat']} "
                f"rapid={raw['rapid_rise']} step_recent={raw['step_recent']} risk={risk.kind} reason={risk.reason}")
        blocking = min((key for key, risk in risks.items() if risk.kind != 'ready'),
            key=lambda key: classifications[key][0], default=None)
        return dict(evidence=release_sources, margin=min(s["release_margin"] for s in release_sources.values()),
            uncertain=any(s["uncertain"] for s in release_sources.values()), classifications=classifications,
            observation_eligible=all(risk.kind != 'refused' or risk.reason == 'warming_budget_exhausted'
                for risk in risks.values()),
            blocking_source=blocking, reason=risks[blocking].reason if blocking else None,
            can_execute=all(risk.kind == "ready" for risk in risks.values()))

    def _fixed_ordinary(self, frame, actual, context, identity, admitted):
        now, raw = frame["now"], frame["evidence"]
        view = dict(evidence=raw, margin=frame["margin"], demand=frame["demand"],
                    deficits=frame["deficits"], uncertain=frame["uncertain"], reset=False,
                    release=dict(evidence=raw, margin=frame["margin"], uncertain=frame["uncertain"]))
        epochs = self._fixed_epochs
        if not admitted or len(context) > 4:
            view["reset"] = bool(epochs)
            epochs.clear()
            self._cycle_proofs.clear()
            return view
        if self._pending():
            return view
        identities = {source[0]: source for source in identity}
        for host, source_context in context.items():
            epoch = epochs.get(host)
            key = (identities[host], actual, frame["minimum"])
            current = {host: source_context}
            extended = self._extend_context(epoch.context, current, anchored_power=True) if epoch is not None else None
            continuous = epoch is not None and 0 < now - epoch.last <= 6
            if epoch is None or epoch.identity != key or extended is None or not continuous:
                view["reset"] = view["reset"] or epoch is not None and (epoch.identity != key or not continuous)
                response_since = (epoch.response_since if epoch is not None and epoch.identity == key and continuous else now)
                epochs[host] = _FixedEpoch(key, current, now, now, response_since)
            else:
                epoch.context, epoch.last = extended, now

        sources, classifications, filtered = dict(raw), {}, set()
        for key, sensor in raw.items():
            epoch, wifi = epochs[key[0]], key[2] == "wifi"
            points = [p for p in self.history[key[0]][key[1]] if p[0] >= epoch.since]
            if not self._thermal_covered(points, now, 30, wifi):
                if sensor["covered"]:
                    rate = max(0, min(sensor["rise"], sensor["slope"]))
                    projected = sensor["temp"] + rate * 30
                    provisional = sensor["rise"] >= .02 or sensor["slope"] >= .02
                    sources[key] = dict(sensor, rise=rate, projected=projected,
                        warming=sensor["rise"] > 0 or sensor["persistent_heat"],
                        uncertain=sensor["uncertain"] or provisional)
                    filtered.add(key)
                    if provisional or sensor["projected"] != projected:
                        classifications[key] = (0,
                            f"[provisional] {key[0]} {key[1]} group={key[2]} context_rebuilding "
                            f"T={sensor['temp']:g} raw_forecast={sensor['projected']:.3f} "
                            f"accepted_forecast={projected:.3f}，保留原始下降余量，暂停未确认升温下的下降")
                continue
            slope = sensor["slope"]
            positive = self.positive_since.get(key[0], {}).get(key[1], now)
            corroborated = slope >= .02 and now - max(positive, epoch.since) >= 30
            drift = False
            if self._thermal_covered(points, now, 180, wifi):
                medians = [median(value for stamp, value in points
                                  if index * 60 <= now - stamp < (index + 1) * 60)
                           for index in range(3)]
                drift = (all(medians[index] - medians[index + 1] >= (1 if wifi else .5)
                             for index in range(2))
                         and not self._settled_step(points, now, sensor["target"], wifi))
            corroborated = corroborated or drift
            noise, _ = self._noise_evidence(points, None, now, sensor["target"], wifi, slope)
            stationary = noise == "stationary" and not corroborated
            rising = sensor["rise"] > 0 or slope > 0 or drift
            provisional = not corroborated and not stationary and (sensor["rise"] >= .02 or slope >= .02 or noise == "pending")
            projected = sensor["temp"] + max(0, slope) * 30 if corroborated else sensor["temp"]
            source = dict(sensor, projected=projected, rise=max(0, slope) if corroborated else 0,
                          persistent_heat=corroborated, warming=rising and not stationary,
                          release_margin=sensor["target"] - max(sensor["temp"], projected), uncertain=provisional)
            sources[key] = source
            filtered.add(key)
            concern = source["rise"] > 0 and projected >= source["target"] - 2
            if stationary or provisional or sensor["projected"] != projected or concern:
                tag = "noise" if stationary else "provisional" if not corroborated else "target"
                status = "stationary" if stationary else "corroborated" if corroborated else "unconfirmed"
                reason = (f"[{tag}] {key[0]} {key[1]} group={key[2]} {status} "
                    f"T={source['temp']:g} raw_forecast={sensor['projected']:.3f} "
                    f"accepted_forecast={projected:.3f}")
                classifications[key] = (0 if provisional or concern else 1 if sensor["projected"] != projected else 2, reason)
        release = self._fixed_release(frame, sources)
        for key, classification in release["classifications"].items():
            if key in classifications:
                classifications[key] = (min(classifications[key][0], classification[0]),
                    classifications[key][1] + " " + classification[1])
            else:
                classifications[key] = classification
        replaced = tuple(f"[{tag}] {key[0]} {key[1].rsplit('/', 1)[-1]} "
            for key in filtered for tag in ("target", "warming", "margin", "uncertain"))
        frame["reasons"] = [reason for reason in frame["reasons"] if not reason.startswith(replaced)]
        frame["reasons"].extend(reason for _, reason in sorted(classifications.values())[:4])
        demand, deficits = 0, set()
        for key, source in sources.items():
            if source["temp"] > source["target"] or source["rise"] > 0 and source["projected"] > source["target"]:
                if not source["fall"]:
                    deficits.add(key)
                demand = MIN_LEVEL
            if source["rise"] > 0 and source["projected"] > source["target"] - 2:
                demand = MIN_LEVEL
        return dict(evidence=sources, margin=min(s["release_margin"] for s in sources.values()),
                    demand=demand, deficits=deficits, uncertain=any(s["uncertain"] for s in sources.values()),
                    reset=view["reset"], release=release)

    def _ceiling_transition(self, frame, actual, healthy, advancing, context, identity):
        now, state, policy = frame['now'], self.operating, self._policy
        view, feedback = self.thermal_view, frame['feedback'] or {}
        failure_ready = False
        qualification_diagnostic = {}
        validated_sources = {key for key, temp, stamp in view.observations}
        operation = policy['operation']
        ceiling_op = operation is not None and operation['purpose'] == 'ceiling_exit'
        nodes = {n['host']: n for n in frame['nodes']}
        baselines = host_baselines(frame['nodes'])
        edge = policy['last_downward']
        for candidate in (operation if ceiling_op else None, edge):
            if candidate is None:
                continue
            rows = candidate.get('ceiling_sources', candidate.get('sources', ()))
            endpoint_continuous = healthy and (actual in (candidate['from'], candidate['destination'])
                if candidate is operation and self._pending() else
                actual == (candidate['edge'][1] if candidate is edge else candidate['destination']))
            for row in rows:
                key = tuple(row['source'])
                raw = frame['evidence'].get((key[0], key[3], key[4]))
                if (not endpoint_continuous or raw is None or key not in validated_sources or key[0] not in nodes
                        or source_key(nodes[key[0]], (key[0], key[3], key[4])) != key
                        or row['baseline'] is None or increased(row['baseline'], baselines.get(key[0]))):
                    row['attributable'] = False
        below = view.complete and view.maximum_c < 80 and now < view.expires_mono
        if not below:
            state.failure_credit = 0
            state.failure_qualification = None
            self._failure_credits.clear()
        fault = self.high_wind.valid_at(now) and isinstance(self.high_wind.proof, SourceFaultProtection)
        edge_candidate = policy['last_downward']
        high_failed_edge = (edge_candidate is not None and edge_candidate['edge'][0] > 10
            and actual == edge_candidate['edge'][1])
        if ceiling_op and not self._pending() and actual is not None and actual <= 10 and not high_failed_edge:
            state.release_basis = 'ordinary'
            return None
        if not (ceiling_op or high_failed_edge or actual is not None and actual > 10
                and not isinstance(self.high_wind.proof, Measured80) or fault):
            return None
        if actual is None or actual > 10 or self._pending() or fault:
            state.observe_credit = 0
            state.evidence_qualified = False
            state.release_basis = 'ceiling80'
            self._fixed_epochs.clear()
            self._cycle_proofs.clear()
            self.motion = MotionAuthority()
            self.demand = min(10, max(frame['minimum'], frame['manual'] or 0, frame['demand']))
        next_eligible = max(self.last_drop + 30, feedback.get('next_motor_at', -math.inf))

        def result(reason, *, source=None):
            state.observe_credit = 0
            state.evidence_qualified = False
            if not failure_ready:
                if reason not in ('ceiling_exit_current_rapid_risk', 'ceiling_exit_hardware_alarm') or not self._pending():
                    self._ceiling_window = None
                state.failure_credit = 0
                state.failure_qualification = None
                self._failure_credits.clear()
            exceptional = self.high_wind.valid_at(now) and isinstance(self.high_wind.proof,
                (SourceFaultProtection, FailedDescentRestoration))
            if exceptional:
                state.release_basis = 'protection'
                source = source or self.high_wind.proof.source
            self.phase = state.phase = 'hold' if reason != 'ceiling_exit_pending' else 'descending'
            self._control_status = dict(phase='protecting' if exceptional else 'waiting_execution' if self._pending() else 'hold',
                primary_reason=reason, release_basis=state.release_basis, blocking_source=source,
                blocking_reason=None if reason == 'ceiling_exit_pending' else reason,
                transition_target=None if exceptional else 10, next_command=self.level, actual_level=actual if healthy else None,
                next_eligible_at=next_eligible if _finite(next_eligible) else None,
                original_deadline=policy['operation']['episode'].original_deadline if self._pending() else None,
                proof_expires_mono=self.high_wind.expires_mono, observe_seconds=0,
                required_seconds=0, margin_c=None, workload_phase=self._workload_cycle.phase,
                next_condition='exact_current_intent_and_motor_readback' if self._pending() else reason)
            if reason == 'ceiling_exit_requalifying':
                self._control_status.update(observe_seconds=state.failure_credit, required_seconds=60,
                    risk_evidence_expires_mono=view.expires_mono, next_condition=state.failure_qualification,
                    blocking_reason=state.failure_qualification, **qualification_diagnostic)
            return self._decision(reason, reason, frame['reasons'], frame['slopes'], frame['complete'], now=now)

        self.level = actual if actual is not None else 10
        if self._pending() and now >= operation['episode'].original_deadline:
            self._retire_execution('execution_timeout', now, cause='原始60秒执行截止到期')
            policy['startup_required'] = True
            return result('ceiling_exit_execution_timeout')
        if (healthy and self._pending() and advancing and actual == operation['destination']
                and feedback['observed_mono'] > operation['destination_requested_at']):
            operation.update(episode=None, confirmed_at=now, status='confirmed', outcome='confirmed')
            self._remember_intent(operation, state='confirmed')
            previous = state.confirmed_level
            state.confirmed_level = actual
            if previous is not None and actual < previous:
                self.last_drop = now
                next_eligible = max(next_eligible, now + 30)
            if ceiling_op:
                self._ceiling_window = None
                state.settle_required = 0
                if operation.get('ceiling_sources'):
                    policy['last_downward'] = dict(edge=(operation['from'], actual), identity=identity,
                        sources=operation['ceiling_sources'], automatic=operation['automatic'],
                        reference={(row['source'][0], row['source'][3]): row['temp_c']
                            for row in operation['ceiling_sources']}, context=operation['baseline'],
                        baselines={row['source'][0]: row['baseline'] for row in operation['ceiling_sources']
                            if row['baseline'] is not None},
                        purpose='ceiling_exit', confirmed_at=now, response_points={},
                        confirmed_source_times={key: feedback['observed_mono']
                            for key, temp, stamp in view.observations})
                state.last_transition = dict(outcome='confirmed', at=now, cause='ceiling_exit', witness=None)
                self.level = actual
        if policy['startup_required'] and operation is not None and operation.get('outcome') == 'execution_timeout':
            at = operation['transition']['at']
            if (healthy and advancing and feedback['observed_mono'] > at
                    and feedback.get('motor_attempts', feedback.get('attempts', 0)) == 0 and not feedback.get('motor_fault')
                    and feedback.get('execution') == 'matched' and feedback.get('confirmed') is True):
                policy['startup_required'] = False
                state.confirmed_level = actual
            return result('ceiling_exit_timeout_reconciliation')
        if not healthy or not _finite(feedback.get('observed_mono')) or not 0 <= now - feedback['observed_mono'] < 6:
            return result('ceiling_exit_fresh_endpoint')
        if feedback.get('protection_interrupted') or feedback.get('readback_failure') or feedback.get('readback_hold'):
            return result('ceiling_exit_endpoint_protection')
        if state.confirmed_level is None:
            matched = feedback.get('confirmed') is True and feedback.get('execution') == 'matched'
            pending_physical_endpoint_adoption = (actual > 10 and below and not self._pending()
                and feedback.get('confirmed') is False and feedback.get('execution') == 'pending'
                and not feedback.get('motor_fault') and feedback.get('motor_attempts', feedback.get('attempts', 0)) < 2)
            if not matched and not pending_physical_endpoint_adoption:
                return result('ceiling_exit_endpoint_reconciliation')
            state.confirmed_level = actual
        elif actual != state.confirmed_level:
            if not advancing:
                return result('ceiling_exit_endpoint_reconciliation')
            if self._pending():
                self._retire_execution('invalidated', now, cause='未请求的物理端点变化')
            state.confirmed_level = actual
            state.identity = identity
            return result('ceiling_exit_endpoint_changed')
        policy['startup_required'] = False
        if state.identity is not None and state.identity != identity:
            if self._pending():
                self._retire_execution('invalidated', now, cause='来源身份改变')
            state.identity = identity
            return result('ceiling_exit_identity_changed')
        state.identity = identity
        if self._thermal_memory.saturated:
            return result('ceiling_exit_memory_conflict')

        nodes = {n['host']: n for n in frame['nodes']}
        baselines = host_baselines(frame['nodes'])
        edge, saved = policy['last_downward'], self._thermal_memory.open_edge
        if edge is not None and edge.get('purpose') == 'ceiling_exit':
            saved = dict(stage='confirmed', sources=edge['sources'], automatic=edge['automatic'])
        live = []
        if edge is not None and edge.get('purpose') == 'ceiling_exit' and actual == edge['edge'][1] and edge['identity'] == identity:
            live = self._ceiling_response(edge, frame, advancing)
        elif (edge is not None and saved is not None and saved['stage'] == 'confirmed'
                and actual == edge['edge'][1] and edge['identity'] == identity):
            for row in saved['sources']:
                key = tuple(row['source'])
                raw = frame['evidence'].get((key[0], key[3], key[4]))
                if (not row['attributable'] or raw is None
                        or not any(source == key for source, temp, stamp in view.observations)
                        or edge.get('purpose') != 'ceiling_exit' and not raw['covered']
                        or source_key(nodes[key[0]], (key[0], key[3], key[4])) != key
                        or increased(row['baseline'], baselines.get(key[0]))):
                    continue
                rise = raw['temp'] - row['temp_c']
                crossing = row['temp_c'] < raw['target'] - 2 <= raw['temp']
                if crossing or rise > 2 and raw['persistent_heat'] and raw['temp'] > raw['target'] - 5:
                    live.append((key, row, raw))
        if live:
            key, row, raw = live[0]
            safe = edge['edge'][0]
            for failed_key, failed_row, failed_raw in live:
                self._thermal_memory.add_failure(failed_key, dict(safe_level=safe, failed_level=actual,
                    baseline=failed_row['baseline'], automatic=saved['automatic'], failure_class='thermal_failure',
                    failed_at=now, witness=dict(source=(failed_key[0], failed_key[3], failed_key[4]),
                        actual_c=failed_raw['temp'])))
                if edge.get('purpose') == 'ceiling_exit':
                    self._thermal_memory.remember_ceiling_danger(failed_key, safe_level=safe,
                        failed_level=actual, baseline=failed_row['baseline'])
                    self._ceiling_recovered.discard(failed_key + (actual,))
            self._ceiling_window = None
            state.failed_floor = max(self._thermal_memory.failures.values(), key=lambda f: f['failed_level'], default=None)
            state.last_transition = dict(outcome='failed', at=now, cause='attributable_descent_failure',
                witness=dict(source=key, actual_c=raw['temp'], safe_level=safe, failed_level=actual))
            if self._thermal_memory.saturated:
                return result('ceiling_exit_memory_conflict', source=key)
            deadline = operation['episode'].original_deadline if self._pending() else now + 60
            self.high_wind = HighWindAuthority(True, (key[0], key[1], key[3]),
                min(feedback['observed_mono'], next(stamp for source, temp, stamp in view.observations
                    if source == key)), raw['temp'],
                FailedDescentRestoration(actual, safe, deadline, key, (safe, actual), now, view.generation))
            self.motion = MotionAuthority(restore_level=safe, restore_from=actual, deadline=deadline)
            self.demand = safe
            self._request(safe, actual, now, context, purpose='rollback', profile='protect')
            if self._pending():
                policy['operation']['high_wind'] = self.high_wind
                policy['operation']['motion'] = self.motion
            return result('failed_descent_restoration', source=key)
        if not below:
            if ceiling_op and self._pending():
                return result('ceiling_exit_revoked_inflight')
            if fault:
                target = min(self.high_wind.ceiling_at(now), actual + 2)
                self.demand = target
                self._request(target, actual, now, context, purpose='protect', profile='protect')
                if self._pending():
                    policy['operation']['high_wind'] = self.high_wind
                return result('source_fault_protection')
            if self.high_wind.valid_at(now) and isinstance(self.high_wind.proof, Measured80):
                state.release_basis = 'ordinary'
                return None
            return result('ceiling_exit_source_evidence')
        if actual <= 10:
            state.release_basis = 'ordinary'
            return result('ceiling_exit_complete') if operation is not None and operation.get('confirmed_at') == now else None
        rapid = next((key for key, raw in frame['evidence'].items() if raw['temp'] >= raw['hard'] - 2
            and (raw['rapid_rise'] or raw['positive_step_at'] is not None
                and 0 <= now - raw['positive_step_at'] < 6)), None)
        if rapid is not None or any(n.get('thermal_throttle') for n in frame['nodes']):
            decision = result('ceiling_exit_current_rapid_risk' if rapid is not None else 'ceiling_exit_hardware_alarm',
                source=rapid)
            decision['control_status'].update(risk_evidence_expires_mono=view.expires_mono,
                next_condition='fresh_frame_without_current_rapid_risk_or_hardware_alarm')
            return decision
        if self._pending() and operation['purpose'] != 'ceiling_exit':
            return result('ceiling_exit_unresolved_execution')
        blockers = tuple(sorted((key, failure) for key, failure in self._thermal_memory.failures.items()
            if failure['failure_class'] == 'thermal_failure' and actual - 1 <= failure['failed_level']
            and key[0] in nodes and source_key(nodes[key[0]], (key[0], key[3], key[4])) == key[:5]
            and (key[0], key[3], key[4]) in frame['evidence']))
        if blockers:
            identity_known = context is not None and all(n.get('hostname_verified') is True for n in frame['nodes'])
            eligible = identity_known and (not self._pending() or ceiling_op)
            elapsed = min(3, max(0, now - state.last_evidence_at)) if state.last_evidence_at is not None else 0
            continuous = state.last_evidence_at is not None and 0 < now - state.last_evidence_at < 6
            thermal_ready = (eligible and not frame['telemetry_fault'] and not frame['wifi_gap']
                and not frame['thermal_risk'] and bool(frame['evidence'])
                and all(raw['covered'] and not raw['rapid_rise']
                    and max(raw['temp'], raw['guard_projected']) <= raw['target'] - 8
                    for raw in frame['evidence'].values()))
            self._workload_policy(frame, actual, context, thermal_ready,
                advancing, continuous, elapsed, idle_ready=thermal_ready
                    and not frame['power_guard'] and not frame['power_warning'] and not frame.get('node_hold'))
            qualified = self._failure_policy(frame, identity, identity_known, actual, eligible,
                advancing, False, 0, False, ceiling=blockers, ceiling_diagnostic=qualification_diagnostic)
            failure_ready = True
            if not qualified:
                return result('ceiling_exit_requalifying')
        else:
            self._ceiling_window = None
            state.failure_credit, state.failure_qualification = 0, None
        if feedback.get('motor_fault') or feedback.get('motor_attempts', feedback.get('attempts', 0)) >= 2:
            return result('ceiling_exit_motor_budget')
        if not self._pending() and now < next_eligible:
            return result('ceiling_exit_motor_interval')
        if not self._pending() and not advancing:
            return result('ceiling_exit_advancing_endpoint')
        episode = operation['episode'] if self._pending() else _Episode(actual, actual - 1, now, now + 60)
        key, _, _ = view.observations[0]
        self.high_wind = HighWindAuthority(True, (key[0], key[1], key[3]),
            min(feedback['observed_mono'], view.expires_mono - 6), view.maximum_c,
            NoAscent(actual, actual, actual - 1, episode.original_deadline, view.generation, episode))
        self._request(actual - 1, actual, now, context, purpose='ceiling_exit', profile='rules')
        current = policy['operation']
        if 'ceiling_sources' not in current:
            baselines = host_baselines(frame['nodes'])
            current['ceiling_sources'] = tuple(dict(source=key, temp_c=temp,
                baseline=baselines.get(key[0]), attributable=baselines.get(key[0]) is not None)
                for key, temp, stamp in view.observations)
        proof = self.high_wind.proof
        self.high_wind = HighWindAuthority(self.high_wind.allowed, self.high_wind.witness,
            self.high_wind.observed_mono, self.high_wind.actual_c,
            NoAscent(proof.from_level, proof.ceiling, proof.destination, proof.original_deadline,
                proof.generation, policy['operation']['episode']))
        policy['operation'].setdefault('automatic', frame['manual'] is None)
        return result('ceiling_exit_pending')

    def _advance_fixed(self, frame):
        now, evidence, state = frame["now"], frame["evidence"], self.operating
        caller = (frame["minimum"], frame["manual"])
        caller_changed = self.mode is not None and self.mode != caller
        self.mode = caller
        if caller_changed:
            state.observe_credit = 0
            state.evidence_qualified = False
        feedback, policy = frame["feedback"] or {}, self._policy
        healthy = self._feedback_valid(feedback, now)
        actual = feedback["level"] if healthy else state.confirmed_level
        matched = healthy and feedback.get("confirmed") is True and feedback.get("execution") == "matched"
        stamp = feedback.get("observed_mono", -math.inf) if healthy else -math.inf
        advancing = healthy and stamp > state.last_feedback
        context = self._context(frame["nodes"]) if not frame["telemetry_fault"] else None
        identity = tuple(sorted((n["host"], n.get("hostname"), n.get("power_source"),
            tuple(sorted((v["id"], v["group"]) for v in n.get("sensors", [])))) for n in frame["nodes"]))
        identity_known = context is not None and all(n.get("hostname_verified") is True for n in frame["nodes"])
        ceiling = self._ceiling_transition(frame, actual, healthy, advancing, context, identity)
        if ceiling is not None:
            state.last_evidence_at = now
            state.last_feedback = max(state.last_feedback, stamp)
            return ceiling
        if identity_known:
            source_nodes = {n['host']: n for n in frame['nodes']}
            if len(evidence) > MAX_SOURCES or any(self._source_memory_key(source_nodes[key[0]], key) is None for key in evidence):
                self._thermal_memory.saturated = True
            for key in evidence:
                self._record_anchor(key, frame)
        identity_changed = identity_known and state.identity is not None and identity != state.identity
        operation = policy["operation"]
        pending_endpoint = (healthy and self._pending() and operation["purpose"] == "release"
            and operation.get("automatic") is True and not caller_changed
            and actual == state.confirmed_level == operation["episode"].original_from
            and operation["destination"] < actual and now < operation["episode"].original_deadline
            and identity_known and not identity_changed
            and self._extend_context(operation["baseline"], context, strict=False) is not None)
        elapsed = min(3, max(0, now - state.last_evidence_at)) if state.last_evidence_at is not None else 0
        continuous = state.last_evidence_at is not None and 0 < now - state.last_evidence_at <= 6
        if state.last_evidence_at is not None and not continuous:
            state.observe_credit = state.failure_credit = 0
        state.last_evidence_at = now
        state.last_feedback = max(state.last_feedback, stamp)
        self.motion = MotionAuthority()
        exit_feedback = (healthy and isinstance(self.high_wind.proof, NoAscent)
            and self.high_wind.valid_at(now) and self.high_wind.proof.from_level == actual)
        endpoint_qualified = matched or (exit_feedback and actual == state.confirmed_level
            and identity_known and not identity_changed)
        saved_edge = self._thermal_memory.open_edge
        tagged_edge = saved_edge is not None and saved_edge.get('release_basis') == HIGH_EXIT_TAG
        high_exit = False
        high_q = None
        if high_exit:
            arriving = (healthy and self._pending()
                and actual == operation['destination'] and stamp > operation['destination_requested_at'])
            physical_endpoint = healthy and (actual == state.confirmed_level or matched and state.confirmed_level is None or arriving)
            high_q = self._qualify_high_exit(frame, actual, context, healthy, physical_endpoint)
            if high_q.reason in ('high_exit_source', 'high_exit_context', 'high_exit_endpoint') and tagged_edge and not arriving:
                self._quarantine_high_exit(now, identity)
            if actual is not None and actual > 10 and self._empirical.deadline is None:
                self._high_exit_authority(frame, actual, high_q)
            exit_feedback = healthy and physical_endpoint
            endpoint_qualified = matched or exit_feedback
        elif state.release_basis == 'high_exit':
            self._high_exit_window = None
        high_release = (frame['manual'] is None and high_exit and actual is not None
            and actual > 10 and self._empirical.deadline is None)
        if state.release_basis != ('high_exit' if high_release else 'ordinary'):
            state.observe_credit = 0
            state.evidence_qualified = False
            state.release_basis = 'high_exit' if high_release else 'ordinary'
        previous_high_qualified = state.evidence_qualified
        previous_high_credit = state.observe_credit
        operation = policy["operation"]
        restoration = operation.get("high_wind") if operation is not None else None
        if (self._pending() and operation["purpose"] == "rollback"
                and isinstance(restoration, HighWindAuthority)
                and isinstance(restoration.proof, SafeEndpointRestoration)
                and now < restoration.proof.original_deadline
                and healthy and actual == restoration.proof.from_level
                and not frame["telemetry_fault"] and not frame["wifi_gap"] and not frame["uncertain"]
                and bool(evidence) and all(s["covered"] for s in evidence.values())
                and identity_known and identity == operation.get("restore_identity")
                and self._extend_context(operation["baseline"], context) is not None
                and not (self.high_wind.valid_at(now) and isinstance(self.high_wind.proof, MeasuredCritical))):
            observed = _source_observed(frame['nodes'], now)
            key = restoration.witness
            current = next((s for k, s in evidence.items() if k[0] == key[0] and k[1] == key[2]), None)
            if current is not None and observed is not None:
                self.high_wind = HighWindAuthority(True, key, min(stamp, observed), current["temp"], restoration.proof)
                self.motion = operation.get("motion", MotionAuthority())
        self.demand = max(frame["minimum"], frame["manual"] or 0, actual or 0)
        frame["reasons"] = [reason for reason in frame["reasons"]
            if not reason.startswith(("[power]", "[load]"))]
        limit = self.effective_limit(now)
        load = max((max(n.get("cpu_load") if n.get("cpu_load") is not None else 100, n.get("gpu_load") if n.get("gpu_load") is not None else 100) for n in frame["nodes"] if n.get("online")), default=100)
        power = max((n.get("power_w") or 0 for n in frame["nodes"] if n.get("online")), default=0)
        active_load = load >= 30 or power >= 80
        automatic = frame['manual'] is None
        thermal_ready = (healthy and endpoint_qualified and identity_known and not identity_changed
            and (state.confirmed_level is None or actual == state.confirmed_level)
            and not frame['telemetry_fault'] and not frame['wifi_gap'] and not frame['thermal_risk']
            and bool(evidence) and all(s['covered'] and not s['rapid_rise']
                and max(s['temp'], s['guard_projected']) <= s['target'] - 8 for s in evidence.values()))
        prep_ready = thermal_ready and all(not s['step_recent'] for s in evidence.values())
        idle_ready = (thermal_ready and matched and bool(frame['complete']) and all(frame['complete'].values())
            and not frame['power_guard'] and not frame['power_warning'] and not frame.get('node_hold')
            and not feedback.get('readback_hold') and not feedback.get('protection_interrupted')
            and not feedback.get('readback_failure'))
        workload_phase = self._workload_policy(frame, actual, context, prep_ready,
            advancing, continuous, elapsed, idle_ready=idle_ready)
        if high_release:
            state.observe_credit = previous_high_credit
            state.evidence_qualified = previous_high_qualified
        settled_workload = workload_phase == 'stable_loaded' and prep_ready
        power_warning = frame['power_warning'] and not settled_workload
        power_guard = frame['power_guard'] and not settled_workload
        warming = any(s["rise"] > 0 or s["persistent_heat"] for s in evidence.values())
        qualified = (healthy and endpoint_qualified and identity_known and not identity_changed
            and not frame["telemetry_fault"] and not frame["wifi_gap"] and not frame["uncertain"]
            and bool(evidence) and all(s["covered"] for s in evidence.values()))
        ordinary = dict(evidence=evidence, margin=frame["margin"], demand=frame["demand"],
                        deficits=frame["deficits"], uncertain=frame["uncertain"], reset=False)
        pending_release_qualified = False
        if automatic and not high_release:
            admitted = (healthy and (endpoint_qualified or pending_endpoint) and identity_known and not frame["telemetry_fault"]
                and not frame["thermal_risk"] and not frame["wifi_gap"]
                and not power_warning and not power_guard
                and not frame.get("node_hold") and not feedback.get("readback_hold")
                and not feedback.get("protection_interrupted") and not feedback.get("readback_failure")
                and bool(frame["complete"]) and all(frame["complete"].values())
                and bool(evidence) and all(s["temp"] <= s["target"] - 5 for s in evidence.values()))
            ordinary = self._fixed_ordinary(frame, actual, context, identity, admitted)
            pending_release_qualified = admitted and pending_endpoint
            if pending_release_qualified:
                ordinary["release"] = self._fixed_release(frame, evidence, pending=True)
                frame["reasons"].extend(reason for _, reason in sorted(
                    ordinary["release"]["classifications"].values())[:4])
        else:
            self._fixed_epochs.clear()
            self._cycle_proofs.clear()
        ordinary.setdefault('release', dict(evidence=evidence, margin=frame['margin'], uncertain=frame['uncertain']))
        ordinary_evidence = ordinary["evidence"]
        release = ordinary["release"] if automatic else ordinary
        release_evidence, margin = release["evidence"], release["margin"]
        release_warming_veto = (any((s.get("warming", s["rise"] > 0 or s["persistent_heat"]))
            and s["release_margin"] < 8 for s in release_evidence.values())
            if automatic else warming and margin < 8)
        ordinary_base_qualified = (healthy and endpoint_qualified and identity_known and not identity_changed
            and not frame["telemetry_fault"] and not frame["wifi_gap"]
            and (not automatic or not feedback.get("protection_interrupted") and not feedback.get("readback_failure"))
            and bool(evidence) and all(s["covered"] for s in evidence.values()))
        ordinary_qualified = ordinary_base_qualified and not ordinary["uncertain"]
        descent_qualified = ((ordinary_base_qualified or pending_release_qualified)
            and not (caller_changed and self._pending())
            and (not release["uncertain"] or release.get('observation_eligible', False)))
        release_qualified = (descent_qualified and not release_warming_veto and not power_warning
            and not power_guard and margin >= (5 if load <= 15 and power < 80 else 8)
            and workload_phase != 'preparing' and not release['uncertain'])
        credit = elapsed if qualified and state.evidence_qualified and advancing and continuous else 0
        release_credit = elapsed if descent_qualified and state.evidence_qualified and advancing and continuous else 0
        if ordinary["reset"]:
            state.observe_credit = release_credit = 0
        state.evidence_qualified = qualified
        if high_release:
            release_qualified = high_q.qualified and high_q.complete and not caller_changed
            descent_qualified = high_q.qualified and not caller_changed
            release_credit = elapsed if (descent_qualified and previous_high_qualified
                and advancing and continuous and not self._pending()) else 0
            state.evidence_qualified = descent_qualified

        def transition(outcome, cause, witness=None):
            state.last_transition = dict(outcome=outcome, at=now, cause=cause, witness=witness)
            if self.trial is not None and outcome in ("failed", "invalidated"):
                previous = self.trial.get("outcome")
                self._finish_trial(outcome)
                if self.trial.get("outcome") == outcome and (previous != outcome or not self.trial.get("transition")):
                    self.trial["transition"] = dict(state.last_transition)

        def result(tag, detail):
            self.phase = state.phase
            reason = release.get('reason')
            blocking = release.get('blocking_source')
            phase, condition = 'observing', 'complete_safe_observation'
            if self.motion.source is not None:
                phase, reason, condition = 'critical', 'measured_critical', 'fresh_safe_temperature'
            elif self.motion.restore_level is not None:
                phase, reason, condition = 'recovering', 'attributed_descent_failure', 'safe_endpoint_readback'
            elif self._thermal_memory.saturated or self._restored_edge:
                phase, reason, condition = 'hold', ('memory_capacity' if self._thermal_memory.saturated
                    else 'unresolved_descent'), 'verify_restrictive_memory'
            elif state.failed_floor is not None:
                phase, reason, condition = 'hold', ('unresolved_descent' if state.failed_floor.get('failure_class') == 'unresolved_descent'
                    else 'failed_descent_floor'), 'lower_same_host_workload_60s'
                blocking = state.failed_floor['witness']['source']
            elif reason == 'warming_budget_exhausted':
                phase, condition = 'hold', 'safe_comparable_plateau_180s'
            elif workload_phase == 'preparing':
                phase, reason, condition = 'preparing', 'new_workload', 'comparable_safe_workload_60s'
            elif self._pending():
                phase, reason, condition = 'waiting_execution', 'pending_intent', 'persisted_snapshot_and_readback'
            elif not descent_qualified:
                phase, reason, condition = 'hold', reason or 'incomplete_safe_evidence', 'fresh_complete_safe_evidence'
            elif margin < (5 if load <= 15 and power < 80 else 8):
                phase, reason, condition = 'hold', 'insufficient_margin', 'safe_projected_margin'
            elif automatic and not release.get('can_execute', False):
                phase, condition = 'observing', 'same_source_ready'
            self._control_status = dict(phase=phase, primary_reason=reason or 'safe_observation',
                blocking_source=[str(v)[:160] for v in blocking] if blocking is not None else None,
                margin_c=margin if _finite(margin) else None,
                observe_seconds=state.observe_credit, required_seconds=OBSERVE_SECONDS,
                next_condition=condition, workload_phase=workload_phase)
            if high_exit and (high_release or self.motion.restore_level is not None or self.motion.source is not None):
                high_reason = high_q.reason
                high_phase = 'observing' if high_q.qualified or high_reason == 'high_exit_warmup' else 'hold'
                condition = ('high_exit_complete_trend_60s' if high_reason == 'high_exit_warmup' else
                    'high_exit_comparable_context' if high_reason == 'high_exit_context' else
                    'high_exit_fresh_endpoint' if high_reason in ('high_exit_source', 'high_exit_endpoint') else
                    'high_exit_complete_observation_180s' if high_q.qualified else 'high_exit_safe_critical_margin')
                blocking = high_q.source
                if self.motion.source is not None:
                    high_phase, high_reason, condition = 'critical', 'measured_critical', 'fresh_safe_temperature'
                    blocking = self.motion.source
                elif self.motion.restore_level is not None:
                    high_phase, high_reason, condition = 'recovering', (
                        'high_exit_forecast_restoration' if tag == 'forecast_recovery' else 'high_exit_restoration'), 'safe_endpoint_readback'
                    blocking = None
                elif (self._thermal_memory.saturated or self._restored_edge
                        or saved_edge is not None and saved_edge.get('stage') == 'unresolved' and self._thermal_memory.failures):
                    high_phase, high_reason, condition = 'hold', ('memory_capacity' if self._thermal_memory.saturated
                        else 'unresolved_descent'), 'verify_restrictive_memory'
                    blocking = None
                elif policy['operation'] is not None and policy['operation'].get('outcome') == 'execution_timeout':
                    high_phase, high_reason, condition = 'hold', 'high_exit_timeout', 'high_exit_fresh_endpoint'
                    blocking = None
                elif state.failed_floor is not None:
                    high_phase, high_reason, condition = 'hold', ('unresolved_descent'
                        if state.failed_floor.get('failure_class') == 'unresolved_descent' else 'failed_descent_floor'), 'lower_same_host_workload_60s'
                    blocking = None
                elif self._pending():
                    high_phase, high_reason, condition = 'waiting_execution', 'pending_intent', 'persisted_snapshot_and_readback'
                    blocking = None
                elif high_q.qualified and state.observe_credit >= OBSERVE_SECONDS and high_q.complete:
                    high_reason = 'high_exit_ready'
                self._control_status.update(phase=high_phase, primary_reason=high_reason,
                    blocking_source=list(blocking) if blocking else None, next_condition=condition,
                    release_basis='high_exit', margin_reference='critical75', margin_c=high_q.margin,
                    measured_margin_c=high_q.measured_margin, trend_rate_c_per_second=high_q.trend,
                    chain_warming_c=high_q.warming, warming_budget_c=WARMING_BUDGET_C,
                    ordinary_reason=('warming_budget_exhausted' if any(a['peak_c'] - a['anchor_c'] > WARMING_BUDGET_C
                        for a in self._thermal_memory.anchors.values()) else 'new_workload' if workload_phase == 'preparing' else release.get('reason')))
            return self._decision(tag, detail, frame["reasons"], frame["slopes"], frame["complete"], False)

        def request(target, purpose="reserve", step=1):
            previous_operation = policy["operation"]
            if purpose == "release" and actual > 10:
                deadline = (previous_operation["episode"].original_deadline
                    if self._pending() else now + 60)
                if high_release:
                    self._high_exit_authority(frame, actual, high_q, destination=target, deadline=deadline)
                else:
                    self._safe_exit_authority(frame, actual, destination=target, deadline=deadline)
            self._request(target, feedback["level"] if healthy else None, now, context,
                purpose=purpose, profile="rules" if purpose == "release" else "ordinary", descent_limit=step)
            if self._pending():
                policy["operation"]["motion"] = self.motion
                if policy["operation"] is not previous_operation:
                    policy["operation"]["automatic"] = frame["manual"] is None
                    if target < actual:
                        if high_release:
                            policy['operation']['release_basis'] = HIGH_EXIT_TAG
                            policy['operation']['exit_identity'] = identity
                        self._remember_descent(policy['operation'], frame)
            state.observe_credit = 0
            state.last_evidence_at = None
            state.evidence_qualified = False
            state.phase = "descending" if actual is not None and target < actual else "ascending"

        def confirm(operation):
            operation.update(episode=None, confirmed_at=now, status='confirmed', elapsed=0)
            self._remember_intent(operation, state='confirmed')
            previous = state.confirmed_level
            state.confirmed_level = actual
            if actual != previous:
                self._fixed_epochs.clear()
                self._cycle_proofs.clear()
            state.observe_credit = 0
            state.last_evidence_at = now
            state.evidence_qualified = False if operation.get('release_basis') == HIGH_EXIT_TAG else release_qualified
            state.settle_required = 120 if operation['purpose'] == 'rollback' else 60
            state.phase = 'settling'
            self.level = actual
            if actual > previous:
                state.last_increase = now
            if actual < previous and operation['purpose'] == 'release' and operation['attribution'] == 'comparable':
                self.last_drop = now
                policy['last_downward'] = dict(edge=(previous, actual), reference=operation['temperatures'],
                    identity=operation.get('exit_identity', identity), context=(operation['baseline']
                        if operation.get('release_basis') == HIGH_EXIT_TAG else context),
                    baselines={host: [pair[0] for pair in entry[2]] for host, entry in operation['baseline'].items()},
                    automatic=operation.get('automatic', True), release_basis=operation.get('release_basis'))
                if self._thermal_memory.open_edge is not None and self._thermal_memory.open_edge['stage'] != 'unresolved':
                    self._thermal_memory.open_edge['stage'] = 'confirmed'
                self._finish_trial('recovery_released')

        hold = feedback.get("readback_hold")
        if not (isinstance(hold, dict) and type(hold.get("until")) in (int, float)
                and now < hold["until"] and not feedback.get("fault") and not feedback.get("protection_interrupted")):
            hold = None
        node_hold = frame.get("node_hold")
        if node_hold and (hold is None or node_hold["level"] > hold["level"]):
            hold = node_hold
        emergency = next(((key, s) for key, s in evidence.items() if s["temp"] >= s["hard"]), None)
        ordinary_enhancement = self.high_wind.valid_at(now) and isinstance(self.high_wind.proof, Measured80)
        if emergency and not ordinary_enhancement:
            key, sensor = emergency
            self.motion = MotionAuthority(now, key, sensor["temp"], sensor["hard"])
        operation = policy["operation"]
        if self._pending() and now >= operation["episode"].original_deadline:
            transition("invalidated", "执行超过原始60秒截止")
            policy["startup_required"] = True
            self._retire_execution("execution_timeout", now, cause="执行超过原始60秒截止")
            self.demand = limit
            self.level = limit if self.motion.source is not None or actual is None else max(actual, min(limit, actual + 2))
            state.phase = "hold"
            state.settle_required = max(60, state.settle_required)
            return result("protect", "execution_timeout 原始60秒执行截止到期，保护风力请求，等待真实读回")
        if (self._pending() and (operation.get('release_basis') == HIGH_EXIT_TAG
                or operation['purpose'] == 'rollback' and isinstance(operation.get('high_wind'), HighWindAuthority)
                and isinstance(operation['high_wind'].proof, SafeEndpointRestoration))
                and healthy and advancing and actual == operation['destination']
                and stamp > operation['destination_requested_at']):
            confirm(operation)
            credit = release_credit = 0
        incompatible = (operation is not None and operation["destination"] > 10
            and isinstance(self.high_wind.proof, NoAscent) and operation["purpose"] != "release")
        if self._pending() and (operation["destination"] > limit or incompatible):
            if actual is not None and actual <= 10:
                if healthy and actual == limit:
                    self._retire_execution("invalidated", now, cause="高风许可撤销，当前物理档位已满足上限")
                    self.level = actual
                else:
                    self._request(limit, feedback["level"] if healthy else None, now, context,
                        purpose="protect", profile="protect")
            else:
                self._retire_execution("invalidated", now, cause="高风许可撤销，停止原执行并保持真实档位")
                self.level = actual if actual is not None else limit
            state.observe_credit = 0
            state.last_evidence_at = None
            state.evidence_qualified = False
            credit = release_credit = 0
        if (policy["startup_required"] and operation is not None
                and operation.get("outcome") == "execution_timeout"):
            self.demand = limit
            self.level = limit if self.motion.source is not None else max(actual or 0, min(limit, self.level))
            timed_out_at = operation["transition"]["at"]
            state.observe_credit = state.failure_credit = 0
            state.last_evidence_at = None
            state.evidence_qualified = False
            state.phase = "hold"
            if (healthy and matched and actual == self.level and stamp > timed_out_at
                    and self.feedback_matches_request(self.level, stamp, now)):
                policy["startup_required"] = False
                state.confirmed_level = actual
                state.phase = "settling"
            return result("protect", "execution_timeout 等待超时之后的当前保护档位真实匹配读回")
        if not qualified and (not healthy or not (matched or exit_feedback or pending_release_qualified) or not identity_known or identity_changed
                             or frame["telemetry_fault"] or frame["wifi_gap"]):
            state.observe_credit = state.failure_credit = 0
            state.settle_required = max(state.settle_required, 60)
            if self.trial is not None and self.trial.get("outcome") not in ("failed", "invalidated"):
                transition("invalidated", "反馈、通信或来源证据中断", dict(source=identity))
            if not (high_exit and high_q.reason not in ('high_exit_source', 'high_exit_context', 'high_exit_endpoint')):
                policy["last_downward"] = None
        if identity_changed:
            state.failure_credit = 0
            state.failure_qualification = None
        if identity_known:
            state.identity = identity
        if (self._pending() and policy["operation"]["purpose"] == "release"
                and actual is not None and policy["operation"]["destination"] < actual
                and (frame["telemetry_fault"] or frame["wifi_gap"] or not healthy)):
            transition("invalidated", "下降期间反馈或来源证据中断")
            self._retire_execution("invalidated", now, cause="下降期间反馈或来源证据中断")
        if actual is None or frame["telemetry_fault"] or not healthy and hold is None:
            self.demand = limit
            state.phase = "hold"
            if actual is not None:
                if actual > limit and self.motion.source is None:
                    self.level = actual
                else:
                    target = limit if self.motion.source is not None else min(limit,
                        10 if ordinary_enhancement and actual < 10 else
                        actual + (1 if ordinary_enhancement else 2))
                    if ordinary_enhancement:
                        self.demand = target
                    request(target)
            else:
                self.level = limit
            return result("protect", "保护风力请求，等待当前目的档位匹配读回及当前安全证据；实际未知时禁止盲写")
        policy["startup_required"] = False
        if hold is not None and not frame["thermal_risk"] and self.motion.source is None:
            state.observe_credit = 0
            state.last_evidence_at = None
            state.evidence_qualified = False
            state.phase = "hold"
            self.level = max(actual or 0, min(limit, hold["level"]))
            return result("communication_hold", "通信中断，保留有界安全档；实际反馈仍按未知报告")

        if state.confirmed_level is None:
            state.confirmed_level = actual
            self.level = actual
        operation = policy["operation"]
        if self._pending():
            if (advancing and (matched or exit_feedback and operation["purpose"] == "release") and actual == operation["destination"]
                    and stamp > operation["destination_requested_at"]):
                confirm(operation)
                if self.motion.source is None and not frame["thermal_risk"]:
                    return result("recovery_released", "目的档位正转速已确认，开始热响应观察")
            else:
                if not frame["thermal_risk"] and not emergency:
                    if operation.get('release_basis') == HIGH_EXIT_TAG:
                        if (not automatic and operation.get('automatic') is True
                                and high_q is not None and high_q.qualified and high_q.complete
                                and not any(s['positive_step_at'] is not None
                                    and s['positive_step_at'] > operation['destination_requested_at'] for s in evidence.values())):
                            self.level = actual
                            return result('trial_pending', '手动选择已撤销下降许可，仅等待原始截止前的已发请求真实读回')
                        if not release_qualified or caller_changed or any(s['positive_step_at'] is not None
                                and s['positive_step_at'] > operation['destination_requested_at'] for s in evidence.values()):
                            transition('invalidated', '高档退出资格中断')
                            self._retire_execution('invalidated', now, cause='高档退出资格中断')
                            self.level = actual
                        else:
                            self._high_exit_authority(frame, actual, high_q, destination=operation['destination'],
                                deadline=operation['episode'].original_deadline)
                            self.level = operation['destination']
                            return result('trial_pending', '等待目的档位正转速读回，保持原始执行截止')
                    elif operation["purpose"] == "release" and operation["destination"] < actual and (not release_qualified
                            or automatic and any(s["positive_step_at"] is not None
                                and s["positive_step_at"] > operation["destination_requested_at"] for s in evidence.values())
                            or margin < (5 if load <= 15 and power < 80 else 8)
                            or power_warning or power_guard or workload_phase == 'preparing'):
                        transition("invalidated", "下降期间温度或负载资格改变")
                        self._retire_execution("invalidated", now, cause="下降期间温度或负载资格改变")
                        self.level = actual
                        state.settle_required = 60
                    else:
                        if self.motion.source is None:
                            self.motion = operation.get("motion", MotionAuthority())
                        if operation["purpose"] == "release" and isinstance(self.high_wind.proof, NoAscent):
                            self._safe_exit_authority(frame, actual, destination=operation["destination"],
                                deadline=operation["episode"].original_deadline)
                        return result("trial_pending", "等待目的档位正转速读回，保持原始执行截止")
        if actual != state.confirmed_level:
            transition("invalidated", "实际档位出现未请求变化，原下降边不可归因", dict(actual_level=actual))
            policy["last_downward"] = None
            state.confirmed_level = actual
            state.observe_credit = 0
            state.last_evidence_at = None
            state.evidence_qualified = False
            credit = release_credit = 0
            state.settle_required = max(60, state.settle_required)
            state.phase = "settling"
            self.level = actual

        self._reconcile_restored_edge(frame, actual, identity, context, qualified and matched and advancing)
        edge = policy["last_downward"]
        baselines = host_baselines(frame['nodes'])
        high_edge = edge is not None and edge.get('release_basis') == HIGH_EXIT_TAG
        if high_edge and (edge['identity'] != identity
                or self._extend_context(edge['context'], context, anchored_power=True) is None
                or high_q is None or high_q.reason in ('high_exit_source', 'high_exit_context', 'high_exit_endpoint')):
            self._quarantine_high_exit(now, identity)
            policy['last_downward'] = edge = None
        ceiling_edge = edge is not None and edge.get('purpose') == 'ceiling_exit'
        if edge is not None and not high_edge and not ceiling_edge:
            confounded = edge.setdefault('confounded_hosts', set())
            confounded.update(host for host, baseline in edge['baselines'].items()
                if increased(baseline, baselines.get(host)))
            if self._thermal_memory.open_edge is not None:
                for row in self._thermal_memory.open_edge['sources']:
                    if row['source'][0] in confounded:
                        row['attributable'] = False
            if confounded == set(edge['baselines']):
                transition('invalidated', '下降后各机器负载或功率改变，原下降边不可归因')
                policy['last_downward'] = edge = None
                self._thermal_memory.open_edge = None
                state.observe_credit = 0
        witnesses = [(key, sensor) for key, sensor in evidence.items() if sensor["covered"] and
            (sensor["temp"] >= sensor["target"] - 2 or sensor["persistent_heat"]
             and sensor["temp"] > sensor["target"] - 5 and edge is not None
             and sensor["temp"] - edge["reference"].get((key[0], key[1]), sensor["temp"]) > 2)]
        high_forecast = False
        if high_edge and edge is not None:
            if actual == edge['edge'][1] and healthy and not frame['thermal_risk'] and self.motion.source is None:
                witnesses, high_forecast = self._high_exit_response(edge, frame)
            else:
                witnesses = []
        elif (edge is not None and not ceiling_edge and actual == edge["edge"][1] and edge["identity"] == identity
                and qualified and witnesses):
            witnesses = [(key, sensor) for key, sensor in witnesses
                if key[0] not in edge.get('confounded_hosts', ())]
        else:
            witnesses = []
        if witnesses or high_forecast:
            key, sensor = witnesses[0] if witnesses else min(evidence.items(), key=lambda row: 75 - row[1]['guard_projected'])
            witness = dict(source=key, actual_c=sensor["temp"], projected_c=sensor["projected"], target_c=sensor["target"])
            if witnesses:
                transition("failed", "试降后同源温度达到恢复条件", witness)
            for failed_key, failed_sensor in witnesses:
                node = next(n for n in frame['nodes'] if n['host'] == failed_key[0])
                base = edge['baselines'][failed_key[0]]
                failure = dict(safe_level=edge['edge'][0], failed_level=actual, load=max(base[:2]), power=base[2],
                    baseline=list(base), failed_at=now, identity=identity,
                    witness=dict(source=failed_key, actual_c=failed_sensor['temp'],
                        projected_c=failed_sensor['projected'], target_c=failed_sensor['target']),
                    automatic=edge.get('automatic', True), failure_class='thermal_failure')
                memory_key = self._source_memory_key(node, failed_key)
                if memory_key is not None:
                    self._thermal_memory.add_failure(memory_key, failure)
            state.failed_floor = max(self._thermal_memory.failures.values(), key=lambda f: f['failed_level'], default=None)
            if not high_edge:
                self._thermal_memory.open_edge = None
            if self.motion.source is None:
                self.motion = MotionAuthority(restore_level=edge["edge"][0], restore_from=actual, deadline=now + 60)
                if edge["edge"][0] > 10:
                    node = next(n for n in frame["nodes"] if n["host"] == key[0])
                    observed = _source_observed(frame['nodes'], now)
                    if observed is not None:
                        self.high_wind = HighWindAuthority(True, (key[0], node["hostname"], key[1]),
                            min(stamp, observed), sensor["temp"], SafeEndpointRestoration(actual, edge["edge"][0], self.motion.deadline))
                        limit = self.effective_limit(now)
            self.demand = limit if self.motion.source is not None else min(limit, edge["edge"][0])
            request(self.demand, "rollback")
            policy["operation"]["high_wind"] = self.high_wind
            policy["operation"]["restore_identity"] = identity
            policy["last_downward"] = None
            if self.motion.source is not None:
                return result("protect", "物理档位已确认，但当前危急温度优先请求已授权保护风力")
            return result('forecast_recovery' if high_forecast and not witnesses else 'recovery',
                '预测危险余量不足，预防性恢复先前确认端点' if high_forecast and not witnesses else '试降升温，恢复该下降边先前确认的安全档')
        forecast_concern = (not high_edge and not ceiling_edge and edge is not None and actual == edge["edge"][1] and edge["identity"] == identity
            and ordinary_qualified and any(s["covered"] and (s["projected"] >= s["target"] - 2
                or s["persistent_heat"] and s["projected"] > s["target"] - 5
                and s["temp"] - edge["reference"].get((key[0], key[1]), s["temp"]) > 2)
                for key, s in ordinary_evidence.items()))
        if forecast_concern:
            state.observe_credit = 0
            state.last_evidence_at = None
            state.evidence_qualified = False
            credit = release_credit = 0
            frame["reasons"].append("[forecast] 预测余量不足，暂停下降并重建180秒观察；尚无实测失败证据，保留原下降边")
        if self._failure_policy(frame, identity, identity_known, actual, qualified,
                advancing, continuous, elapsed, warming):
            state.observe_credit = 0
            state.last_evidence_at = None
            state.evidence_qualified = False
            credit = release_credit = 0
        idle = load <= 15 and power < 80
        floor = max(frame["minimum"], frame["manual"] or 0)
        thermal = frame["thermal_risk"] or bool(ordinary["deficits"]) or any(
            s["rise"] > 0 and s["projected"] >= s["target"] - 2 for s in ordinary_evidence.values())
        target = max(floor, min(limit, ordinary["demand"]) if thermal else 0,
                     min(limit, state.workload_epoch[1] + 2) if state.workload_epoch else 0)
        if isinstance(self.high_wind.proof, TenLevelNonRecovery) and self.high_wind.valid_at(now):
            target = max(target, self.high_wind.ceiling_at(now))
        if self.motion.source is not None:
            target = limit
        if ordinary_enhancement:
            target = min(limit, 10 if actual < 10 else actual + 1)
        self.demand = target
        if high_release and not frame['thermal_risk'] and self.motion.source is None and not (
                self.high_wind.valid_at(now) and isinstance(self.high_wind.proof, TenLevelNonRecovery)):
            self.demand = max(frame['minimum'], 10 if active_load else 0)
            state.evidence_qualified = descent_qualified
            state.observe_credit = min(OBSERVE_SECONDS,
                state.observe_credit + release_credit) if descent_qualified and not self._pending() else 0
            destination = max(10, frame['minimum'], actual - 1)
            if state.failed_floor:
                destination = max(destination, state.failed_floor['failed_level'] + 1)
            restricted = (self._thermal_memory.saturated or self._restored_edge
                or saved_edge is not None and saved_edge.get('stage') == 'unresolved' and self._thermal_memory.failures
                or bool(self._thermal_memory.failures) and state.failed_floor is None)
            if (descent_qualified and high_q.complete and state.observe_credit >= OBSERVE_SECONDS
                    and advancing and now - self.last_drop >= 30 and destination == actual - 1
                    and not restricted and not caller_changed):
                request(destination, 'release')
                return result('trial_pending', '高档退出完整同源观察完成，申请相邻下降并等待真实确认')
            self.level = actual
            state.phase = 'hold'
            return result('rules_release', '高档退出等待完整同源趋势、危急余量与新端点观察')
        if state.workload_epoch is not None:
            frame["reasons"].append(f"[workload] 周期起点{state.workload_epoch[1]}档，预备需求最多加两档；可比安全60秒结束预备")
        if target > actual:
            state.observe_credit = 0
            state.evidence_qualified = False
            allowed = self.motion.source is not None or now - state.last_increase >= 10
            if allowed and healthy and (matched or self.motion.source is not None):
                request(target if self.motion.source is not None else min(target, actual + (1 if actual >= 10 else 2)))
                return result("graded_pending", "当前需求分步上调，每次最多两档，确认后继续观察")
            self.level = actual
            return result("graded_hold", "上调间隔或匹配读回尚未满足")
        eligible = descent_qualified and not forecast_concern and not release_warming_veto and not power_warning and not power_guard
        eligible = eligible and margin >= (5 if idle else 8)
        if eligible:
            state.observe_credit += release_credit
        else:
            state.observe_credit = 0
        state.evidence_qualified = eligible or (qualified and idle and state.workload_epoch is not None)
        required = max(state.settle_required, OBSERVE_SECONDS)
        step = 1
        if frame["manual"] is not None:
            required = max(state.settle_required, 30 if idle and frame["margin"] >= 8 else 180)
        state.observe_credit = min(state.observe_credit, required)
        destination = max(floor, actual - step)
        if state.failed_floor:
            destination = max(destination, state.failed_floor["failed_level"] + 1)
        step_hold = ((automatic and not release.get("can_execute", False)) or workload_phase == 'preparing'
            or self._thermal_memory.saturated or self._restored_edge)
        if eligible and step_hold and destination < actual:
            frame["reasons"].append("[release-wait] 当前下降执行条件未全部满足，保持已确认档位；详见来源判定和运行状态")
        if (eligible and state.observe_credit >= required and now - self.last_drop >= 30 and destination < actual
                and not step_hold):
            request(destination, "release", actual - destination)
            return result("trial_pending", "连续安全观察完成，申请下降并等待真实确认")
        self.level = actual
        state.phase = "settling" if state.observe_credit < state.settle_required else "hold"
        return result("rules_release", "预测余量不足，保持已确认档位，等待重新取得完整下降观察" if forecast_concern else
            f"固定规则观察 {state.observe_credit:g}/{required:g}s，需求{self.demand}，下一档{actual}")

    @staticmethod
    def _rules_warming_veto(frame):
        sustained = frame["warming"] or frame["persistent_heat"] or any(
            sensor["slope"] >= .02 for sensor in frame["evidence"].values())
        return frame["margin"] < 5 and sustained

    def _advance_rules(self, frame, *, reserve, valid, matched, advancing, changed, continuity_lost):
        now, actual = frame["now"], frame["actual"]
        policy, evidence = self._policy, frame["evidence"]
        def result(tag, detail):
            self.phase = "hold"
            policy["learning_window"].clear()
            policy["learning_key"] = None
            return self._decision(tag, detail, frame["reasons"], frame["slopes"], frame["complete"], False)

        edge = policy["last_downward"]
        comparable = (edge is not None and edge["edge"][1] == actual
            and edge["identity"] == frame["identity"] and edge["caller"] == frame["caller"]
            and self._extend_context(edge["context"], frame["context"], strict=False) is not None)
        danger = any(s["covered"] and s["projected"] >= s["target"] - 2
                     for s in evidence.values())
        risen = (comparable and any(
            s["covered"] and s["persistent_heat"] and s["projected"] > s["target"] - 5
            and s["temp"] - edge["reference"].get((key[0], key[1]), s["temp"]) > 2
            for key, s in evidence.items()))
        if comparable and (danger or risen):
            self._finish_trial("failed")
            self._request(max(actual, edge["edge"][0], reserve), actual, now, frame["context"],
                          purpose="rollback", profile="ordinary")
            witnesses = []
            for key, sensor in evidence.items():
                primary = sensor["covered"] and sensor["projected"] >= sensor["target"] - 2
                secondary = (sensor["covered"] and sensor["persistent_heat"]
                    and sensor["projected"] > sensor["target"] - 5
                    and sensor["temp"] - edge["reference"].get((key[0], key[1]), sensor["temp"]) > 2)
                if primary or secondary:
                    witnesses.append(f"{key[0]} {key[1]} actual={sensor['temp']:g}C "
                        f"accepted_projection={sensor['projected']:.3f}C target={sensor['target']:g}C "
                        f"rule={'primary(target-2)' if primary else 'secondary(own<5+rise>2)'}")
            return result("recovery", "接近目标，恢复先前实际确认的安全风力；" + "; ".join(witnesses))
        if actual != self.level:
            self._request(max(actual, self.level, reserve), actual, now, frame["context"],
                          purpose="reserve", profile="ordinary")
            policy["state"] = dict(tag="hold")
            return result("recovery", "实际档位偏离先前确认请求，停止下降并恢复至少先前安全风力")
        if reserve > actual:
            self._request(reserve, actual, now, frame["context"], purpose="reserve", profile="ordinary")
            return result("graded_pending", f"当前真实保底要求{reserve}档，等待匹配读回")
        if frame["deficits"]:
            witness = policy["state"].get("witness")
            witness = witness if witness in frame["deficits"] else min(frame["deficits"])
            state = self._credit("graded", frame, valid, witness=witness)
            if state["earned"] >= 30 and valid and actual < MAX_LEVEL:
                self._request(actual + 1, actual, now, frame["context"], purpose="graded", profile="ordinary")
            return result("graded_pending" if self._pending() else "graded_hold",
                          f"同一传感器持续不足 {state['earned']:g}/30s，实际{actual}档")
        reset = (changed or continuity_lost or not valid or actual != self.level)
        if reset:
            policy["state"] = dict(tag="rules_release")
        warming_veto = self._rules_warming_veto(frame)
        eligible = (valid and matched and advancing and not reset and bool(evidence)
            and all(s["covered"] for s in evidence.values()) and self._workload_mean(now, 30) is not None
            and not frame["power_warning"] and not frame["power_guard"] and not frame["load_event"]
            and not frame["target_risk"] and not frame["uncertain"] and not warming_veto)
        margin = frame["margin"]
        required, step = (30, 3) if margin >= 10 else (30, 2) if margin >= 5 else (60, 1)
        if frame["manual"] is not None:
            step = 1
        eligible = eligible and margin >= 3 and actual > reserve
        state = self._credit("rules_release", frame, eligible)
        detail = (f"目标余量 {margin:.1f} C，安全观察 {state['earned']:g}/{required}s，"
                  f"实际{actual}档，保底{reserve}档，降档最小间隔30s")
        if state["release_reset"]:
            detail += "，" + state["release_reset"]
        pauses = []
        for active, reason in ((not valid, "温度或匹配读回证据不足"),
                (frame["power_warning"], "即时功耗警告"), (frame["power_guard"], "持续功耗保护"),
                (frame["load_event"], "即时负载事件"), (frame["uncertain"], "温度变化待确认"),
                (warming_veto, "持续升温且预测余量不足5C"), (frame["target_risk"], "目标温度风险"),
                (margin < 3, "目标余量低于3C"), (actual <= reserve, "已达当前真实保底")):
            if active:
                pauses.append(reason)
        if pauses:
            detail += "，暂停原因：" + "、".join(pauses)
        if eligible and state["earned"] >= required and now - self.last_drop >= 30:
            destination = max(reserve, actual - step)
            self._request(destination, actual, now, frame["context"], purpose="release", profile="rules",
                          reference=dict(state["workload_reference"]), descent_limit=actual - destination)
            policy["operation"]["release_identity"] = frame["identity"]
            return result("trial_pending", detail + f"，低温跳档请求 {actual}->{destination}，等待设备读回")
        self.level = actual
        return result("rules_release", detail + ("，暂停累计" if not eligible or state["release_reset"] else "，继续观察"))

    def _advance(self, frame):
        now, nodes = frame["now"], frame["nodes"]
        policy, reasons = self._policy, frame["reasons"]
        context = self._context(nodes) if not frame["telemetry_fault"] else None
        if context is None:
            self.power_exposure.clear()
        caller = (frame["minimum"], frame["manual"])
        changed = self.mode is not None and self.mode != caller
        self.mode = caller
        healthy = self._feedback_valid(frame["feedback"], now)
        hold = frame["feedback"].get("readback_hold") if isinstance(frame["feedback"], dict) else None
        hold = (hold if isinstance(hold, dict) and type(hold.get("level")) is int
                and 0 <= hold["level"] <= MAX_LEVEL and type(hold.get("until")) in (int, float)
                and math.isfinite(hold["until"]) and now < hold["until"]
                and frame["feedback"].get("fault") is False
                and not frame["feedback"].get("protection_interrupted")
                and not policy["startup_required"] else None)
        if not healthy:
            self.power_exposure.clear()
        actual = frame["feedback"]["level"] if healthy else self.level
        stamp = frame["feedback"]["observed_mono"] if healthy else -math.inf
        advancing = healthy and stamp > self.last_feedback
        matched = healthy and frame["feedback"]["execution"] == "matched" and frame["feedback"]["confirmed"] is True
        self.last_feedback = max(self.last_feedback, stamp)
        identity = tuple(sorted((node["host"], node.get("hostname"),
                                 tuple(sorted((key, s["group"]) for key, s in
                                              self.expected.get(node["host"], {}).items()))) for node in nodes))
        frame.update(context=context, caller=caller, identity=identity, actual=actual)
        limit = self.effective_limit(now)
        self.level = min(self.level, limit)
        if self._pending() and policy["operation"]["destination"] > limit:
            self._request(limit, actual, now, context, purpose="protect", profile="protect")
        valid = (advancing and matched and context is not None and bool(frame["complete"])
                 and all(frame["complete"].values()) and not frame["wifi_gap"] and not frame["uncertain"])
        floor = max(frame["minimum"], frame["manual"] or 0)
        temperatures = self._temperatures()
        cpu_gpu = [s for s in frame["evidence"].values() if s["group"] in ("cpu", "gpu")]
        if any(s["temp"] >= 60 for s in cpu_gpu):
            policy["preparation"] = True
        elif cpu_gpu and all(s["temp"] <= 58 for s in cpu_gpu):
            policy["preparation"] = False
        reserve = max(floor, frame["demand"], MIN_LEVEL if policy["preparation"] else 0)
        continuous = self._extend_context(self.workload_context, context, strict=False) is not None
        continuity_lost = self.workload_context is not None and not continuous
        if not continuous:
            self.workload_history.clear()
        if context is not None:
            for host, (_, _, ranges) in context.items():
                points = self.workload_history.setdefault(host, deque(maxlen=64))
                while len(points) > 1 and now - points[1][0] >= 60:
                    points.popleft()
                points.append((now, tuple(low for low, _ in ranges)))
        self.workload_context = context
        means = self._workload_mean(now, 30)
        references = [r for r in (self.search_reference,
                      self.gear_window["reference"] if self.gear_window else None) if r is not None]
        workload_change = bool(references) and means is None
        detail = "workload coverage=insufficient/30s" if means is None else "workload coverage=30/30s"
        if references and means is not None:
            deltas = [(value - reference[host][index], host, index)
                      for reference in references for host, values in means.items()
                      for index, value in enumerate(values)]
            delta, host, index = max(deltas, key=lambda item: abs(item[0]) / (15 if item[2] < 2 else 10))
            workload_change = any(abs(d) >= (15 if i < 2 else 10) for d, _, i in deltas)
            detail += f" fixed_delta={host} {('cpu', 'gpu', 'power')[index]} {delta:.3f}/{15 if index < 2 else 10}"
        reasons.append("[context] " + detail)
        reasons.append(f"[feedback] requested={self.level} actual={actual} matched={matched} "
                       f"advancing={advancing} age={now - stamp:g}/9s")
        trial = self.trial
        retained = trial is not None and trial["outcome"] in (None, "success", "manual_complete", "recovery_released")
        comparable = self._extend_context(trial["baseline"], context, strict=False) if trial else None
        confounded = (changed or not healthy or frame["telemetry_fault"] or continuity_lost
                      or workload_change or comparable is None or frame["power_guard"]
                      or frame["load_event"] == "unverified_load"
                      or trial is not None and trial["confirmed_at"] is not None
                      and (actual != trial["to"] or not matched))
        attributable = (retained and trial["confirmed_at"] is not None and not confounded
                        and not frame["power_warning"])
        if retained and confounded:
            self._finish_trial("invalidated")
        if (changed or continuity_lost or workload_change or frame["power_guard"] or frame["power_warning"] or context is None
                or frame["load_event"] == "unverified_load"):
            self._retire_guards()
        if changed:
            policy["cold_band"] = False
            policy["state"] = dict(tag="hold")
        if context is None:
            reasons.append("[context] 功率或来源未知，冻结下降、准入和学习")

        def result(tag, text, eligible=False):
            window = policy["learning_window"]
            key = (actual, identity, caller)
            strict = (not self.rules_only and eligible and valid and frame["low_load"] and frame["manual"] is None
                      and not changed and not frame["power_guard"] and not frame["power_warning"] and not frame["load_event"]
                      and not frame["warming"] and not frame["persistent_heat"]
                      and actual == self.level and not self._pending() and not policy["cold_band"])
            if not strict or policy["learning_key"] != key:
                window.clear()
            policy["learning_key"] = key if strict else None
            learning = False
            if strict:
                if window and not 0 < now - window[-1][0] <= 6:
                    window.clear()
                while len(window) > 1 and window[1][0] <= now - 120:
                    window.popleft()
                window.append((now, context))
                stable = window[0][1]
                for _, sample in list(window)[1:]:
                    stable = self._extend_context(stable, sample)
                    if stable is None:
                        break
                learning = stable is not None and now - window[0][0] >= 120
            owner = {"graded_hold": "graded", "trial_observe": "probe_observe", "steady_low": "ordinary_search",
                     "trial_pending": "pending", "graded_pending": "pending"}.get(tag, tag)
            if tag not in ("recovery_released", "recovery", "protect", "manual", "thermal_uncertain", "warming_hold", "power_warning"):
                if policy["state"]["tag"] != owner:
                    policy["state"] = dict(tag=owner)
            prefix = "[cold-release] " if (tag == "cold_release" or
                     self.trial is not None and self.trial.get("profile") == "cold"
                     and (self._pending() or tag == "recovery_released")) else ""
            return self._decision(tag, prefix + text, reasons, frame["slopes"], frame["complete"], learning)

        pending = self._pending()
        operation = policy["operation"]
        expired = pending and now >= operation["episode"].original_deadline
        if expired:
            self.power_exposure.clear()
            episode = operation["episode"]
            self._retire_execution("execution_timeout", now, cause="原请求60秒执行截止到期")
            operation["elapsed"] = max(0, now - episode.original_requested_at)
            policy.update(startup_required=True, protect_cause="fault", cold_band=False,
                          state=dict(tag="protect"), rebound=None, last_downward=None)
            self._retire_search()
            self.level, self.phase = self.effective_limit(now), "protect"
            self.history.clear()
            self.positive_since.clear()
            reasons.insert(0, "[feedback] 原请求60s执行截止已到期，晚到确认不能消除超时")
            return result("protect", f"execution_timeout original_deadline={episode.original_deadline:g}，请求保护风力并重建启动证据")

        if frame["telemetry_fault"] or not healthy and hold is None or frame["thermal_risk"]:
            fault = frame["telemetry_fault"] or not healthy
            self._finish_trial("failed" if attributable and frame["thermal_risk"] else "invalidated")
            self._retire_search()
            self._retire_guards()
            self._request(MAX_LEVEL, actual, now, context, purpose="protect", profile="protect")
            policy["state"] = dict(tag="protect")
            policy["cold_band"] = False
            policy["rebound"] = policy["last_downward"] = None
            self.phase = "protect"
            if fault:
                policy["startup_required"], policy["protect_cause"] = True, "fault"
                self.history.clear()
                self.positive_since.clear()
                self.workload_history.clear()
                self.workload_context = None
            else:
                policy["protect_cause"] = "thermal"
                policy["thermal_witnesses"] = {key for key, s in frame["evidence"].items()
                    if s["temp"] >= s["hard"] - 2 or s["rise"] > 0 and s["projected"] >= s["hard"]
                    or s["group"] in ("cpu", "gpu") and s["temp"] >= 70 and s["rise"] > 0}
            if not healthy:
                reasons.insert(0, "[feedback] 净化器实际反馈未知、过期或异常，有效期<=9s，保护风力请求尚不代表执行")
            return result("protect", "保护风力请求，等待当前目的档位匹配读回及当前安全证据")

        node_hold = frame.get("node_hold")
        if node_hold is not None and (hold is None or node_hold["level"] >= hold["level"]):
            hold = node_hold
        if hold is not None:
            self._finish_trial("invalidated")
            self._retire_search()
            self._retire_guards()
            policy.update(state=dict(tag="readback_hold"), rebound=None, last_downward=None)
            policy["learning_window"].clear()
            policy["learning_key"] = None
            safe = max(hold["level"], operation["episode"].original_from if self._pending() else self.level)
            destination = max(self.level, safe, reserve)
            self._request(destination, actual if healthy else safe, now, context, purpose="reserve", profile="ordinary")
            if self._pending():
                policy["operation"]["readback_floor"] = hold["level"]
            self.phase = "hold"
            return result("communication_hold" if hold.get("stage") == "node" else "readback_hold", f"{'读回已恢复，等待两次间隔3秒的匹配观察' if hold.get('recovering') else '设备读回短暂中断'}；"
                          f"通信阶段{hold.get('stage', 'hold')}，请求至少{destination}档，实际以有效读回为准；暂停降档 failures={hold.get('failures')}")

        if self._pending():
            trial = policy["operation"]
            if confounded:
                trial["attribution"] = "confounded"
            episode = trial["episode"]
            trial["elapsed"] = max(0, now - episode.original_requested_at)
            destination = trial["destination"]
            cold_authority_lost = (trial["purpose"] == "release" and trial["profile"] in ("cold", "manual")
                and destination < episode.original_from
                and (not frame["cold"] or not frame["low_load"] or frame["warming"]
                     or frame["persistent_heat"] or frame["uncertain"] or frame["load_event"]))
            rules_authority_lost = (trial["profile"] == "rules" and destination < episode.original_from
                and (confounded or not healthy or not all(frame["complete"].values())
                     or trial.get("release_identity") != identity
                     or self._extend_context(trial["baseline"], context, strict=False) is None
                     or self._workload_increase(trial["reference"], means) is not None
                     or frame["margin"] < (10 if episode.planned_step == 3 else
                                           5 if episode.planned_step == 2 else 3)
                     or frame["power_warning"] or frame["power_guard"]
                     or frame["target_risk"] or frame["uncertain"] or frame["load_event"]
                     or self._rules_warming_veto(frame)))
            if rules_authority_lost:
                workload_reason = self._workload_increase(trial["reference"], means)
                reasons.append("[rules-release] 撤销下降权限：" +
                               (workload_reason or "来源、反馈、温度或即时负载保护条件改变"))
                self._finish_trial("invalidated")
                self._request(max(actual, episode.original_from, reserve), actual, now, context,
                              purpose="rollback", profile="ordinary")
            elif reserve > destination:
                self._request(max(actual, reserve), actual, now, context, purpose="reserve", profile="ordinary")
            elif (cold_authority_lost or frame["target_risk"] and
                    (trial["purpose"] == "probe" or trial["purpose"] == "release"
                     and trial["profile"] in ("ordinary", "cold", "manual"))):
                self._finish_trial("invalidated")
                self._request(max(actual, trial["from"], reserve), actual, now, context,
                              purpose="rollback", profile="ordinary")
                self._retire_search()
            elif trial["purpose"] == "probe" and confounded:
                self._request(max(MIN_LEVEL, actual, trial["from"], reserve), actual, now, context,
                              purpose="rollback", profile="ordinary")
                self._retire_search()
            elif frame["power_warning"] and destination < episode.original_from:
                self._finish_trial("invalidated")
                self._request(max(actual, episode.original_from, reserve), actual, now, context,
                              purpose="rollback", profile="ordinary")
                self._retire_search()
            trial = policy["operation"]
            confirmation = (advancing and matched and actual == trial["destination"]
                            and stamp > trial["destination_requested_at"] and not frame["wifi_gap"])
            downward = trial["destination"] < episode.original_from
            if confirmation:
                trial["episode"] = None
                trial["confirmed_at"] = now
                if trial is not self.trial:
                    trial["status"] = "confirmed"
                trial["elapsed"] = trial["observation_elapsed"] = 0
                self.level = actual
                policy["state"] = dict(tag="hold")
                if downward:
                    policy["last_downward"] = dict(edge=(episode.original_from, actual),
                        reference=trial["temperatures"] if trial["profile"] == "rules" else temperatures,
                        context=context, identity=identity, caller=caller, rebound_done=confounded)
                if confounded:
                    policy["cold_band"] = False
                    self._retire_search()
                if trial["purpose"] == "probe" and trial["attribution"] == "comparable" and not confounded:
                    trial.update(status="observing", deadline=now + 300)
                    self.phase = "observe"
                    self.gear_window = dict(level=actual, since=now, context=context, reference=means)
                elif (trial["purpose"] in ("release", "manual") and downward
                      and actual == episode.original_to and trial["attribution"] == "comparable"):
                    outcome = "manual_complete" if trial["profile"] == "manual" and actual < 10 else "recovery_released"
                    self._finish_trial(outcome)
                    self.gear_window = (None if trial["profile"] == "rules" else
                                        dict(level=actual, since=now, context=context, reference=means))
                    self.recovery_since = now
                    self.phase = "hold"
                    self._credit("rules_release" if trial["profile"] == "rules" else
                                 "cold_release" if trial["profile"] == "cold" else "recovery", frame, valid)
                    return result("recovery_released", f"实际确认{trial['from']}->{actual}，恢复观察，不是容量证明")
                else:
                    self.phase = "hold"
                    if trial["purpose"] == "graded" and frame["deficits"]:
                        self._credit("graded", frame, valid, witness=min(frame["deficits"]))
                    return result("recovery", f"当前目的档位{actual}已确认，重新建立当前证据")
            else:
                if trial["purpose"] == "probe" and frame["target_risk"] and not confounded:
                    self._finish_trial("invalidated")
                    self._request(max(MIN_LEVEL, trial["from"], actual, reserve), actual, now, context,
                                  purpose="rollback", profile="ordinary")
                policy["state"] = dict(tag="pending")
                return result("graded_pending" if trial["purpose"] in ("graded", "reserve") else "trial_pending",
                    f"original={episode.original_from}->{episode.original_to} requested={self.level} actual={actual} "
                    f"destination_stamp>{trial['destination_requested_at']:g} elapsed={trial['elapsed']:g}/60s "
                    f"deadline={episode.original_deadline:g}，禁止借用待执行时间")

        if policy["startup_required"]:
            self.level, self.phase = self.effective_limit(now), "protect"
            if actual != self.effective_limit(now):
                if operation is None or operation["outcome"] != "execution_timeout":
                    self._request(MAX_LEVEL, actual, now, context, purpose="protect", profile="protect")
                return result("protect", f"启动或故障恢复，等待实际{self.effective_limit(now)}档确认")
            startup_ok = (valid and frame["quiet"] and not changed and not frame["power_warning"]
                          and frame["load_event"] != "load_spike")
            state = self._credit("startup", frame, startup_ok)
            self.recovery_since = state["since"] if state["last"] is not None else None
            if not startup_ok or state["earned"] < 30:
                return result("thermal_uncertain" if frame["uncertain"] else "warming_hold" if frame["warming"] else "protect",
                              f"启动新基线 earned={state['earned']:g}/30s，完整匹配确认后才允许下降")
            policy["startup_required"], policy["protect_cause"] = False, None
            policy["state"] = dict(tag="hold")
            if not self.rules_only and actual > max(MIN_LEVEL, reserve) and now - self.last_drop >= 30:
                profile = "cold" if frame["cold"] and frame["low_load"] and not frame["warming"] else "ordinary"
                policy["cold_band"] = profile == "cold"
                self._request(actual - 1, actual, now, context, purpose="release", profile=profile)
                return result("trial_pending", f"启动证据30s完成，相邻请求{actual - 1} deadline={now + 60:g}")

        if self.rules_only:
            return self._advance_rules(frame, reserve=reserve, valid=valid, matched=matched,
                                       advancing=advancing, changed=changed, continuity_lost=continuity_lost)

        trial = self.trial
        active_probe = trial is not None and trial["kind"] == "probe" and trial["outcome"] is None
        if retained and confounded:
            self._finish_trial("invalidated")
            was_search = self.search_anchor is not None or trial["kind"] == "probe"
            self._retire_search()
            self._retire_guards()
            if was_search:
                self._request(max(MIN_LEVEL, actual, trial["from"], reserve), actual, now, context,
                              purpose="rollback", profile="ordinary")
                self.phase = "rollback"
                return result("confounded", "来源/身份/档位或固定工作负载失效；" + detail)
        if (active_probe or self.search_anchor is not None or
                attributable and trial["kind"] == "probe") and frame["target_risk"]:
            self._finish_trial("failed" if attributable else "invalidated")
            self._retire_search()
            self._retire_guards()
            self._request(max(MIN_LEVEL, actual, trial["from"], reserve), actual, now, context,
                          purpose="rollback", profile="ordinary")
            self.phase = "rollback"
            return result("rollback", "目标温度风险，回退先前确认档位；仅确认且可归因的试验记为热失败")
        if reserve > actual:
            self._finish_trial("invalidated")
            self._retire_search()
            policy["cold_band"] = False
            self._request(reserve, actual, now, context, purpose="reserve",
                          profile="manual" if floor > actual else "ordinary")
            return result("graded_pending", f"常规需求/60C准备 requested={reserve} actual={actual} deadline={now + 60:g}")
        if active_probe:
            qualification = self._qualification(now, frame["manual"], frame["cold"], frame["margin"])
            trial["elapsed"] = max(0, now - trial["confirmed_at"])
            trial["settling"] = qualification["detail"]
            if now >= trial["deadline"]:
                unresolved = (trial.get("nonsettled") or frame["warming"] or frame["uncertain"]
                              or frame["persistent_heat"] or frame["load_event"] or not advancing
                              or frame["margin"] < 3)
                cause = "missing_samples" if not qualification["covered"] and not unresolved else "nonsettled"
                self._finish_trial("inconclusive_timeout")
                self._retire_search()
                self._request(max(MIN_LEVEL, trial["from"], actual, reserve), actual, now, context,
                              purpose="rollback", profile="ordinary")
                self.phase = "rollback"
                reasons.append(f"[trial] cause={cause}")
                return result("rollback", "probe inconclusive_timeout，300s绝对观察截止，回退并重建当前证据")
            if frame["power_warning"] and reserve <= actual:
                policy["state"]["last"] = None
                if policy["rebound"] is not None:
                    policy["rebound"]["last"] = None
                return result("power_warning", "短时功耗升高，保留已确认观察及绝对截止，暂停下降和学习")
            observation = advancing and matched and context is not None and not frame["wifi_gap"]
            state = self._credit("probe_observe", frame, observation)
            eligible = valid and frame["quiet"] and not frame["persistent_heat"] and not frame["load_event"]
            if eligible:
                trial["observation_elapsed"] = trial["elapsed"]
            if trial["elapsed"] >= 120:
                unresolved = trial.setdefault("nonsettled", set())
                unresolved.update(qualification["nonsettled"])
                if eligible:
                    unresolved.difference_update(qualification["settled_sensors"])
            if (eligible and (state["earned"] >= 120 or math.isclose(state["earned"], 120, rel_tol=0, abs_tol=1e-9))
                    and (trial["elapsed"] >= 120 or math.isclose(trial["elapsed"], 120, rel_tol=0, abs_tol=1e-9))
                    and qualification["qualified"] and not trial.get("nonsettled")):
                self._finish_trial("manual_complete" if frame["manual"] is not None else "success")
                self.search_since = now
                policy["state"] = dict(tag="hold")
                self._credit("ordinary_search", frame, True)
                return result("steady_low", "已确认120s有界观察收敛，重新累计60s准入；不证明带载能力")
            return result("thermal_uncertain" if frame["uncertain"] else "warming_hold" if frame["persistent_heat"] else "trial_observe",
                          f"probe confirmed={trial['elapsed']:g}/120s earned={state['earned']:g} "
                          f"cold=False margin={frame['margin']:.3f}/3C deadline={trial['deadline']:g}; {qualification['detail']}")

        edge = policy["last_downward"]
        if actual != self.level or actual < MIN_LEVEL and edge is not None and actual != edge["edge"][1]:
            if actual < MIN_LEVEL and edge is not None and actual != edge["edge"][1]:
                self._retire_search()
                self._request(max(MIN_LEVEL, reserve), actual, now, context, purpose="rollback", profile="ordinary")
                return result("confounded", "未经当前请求确认的低档读回，重建至少10档的普通证据")
            self.level = actual
            policy["state"] = dict(tag="hold")
            self._retire_search()
        if not matched:
            policy["state"] = dict(tag="hold")
            return result("hold", "执行待匹配，冻结证据")

        if frame["power_warning"] and not frame["deficits"]:
            policy["state"]["last"] = None
            if policy["rebound"] is not None:
                policy["rebound"]["last"] = None
            return result("power_warning", "短时功耗升高，保持已确认档位，暂停下降、准入及学习")

        hot_witnesses = policy["thermal_witnesses"]
        hot_fall = (bool(hot_witnesses) and all(key in frame["evidence"] and frame["evidence"][key]["fall"]
                    for key in hot_witnesses)
                    and all(s["temp"] <= 66 for s in cpu_gpu)
                    and all(s["temp"] < s["hard"] - 2 and
                            (s["group"] != "board" or s["temp"] <= s["target"] - 2)
                            for s in frame["evidence"].values()))
        if hot_fall and valid and not frame["power_warning"] and actual > max(MIN_LEVEL, reserve):
            state = self._credit("high_recovery", frame, True)
            if state["earned"] >= 30 and now - self.last_drop >= 30:
                self._request(actual - 1, actual, now, context, purpose="release", profile="recovery")
                return result("trial_pending", f"危险同源传感器持续降温 earned=30/30s，热恢复请求{actual - 1}")
            return result("high_recovery", f"危险同源传感器降温，earned={state['earned']:g}/30s，60C以上保留至少10")
        deficits = frame["deficits"]
        if deficits and actual >= MIN_LEVEL:
            previous = policy["state"].get("witness")
            witness = previous if previous in deficits else min(deficits)
            state = self._credit("graded", frame, valid, witness=witness)
            policy["cold_band"] = False
            self._retire_search()
            if state["earned"] >= 30 and valid and actual < MAX_LEVEL:
                self._request(actual + 1, actual, now, context, purpose="graded", profile="ordinary")
                return result("graded_pending", f"同一传感器{witness}在确认档位{actual}持续不足30s，相邻请求{actual + 1}")
            return result("graded_hold", f"升档观察 actual={actual} witness={witness} earned={state['earned']:g}/30s，RPM不证明容量")
        if policy["state"]["tag"] == "graded":
            policy["state"] = dict(tag="hold")
            return result("recovery", "不足证据清除，重新建立下降证据")

        edge = policy["last_downward"]
        rebound = policy["rebound"]
        stable_edge = (edge is not None and edge["edge"][1] == actual and edge["caller"] == caller
                       and edge["identity"] == identity and self._extend_context(edge["context"], context) is not None)
        deltas = [temperatures[key] - value for key, value in edge["reference"].items()
                  if key in temperatures] if stable_edge else []
        if (stable_edge and not edge["rebound_done"] and rebound is None and deltas and max(deltas) > 0
                and max(deltas) <= 2 and not frame["target_risk"] and reserve <= actual
                and all(s["temp"] < s["target"] for s in frame["evidence"].values())):
            rebound = policy["rebound"] = dict(start=now, deadline=now + 90, earned=0, last=None)
        if rebound is not None:
            bounded = (stable_edge and bool(deltas) and max(deltas) <= 2
                       and not frame["target_risk"] and reserve <= actual
                       and all(s["temp"] < s["target"] for s in frame["evidence"].values()))
            if not bounded:
                if edge is not None:
                    edge["rebound_done"] = True
                policy["rebound"], policy["cold_band"] = None, False
                policy["state"] = dict(tag="hold")
                self._retire_search()
                return result("warming_hold", "当前回温超过固定2C边界或目标余量失效，保持并重新取得资格")
            eligible = valid and bounded
            if eligible and rebound["last"] is not None and 0 < now - rebound["last"] <= 6:
                rebound["earned"] += now - rebound["last"]
            rebound["last"] = now if eligible else None
            settled = eligible and all(s["covered"] and s["slope"] < .02 and s["projected"] <= s["target"] - 3
                                       for s in frame["evidence"].values())
            if (rebound["earned"] >= 60 and settled) or now >= rebound["deadline"]:
                if edge is not None:
                    edge["rebound_done"] = True
                policy["rebound"] = None
                policy["state"] = dict(tag="hold")
                if not settled:
                    policy["cold_band"] = False
                    self._retire_search()
                    return result("hold", "回温90s固定观察到期，证据不足，保持并重新取得资格")
            else:
                return result("rebound", f"小回温固定参考，保持实际{actual} earned={rebound['earned']:g}/60s "
                              f"deadline={rebound['deadline']:g}，禁止学习或重置90s上限")

        cold_ok = (frame["cold"] and frame["low_load"] and not frame["power_guard"]
                   and context is not None and not changed and not frame["load_event"]
                   and not frame["warming"] and not frame["persistent_heat"] and not frame["uncertain"])
        if not cold_ok and not frame["wifi_gap"] and (not frame["cold"] or not frame["low_load"]
                or frame["power_guard"] or frame["load_event"] or frame["warming"] or changed):
            policy["cold_band"] = False
        if cold_ok and actual > reserve:
            policy["cold_band"] = True
            self._retire_search()
            state = self._credit("cold_release", frame, valid)
            if state["earned"] >= 30 and valid and now - self.last_drop >= 30:
                self._request(actual - 1, actual, now, context, purpose="release",
                              profile="manual" if frame["manual"] is not None else "cold")
                return result("trial_pending", f"相邻冷态请求{actual}->{actual - 1} earned=30/30s deadline={now + 60:g}")
            return result("cold_release", f"实际{actual}冷态下降 earned={state['earned']:g}/30s floor={reserve}，不计容量学习")

        if policy["cold_band"] and cold_ok and actual <= reserve:
            policy["state"] = dict(tag="cold_release")
            return result("manual" if frame["manual"] is not None else "cold_release",
                          f"冷态保持实际{actual} floor={reserve}，释放不能提供学习信用")

        if actual > max(MIN_LEVEL, reserve):
            eligible = valid and frame["quiet"] and frame["load_event"] != "load_spike" and not changed
            state = self._credit("recovery", frame, eligible)
            self.recovery_since = state["since"] if state["last"] is not None else None
            if state["earned"] >= 30 and eligible and now - self.last_drop >= 30:
                self._request(actual - 1, actual, now, context, purpose="release", profile="ordinary")
                return result("trial_pending", f"release {actual}->{actual - 1} safe=30/30s deadline={now + 60:g}")
            return result("recovery", f"release actual={actual} safe={state['earned']:g}/30s margin={frame['margin']:.3f}/2C")

        search_valid = (valid and not changed and not continuity_lost and not workload_change
                        and not frame["power_guard"] and not frame["target_risk"])
        if (self.gear_window is not None and (self.gear_window["level"] != actual or
                self._extend_context(self.gear_window["context"], context, strict=False) is None)):
            self.gear_window = None
        if not search_valid and (changed or continuity_lost or workload_change or frame["power_guard"] or frame["target_risk"]):
            self._retire_search()
        if self.gear_window is None and search_valid and means is not None:
            self.gear_window = dict(level=actual, since=now, context=context, reference=means)
        qualification = self._qualification(now, frame["manual"], frame["cold"], frame["margin"])
        eligible = search_valid and qualification["qualified"] and not frame["persistent_heat"] and not frame["load_event"]
        state = self._credit("ordinary_search", frame, eligible)
        if not qualification["qualified"] and not frame["wifi_gap"]:
            state["earned"], state["last"] = 0, None
        if eligible and self.search_since is None:
            self.search_since = now
        if (eligible and state["earned"] >= 60 and actual > reserve and self.gear_window is not None
                and now - self.gear_window["since"] >= 120 and now - self.last_drop >= 30):
            if self.search_anchor is None:
                self.search_anchor = frozenset(temperatures)
                self.search_context = context
                self.search_reference = self.gear_window["reference"]
            self._request(actual - 1, actual, now, context, purpose="probe", profile="ordinary",
                          reference=self.search_reference)
            self.search_since = None
            return result("trial_pending", f"probe actual={actual}->{actual - 1} search_ready=60/60s deadline={now + 60:g}")
        self.phase = "hold"
        strict_learning = (eligible and frame["low_load"] and frame["manual"] is None
                           and actual <= reserve and not policy["cold_band"])
        return result("manual" if frame["manual"] is not None else "warming_hold" if frame["persistent_heat"]
                      else "thermal_uncertain" if frame["uncertain"] else "steady_low",
                      f"search_ready={state['earned']:g}/60s cold={frame['cold']} actual={actual} floor/demand={reserve} "
                      f"{detail}; {qualification['detail']}", strict_learning)


class HistoryLimit(Exception):
    pass


class Journal:
    FILE = re.compile(r"thermal-\d{20}\.jsonl")
    INDEX = re.compile(r"thermal-\d{20}\.jsonl\.bounds\.json")
    SCRATCH = re.compile(r"thermal-(?:index-)?work-[a-z0-9_]{8}")
    SEGMENT_BYTES = 320 * 1024
    INDEX_BYTES = 1024
    INDEX_TOTAL_BYTES = 8 * 1024 * 1024
    DIRECTORY_ENTRIES = 32768
    PRUNE_BYTES = 8 * 1024 * 1024
    PRUNE_SECONDS = 2
    HISTORY_BYTES = 64 * 1024 * 1024
    HISTORY_ENTRIES = 32768
    HISTORY_SECONDS = 2

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.current = None
        self.bucket = None
        self.ranges = {}
        self.retained_since = None
        for path in self.root.iterdir():
            if self.SCRATCH.fullmatch(path.name) and path.is_file() and not path.is_symlink():
                path.unlink()

    def files(self):
        return sorted(p for p in self.root.iterdir()
                      if self.FILE.fullmatch(p.name) and p.is_file() and not p.is_symlink())

    def size(self):
        return sum(p.stat().st_size for p in self.root.iterdir()
                   if (self.FILE.fullmatch(p.name) or self.INDEX.fullmatch(p.name) or self.SCRATCH.fullmatch(p.name))
                   and p.is_file() and not p.is_symlink())

    @staticmethod
    def _finite(stamp):
        try:
            return type(stamp) in (int, float) and math.isfinite(stamp)
        except OverflowError:
            return False

    @staticmethod
    def _identity(path):
        import stat
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not info.st_ino:
            raise OSError("日志身份不可验证")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    @staticmethod
    def _sync_directory(root):
        # Windows has no equivalent directory fsync through this Python API.
        if os.name == "nt":
            return
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _check(work):
        if time.monotonic() > work["deadline"]:
            raise TimeoutError("日志维护超出时间预算")

    @classmethod
    def _inventory(cls, root, work):
        paths, owned, index_bytes = [], 0, 0
        index_sizes = {}
        with os.scandir(root) as directory:
            for count, entry in enumerate(directory, 1):
                cls._check(work)
                if count > cls.DIRECTORY_ENTRIES:
                    raise OSError("日志目录超出预算")
                if not entry.is_file(follow_symlinks=False):
                    continue
                raw = cls.FILE.fullmatch(entry.name)
                index = cls.INDEX.fullmatch(entry.name)
                if raw or index or cls.SCRATCH.fullmatch(entry.name):
                    length = entry.stat(follow_symlinks=False).st_size
                    owned += length
                    if index:
                        index_bytes += length
                        index_sizes[entry.name] = length
                        if index_bytes > cls.INDEX_TOTAL_BYTES:
                            raise OSError("日志索引超出预算")
                    if raw:
                        paths.append(Path(entry.path))
        work["index_sizes"] = index_sizes
        return sorted(paths), owned, index_bytes

    @classmethod
    def _certificate(cls, path, work):
        identity = cls._identity(path)
        if identity[2] > cls.SEGMENT_BYTES:
            raise OSError("日志段超过读取上限")
        sidecar = Path(str(path) + ".bounds.json")
        try:
            before = cls._identity(sidecar)
        except FileNotFoundError:
            return None
        if before[2] > cls.INDEX_BYTES:
            raise OSError("日志索引文件过大")
        work["index"] = work.get("index", 0) + before[2]
        if work["index"] > cls.INDEX_TOTAL_BYTES:
            raise OSError("日志索引读取超出预算")
        cls._check(work)
        with sidecar.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != before:
                raise OSError("日志索引读取时发生变化")
            content = stream.read(before[2])
        if cls._identity(sidecar) != before or cls._identity(path) != identity:
            raise OSError("日志索引读取时发生变化")
        try:
            record = json.loads(content)
            first, last = record["min_ts"], record["max_ts"]
            valid = record["valid_ts"]
            if (type(record["version"]) is not int or record["version"] != 1
                    or record["segment"] != path.name
                    or not isinstance(record["identity"], list) or len(record["identity"]) != 4
                    or any(type(value) is not int for value in record["identity"])
                    or tuple(record["identity"]) != identity or type(valid) is not bool
                    or not cls._finite(first) or not cls._finite(last) or first > last
                    or not valid and (first != 0 or last != 0)):
                return None
            return ((first, last), identity, valid)
        except (ValueError, UnicodeError, KeyError, TypeError):
            return None

    @classmethod
    def index_ready(cls, root):
        root = Path(root)
        work = {"deadline": time.monotonic() + 10, "index": 0}
        paths, owned, index_bytes = cls._inventory(root, work)
        raw_bytes = 0
        for path in paths:
            cls._check(work)
            record = cls._certificate(path, work)
            if record is None:
                raise OSError("日志索引缺失或失配")
            raw_bytes += record[1][2]
        cls._check(work)
        return {"ready": True, "segments": len(paths), "raw_bytes": raw_bytes, "index_bytes": index_bytes}

    def _cached(self, path):
        record = self.ranges.get(path)
        if (isinstance(record, tuple) and len(record) == 3
                and record[1] == self._identity(path)):
            return record
        return None

    @classmethod
    def _range(cls, content, work=None):
        first = last = None
        for line in content.splitlines():
            if work is not None:
                cls._check(work)
            try:
                stamp = json.loads(line)["ts"]
                if cls._finite(stamp):
                    first = stamp if first is None else min(first, stamp)
                    last = stamp if last is None else max(last, stamp)
            except (ValueError, UnicodeError, KeyError, TypeError):
                continue
        return ((first, last), True) if first is not None else ((0, 0), False)

    def _raw(self, path, work, repair=False):
        identity = self._identity(path)
        length = identity[2]
        if length > self.SEGMENT_BYTES or length > work["raw"] or repair and length > work["repair"]:
            raise OSError("日志原始读取超出预算")
        self._check(work)
        work["raw"] -= length
        if repair:
            work["repair"] -= length
        with path.open("r+b" if repair else "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != identity:
                raise OSError("日志读取时发生变化")
            content = stream.read(length)
            if repair:
                os.fsync(stream.fileno())
        if len(content) != length or self._identity(path) != identity:
            raise OSError("日志读取时发生变化")
        self._check(work)
        return content

    def _publish(self, path, bounds, valid):
        self.ranges.pop(path, None)
        identity = self._identity(path)
        sidecar = Path(str(path) + ".bounds.json")
        if sidecar.is_symlink() or sidecar.exists() and not sidecar.is_file():
            raise OSError("日志索引不是自有普通文件")
        content = json.dumps({"version": 1, "segment": path.name, "identity": identity,
                              "min_ts": bounds[0], "max_ts": bounds[1], "valid_ts": valid},
                             allow_nan=False, separators=(",", ":")).encode()
        if len(content) > self.INDEX_BYTES:
            raise OSError("日志索引文件过大")
        self._replace(sidecar, content)
        if self._identity(path) != identity:
            raise OSError("日志发布时发生变化")
        self.ranges[path] = (bounds, identity, valid)

    def _bounds(self, path, work):
        record = self._cached(path)
        self.ranges.pop(path, None)
        record = record or self._certificate(path, work)
        if record is None:
            content = self._raw(path, work, repair=True)
            bounds, valid = self._range(content, work)
            self._sync_directory(path.parent)
            self._publish(path, bounds, valid)
        else:
            self.ranges[path] = record
        self._check(work)
        return self.ranges[path][0]

    def append(self, row):
        line = (json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(line) > 65536:
            raise ValueError("记录过大")
        bucket = int(row["ts"] // 900)
        if (self.current is None or not self.current.exists() or self.bucket != bucket
                or self.current.stat().st_size + len(line) > self.SEGMENT_BYTES):
            self.current = self.root / f"thermal-{time.time_ns():020d}.jsonl"
            self.bucket = bucket
        path = self.current
        record = None
        if path.exists():
            self.bounds(path)
            record = self.ranges[path]
            if path.stat().st_size:
                with path.open("rb") as stream:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        self.ranges.pop(path, None)
                        raise OSError("日志尾行不完整，拒绝追加")
        self.ranges.pop(path, None)
        try:
            with path.open("ab") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            self._sync_directory(path.parent)
            bounds, valid = self._range(line)
            if record is not None and record[2]:
                bounds = (min(record[0][0], bounds[0]), max(record[0][1], bounds[1])) if valid else record[0]
                valid = True
            self._publish(path, bounds, valid)
        except BaseException:
            self.ranges.pop(path, None)
            raise
        if valid:
            self.retained_since = min(self.retained_since, bounds[0]) if self.retained_since is not None else bounds[0]

    def bounds(self, path):
        work = {"deadline": time.monotonic() + self.PRUNE_SECONDS, "raw": self.SEGMENT_BYTES,
                "repair": self.SEGMENT_BYTES, "index": 0}
        return self._bounds(Path(path), work)

    def read(self, since, limit):
        rows = deque(maxlen=limit)
        for path in self.files():
            if self.bounds(path)[1] < since:
                continue
            for line in path.read_bytes().splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and self._finite(row.get("ts")) and row["ts"] >= since:
                        rows.append(row)
                except (ValueError, UnicodeError):
                    continue
        return list(rows)

    def history(self, since, until, points=600):
        if since > until:
            return {"rows": [], "samples": [], "samples_truncated": False}
        bins = {}
        samples = deque(maxlen=200)
        count = entries = consumed = 0
        deadline = time.monotonic() + self.HISTORY_SECONDS
        bounds = dict(self.ranges)
        metadata_work = {"deadline": deadline, "index": 0}
        def check():
            if (entries > self.HISTORY_ENTRIES or consumed > self.HISTORY_BYTES
                    or time.monotonic() > deadline):
                raise HistoryLimit("历史读取超出预算，请缩小时间范围或选择更早的结束时间")
        paths = []
        with os.scandir(self.root) as directory:
            for entry in directory:
                entries += 1
                check()
                if self.FILE.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                    paths.append(Path(entry.path))
        previous_stamp = None
        width = max(3, (until - since) / points)
        for path in sorted(paths):
            check()
            previous_index = metadata_work["index"]
            try:
                cached = bounds.get(path)
                if path != self.current:
                    if not (isinstance(cached, tuple) and len(cached) == 3
                            and cached[1] == self._identity(path)):
                        cached = self._certificate(path, metadata_work)
                    if cached is not None and (cached[0][1] < since or cached[0][0] > until):
                        continue
            except FileNotFoundError:
                continue
            except OSError:
                cached = None
            finally:
                consumed += metadata_work["index"] - previous_index
                check()
            try:
                with path.open("rb") as stream:
                    content = stream.read(self.SEGMENT_BYTES + 1)
            except FileNotFoundError:
                continue
            consumed += len(content)
            check()
            if len(content) > self.SEGMENT_BYTES:
                raise HistoryLimit("历史文件超过读取上限，请缩小时间范围")
            for line in content.splitlines():
                entries += 1
                check()
                try:
                    row = json.loads(line)
                    stamp = row["ts"]
                    if not self._finite(stamp) or not since <= stamp <= until:
                        continue
                    sample = {key: row[key] for key in ("ts", "snapshot_at", "requested_level") if key in row}
                    if isinstance(row.get("active_power_policy"), dict):
                        sample["active_power_policy"] = {host: {key: policy[key] for key in
                            ("version", "threshold_w", "source", "generated_at") if key in policy}
                            for host, policy in row["active_power_policy"].items() if isinstance(policy, dict)}
                    for key, fields in (("control", ("manual_level",)),
                                        ("decision", ("level", "demand", "operating", "reasons", "scenario", "scenario_detail",
                                                      "learning_eligible", "trial", "execution_trial", "retry_blocked",
                                                      "trend_complete", "high_wind_gate", "high_wind_limit",
                                                      "normal_ceiling", "next_command", "actual_level", "transition_target")),
                                        ("fan", ("configured", "confirmed", "level", "rpm", "power", "mode",
                                                 "verified_at", "observed_mono", "execution", "fault", "attempts", "error",
                                                 "ambient_c", "ambient_source", "ambient_verified_at")),
                                        ("learning", ("mono", "healthy", "dropped_records"))):
                        if isinstance(row.get(key), dict):
                            sample[key] = {field: row[key][field] for field in fields if field in row[key]}
                    high = row.get("decision", {}).get("high_wind") if isinstance(row.get("decision"), dict) else None
                    if isinstance(high, dict):
                        public = {key: high[key][:240] for key in ("reason", "detail", "policy", "prediction")
                                  if isinstance(high.get(key), str)}
                        public.update({key: high[key] for key in ("actual_c", "observation_seconds", "episode_deadline", "ceiling", "working_c", "window_seconds")
                                       if self._finite(high.get(key))})
                        if high.get('exception') in ('SourceFaultProtection', 'FailedDescentRestoration'):
                            public['exception'] = high['exception']
                        source = high.get("source")
                        if valid_source(source):
                            public['source'] = list(source)
                        elif isinstance(source, (list, tuple)) and len(source) == 3 and all(isinstance(v, str) for v in source):
                            public["source"] = [v[:160] for v in source]
                        faults = high.get('source_faults')
                        if isinstance(faults, list):
                            public['source_faults'] = [dict(source=list(fault['source']),
                                fault=fault['fault'], diagnosed_mono=fault['diagnosed_mono'])
                                for fault in faults[:MAX_SOURCES] if isinstance(fault, dict)
                                and valid_source(fault.get('source')) and fault.get('fault') in {v.value for v in SourceFault}
                                and self._finite(fault.get('diagnosed_mono'))]
                        sample.setdefault("decision", {})["high_wind"] = public
                    status = row.get('decision', {}).get('control_status') if isinstance(row.get('decision'), dict) else None
                    if isinstance(status, dict):
                        public = {key: (status[key][:160] if isinstance(status.get(key), str) else None)
                            for key in ('phase', 'primary_reason', 'next_condition', 'workload_phase')}
                        public.update({key: status[key] if self._finite(status.get(key)) else None
                            for key in ('margin_c', 'observe_seconds', 'required_seconds')})
                        if status.get('release_basis') == 'high_exit' and status.get('margin_reference') == 'critical75':
                            public.update(release_basis='high_exit', margin_reference='critical75')
                            public.update({key: status[key] if self._finite(status.get(key)) else None
                                for key in ('measured_margin_c', 'trend_rate_c_per_second', 'chain_warming_c', 'warming_budget_c')})
                            public['ordinary_reason'] = (status['ordinary_reason'][:160]
                                if isinstance(status.get('ordinary_reason'), str) else None)
                        if status.get('release_basis') in ('ceiling80', 'protection'):
                            public['release_basis'] = status['release_basis']
                            public.update({key: status[key] if self._finite(status.get(key)) else None
                                for key in ('transition_target', 'next_command', 'actual_level', 'next_eligible_at',
                                    'original_deadline', 'proof_expires_mono')})
                            public['blocking_reason'] = (status['blocking_reason'][:160]
                                if isinstance(status.get('blocking_reason'), str) else None)
                            if status.get('context_kind') in ('idle', 'strict'):
                                public['context_kind'] = status['context_kind']
                            if 'risk_evidence_expires_mono' in status:
                                value = status['risk_evidence_expires_mono']
                                public['risk_evidence_expires_mono'] = value if self._finite(value) else None
                        source = status.get('blocking_source')
                        public['blocking_source'] = (list(source) if valid_source(source) else
                            [v[:160] for v in source] if isinstance(source, (list, tuple))
                            and len(source) == 3 and all(isinstance(v, str) for v in source) else None)
                        sample.setdefault('decision', {})['control_status'] = public
                    memory = row.get('decision', {}).get('thermal_memory') if isinstance(row.get('decision'), dict) else None
                    if isinstance(memory, dict):
                        public = dict(state=memory['state'] if memory.get('state') in
                            ('ready', 'pending', 'failed', 'invalid', 'not_applicable') else 'unknown',
                            descent_allowed=memory['descent_allowed'] if type(memory.get('descent_allowed')) is bool else None)
                        sample.setdefault('decision', {})['thermal_memory'] = public
                    spec = row.get('decision', {}).get('policy_spec') if isinstance(row.get('decision'), dict) else None
                    if isinstance(spec, dict) and isinstance(spec.get('version'), str):
                        sample.setdefault('decision', {})['policy_version'] = spec['version'][:80]
                        if (spec['version'] in ('enhanced80-r2', 'enhanced80-r3', 'enhanced80-r4') and self._finite(spec.get('enhanced_admission_c'))
                                and spec['enhanced_admission_c'] == 80):
                            sample['decision']['policy_spec'] = dict(version=spec['version'], enhanced_admission_c=80)
                    sample["nodes"] = [{key: node[key] for key in
                        ("host", "online", "sampled_at", "source_ts", "requested_mono", "cpu_temp", "gpu_temp", "board_temp",
                         "power_w", "power_source", "cpu_load", "gpu_load") if key in node}
                        for node in row.get("nodes", []) if isinstance(node, dict)]
                    samples.append(sample)
                    count += 1
                    interrupted = previous_stamp is not None and not 0 < stamp - previous_stamp <= 6
                    previous_stamp = stamp
                    bucket = int((stamp - since) // width)
                    target = bins.setdefault(bucket, {"ts": stamp, "nodes": {}, "level": 0})
                    target["level"] = max(target["level"], row.get("decision", {}).get("level", 0))
                    for node in row.get("nodes", []):
                        item = target["nodes"].setdefault(node["host"], {"host": node["host"], "gap": False})
                        if not node.get("online") or interrupted:
                            item["gap"] = True
                        for key in ("cpu_temp", "gpu_temp", "board_temp"):
                            value = node.get(key)
                            if value is not None:
                                item[key] = max(item.get(key, value), value)
                except (ValueError, UnicodeError, KeyError, TypeError):
                    continue
        check()
        return {"rows": [{**row, "nodes": list(row["nodes"].values())} for _, row in sorted(bins.items())],
                "samples": list(reversed(samples)), "samples_truncated": count > 200}

    def prune(self, now, retention_days, max_bytes):
        cutoff, removed = now - retention_days * 86400, 0
        work = {"deadline": time.monotonic() + self.PRUNE_SECONDS, "raw": self.PRUNE_BYTES,
                "repair": self.SEGMENT_BYTES, "index": 0}
        paths, size, _ = self._inventory(self.root, work)
        for path in paths:
            self._check(work)
            self._bounds(path, work)
        second_paths, size, _ = self._inventory(self.root, work)
        paired = 0
        unchanged = paths == second_paths
        for path in paths:
            self._check(work)
            identity = self._identity(path)
            sidecar_size = self._sidecar_size(path)
            paired += identity[2] + sidecar_size
            unchanged = (unchanged and sidecar_size == work["index_sizes"].get(path.name + ".bounds.json")
                         and identity == self.ranges[path][1] and self.ranges[path][0][0] >= cutoff)
        if size - paired > max_bytes:
            raise OSError("日志孤立索引或工作文件超出容量")
        if unchanged and size <= max_bytes:
            retained_since = now
            valid = False
            for path in paths:
                self._check(work)
                record = self.ranges[path]
                if record[2]:
                    retained_since = min(retained_since, record[0][0]) if valid else record[0][0]
                    valid = True
            self._check(work)
            self.retained_since = retained_since
            return 0
        for path in paths:
            self._check(work)
            first, last = self.ranges[path][0]
            if last < cutoff:
                self._delete(path)
                removed += 1
                continue
            if first >= cutoff:
                continue
            lines = []
            content = self._raw(path, work)
            for line in content.splitlines(keepends=True):
                self._check(work)
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and self._finite(row.get("ts")) and row["ts"] >= cutoff:
                        lines.append(line)
                except (ValueError, UnicodeError):
                    continue
            original = path.stat().st_size
            if not lines:
                self._delete(path)
                removed += 1
            elif sum(map(len, lines)) != original:
                self._rewrite(path, b"".join(lines), work)
        paths, size, _ = self._inventory(self.root, work)
        retained = list(paths)
        while paths and size > max_bytes:
            self._check(work)
            path = paths.pop(0)
            length = path.stat().st_size + self._sidecar_size(path)
            if paths:
                self._delete(path)
                retained.remove(path)
                removed += 1
                size -= length
            else:
                lines = deque(self._raw(path, work).splitlines(keepends=True))
                length_left = sum(map(len, lines))
                while lines and size - length + length_left + self.INDEX_BYTES > max_bytes:
                    self._check(work)
                    length_left -= len(lines.popleft())
                if lines:
                    self._rewrite(path, b"".join(lines), work)
                else:
                    self._delete(path)
                    retained.remove(path)
                    removed += 1
                break
        self._check(work)
        _, size, _ = self._inventory(self.root, work)
        if size > max_bytes:
            raise OSError("日志容量维护未完成")
        self.retained_since = min((self.ranges[path][0][0] for path in retained
                                   if self.ranges[path][2]), default=now)
        return removed

    def _sidecar_size(self, path):
        sidecar = Path(str(path) + ".bounds.json")
        return sidecar.stat().st_size if sidecar.is_file() and not sidecar.is_symlink() else 0

    def _delete(self, path):
        self.ranges.pop(path, None)
        path.unlink()
        self._sync_directory(path.parent)
        sidecar = Path(str(path) + ".bounds.json")
        if sidecar.is_file() and not sidecar.is_symlink():
            sidecar.unlink()
            self._sync_directory(path.parent)

    def _rewrite(self, path, content, work):
        self.ranges.pop(path, None)
        self._check(work)
        if len(content) > work["raw"]:
            raise OSError("日志重写超出预算")
        work["raw"] -= len(content)
        bounds, valid = self._range(content, work)
        self._replace(path, content)
        self._publish(path, bounds, valid)
        self._check(work)

    @classmethod
    def _replace(cls, path, content):
        temp = None
        try:
            prefix = "thermal-index-work-" if cls.INDEX.fullmatch(path.name) else "thermal-work-"
            with tempfile.NamedTemporaryFile(prefix=prefix, dir=path.parent, delete=False) as stream:
                temp = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            cls._sync_directory(path.parent)
        finally:
            if temp is not None and temp.exists():
                temp.unlink()
