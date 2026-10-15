import json
import math
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from statistics import median

GROUPS = ("acpi", "cpu", "nvme", "nic", "mlx", "wifi", "spbm", "other")
LIMITS = {"cpu": (60, 75), "gpu": (60, 75), "board": (65, 75)}
MAX_LEVEL, MIN_LEVEL = 16, 10
POWER_HOSTS = ("192.168.3.180", "192.168.3.181")
POWER_SOURCE = "spbm"
CONTROLLER_VERSION = 14


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
    return {"host": host, "hostname": str(data.get("hostname", host))[:80],
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

    def __init__(self, hosts):
        self.hosts = set(hosts)
        self._policy = dict(level=MAX_LEVEL, phase="protect", trial=None, baseline=None,
                            mode=None, operation=None, last_feedback=-math.inf, last_drop=-math.inf,
                            cool_since=None, recovery_since=None, search_anchor=None,
                            search_context=None, search_reference=None, search_since=None,
                            gear_window=None, state=dict(tag="protect"), startup_required=True,
                            protect_cause="startup", thermal_witnesses=set(), preparation=False,
                            cold_band=False, last_downward=None, rebound=None,
                            learning_window=deque(maxlen=512), learning_key=None)
        self.history = {}
        self.positive_since = {}
        self.expected = {}
        self.sources = {}
        self.last_evaluation = None
        self.load_history = {}
        self.workload_history = {}
        self.workload_context = None
        self.power_exposure = {}

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

    def _finish_trial(self, outcome):
        if self.trial is not None and (self.trial["outcome"] is None or
                                      outcome in ("invalidated", "failed") and self.trial["outcome"] in
                                      ("recovery_released", "success", "manual_complete")):
            self.trial["outcome"] = self.trial["status"] = outcome

    def _retire_search(self):
        self.cool_since = self.baseline = self.search_since = None
        self.search_anchor = self.search_context = self.search_reference = None
        self.gear_window = None

    def _retire_guards(self):
        for expected in self.expected.values():
            for observation in expected.values():
                observation["guard_safe"] = False
                observation.pop("noise_pending", None)

    def _request(self, destination, actual, now, context, *, purpose, profile, reference=None):
        operation = self._policy["operation"]
        if (self._policy["startup_required"] and operation is not None
                and operation["outcome"] == "execution_timeout"):
            self.level = MAX_LEVEL
            return
        if self._pending():
            trial = self._policy["operation"]
            if destination != trial["destination"]:
                trial["destination"] = destination
                trial["destination_requested_at"] = now
                trial["attribution"] = "confounded"
                trial["purpose"], trial["profile"] = purpose, profile
                self._finish_trial("invalidated")
            self.level = destination
            return
        if destination == actual:
            self.level = destination
            return
        episode = _Episode(actual, destination, now, now + 60)
        operation = dict(from_=actual, to=destination, kind="probe" if purpose == "probe" else "release",
                          purpose=purpose, profile=profile, requested_at=now, confirmed_at=None,
                          status="pending", outcome=None, observation_elapsed=None, elapsed=0,
                          deadline=episode.original_deadline, original_deadline=episode.original_deadline,
                          episode=episode, destination=destination,
                          destination_requested_at=now, baseline=context, attribution="comparable",
                          reference=reference, temperatures=self._temperatures())
        operation["from"] = operation.pop("from_")
        self._policy["operation"] = operation
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
        if state.get("key") != key or extended is None:
            state = dict(tag=tag, key=key, context=frame["context"], earned=0, last=None,
                         since=frame["now"], witness=witness, confirmed_at=frame["now"])
            self._policy["state"] = state
        else:
            state["context"] = extended
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
            for key in ("requested_at", "original_deadline", "destination_requested_at"):
                value = diagnostic.get("requested_at" if key == "destination_requested_at" and not execution else key)
                if type(value) in (int, float) and math.isfinite(value):
                    trial[key] = value
            if execution:
                trial.update(original_from=diagnostic["from"], original_to=diagnostic["to"],
                             to=diagnostic["destination"])
                if diagnostic.get("episode") is not None:
                    trial.update(status="pending", outcome=None)
                elif diagnostic.get("confirmed_at") is not None:
                    trial["status"] = "observing" if diagnostic["status"] == "observing" else "confirmed"
        return trial

    def _decision(self, scenario, detail, reasons=(), slopes=None, complete=None, eligible=False):
        trial = self._trial_view(self.trial)
        execution = self._trial_view(self._policy["operation"], execution=True)
        grouped = {}
        for reason in dict.fromkeys(reasons):
            category = reason.split("]", 1)[0] if reason.startswith("[") else reason
            grouped.setdefault(category, []).append(reason)
        summaries = ["; ".join(items[:3]) + (f"; 同类另 {len(items) - 3} 项" if len(items) > 3 else "")
                     for items in grouped.values()]
        return {"level": self.level, "reasons": [detail] + summaries[:15],
                "rise_c_per_min": slopes or {}, "trend_complete": complete or {}, "limits": LIMITS,
                "scenario": scenario, "scenario_detail": detail, "learning_eligible": eligible,
                "trial": trial, "execution_trial": execution, "retry_blocked": False}

    def protect(self, reason):
        now = self.last_evaluation if self.last_evaluation is not None else 0
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
        return self._decision("protect", str(reason))

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
    def _extend_context(baseline, current, *, strict=True):
        if current is None or baseline is None or baseline.keys() != current.keys():
            return None
        extended = {}
        for host, (source, ids, ranges) in current.items():
            old_source, old_ids, old_ranges = baseline[host]
            if (source, ids) != (old_source, old_ids):
                return None
            combined = tuple((min(a[0], b[0]), max(a[1], b[1])) for a, b in zip(ranges, old_ranges))
            if strict and any(high - low > (5 if index < 2 else max(10, .1 * (high + low) / 2))
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

    def decide(self, nodes, now, *, minimum_level=MIN_LEVEL, power_thresholds=None,
               feedback=None, manual_level=None):
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
            reasons.append(f"[fault] 采样间隔异常 elapsed={evaluation_elapsed:g}，要求 0<dt<=6s，重新建立全风保护基线")
            demand, quiet = MAX_LEVEL, False
            telemetry_fault = True
        self.last_evaluation = now
        if {n["host"] for n in nodes} != self.hosts or len(nodes) != len(self.hosts):
            reasons.append(f"[fault] 节点缺失 expected={sorted(self.hosts)} actual={[n['host'] for n in nodes]}，全风保护")
            demand, quiet = MAX_LEVEL, False
            telemetry_fault = True
        for node in nodes:
            host = node["host"]
            if (not node.get("online") or now - node.get("requested_mono", -math.inf) > 6
                    or not 0 <= now - node.get("requested_mono", -math.inf) <= 6
                    or any(node.get(k) is None for k in ("cpu_temp", "gpu_temp", "board_temp"))):
                reasons.append(f"[fault] {host} 温度缺失或过期 age={now - node.get('requested_mono', -math.inf):g}/6s，全风保护")
                demand, quiet = MAX_LEVEL, False
                telemetry_fault = True
                self.history.pop(host, None)
                self.positive_since.pop(host, None)
                continue
            source_ts = node.get("source_ts")
            sampled_at = node.get("sampled_at")
            source_age = (sampled_at + now - node["requested_mono"] - source_ts
                          if type(sampled_at) in (int, float) and math.isfinite(sampled_at)
                          and type(source_ts) in (int, float) and math.isfinite(source_ts) else math.inf)
            if not -2 <= source_age <= 6:
                reasons.append(f"[fault] {host} 来源温度时间过期或超前 age={source_age:g}s，允许-2..6s，全风保护")
                demand, quiet, telemetry_fault = MAX_LEVEL, False, True
                self.history.pop(host, None)
                self.positive_since.pop(host, None)
                continue
            if source_ts is None or source_ts <= self.sources.get(host, -math.inf):
                reasons.append(f"[fault] {host} 采集时间未推进 source={source_ts} previous={self.sources.get(host)}，全风保护")
                demand, quiet = MAX_LEVEL, False
                telemetry_fault = True
                self.history.pop(host, None)
                self.positive_since.pop(host, None)
                if source_ts is not None:
                    self.sources[host] = source_ts
                continue
            else:
                self.sources[host] = source_ts
            credible = [s for s in node["sensors"] if s["calibrated"]]
            if node.get("thermal_throttle") or node.get("ambiguous_sensors"):
                reasons.append(f"[fault] {host} 热节流或传感器身份不明确 throttle={node.get('thermal_throttle')} ambiguous={node.get('ambiguous_sensors')}，全风保护")
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
                    reasons.append(f"[fault] {host} 传感器消失，全风保护 sensor={identity.rsplit('/', 1)[-1]} age={age:g}/30s last_guard_safe={observation['guard_safe']}")
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
                reasons.append(f"[fault] {host} 负载数据缺失 cpu={node.get('cpu_load')} gpu={node.get('gpu_load')}，全风保护")
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
                                       f"old/new={medians[::-1]}，保持风力，暂停继续试降")
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
                falling = (covered and slope <= -.01 and len(recent) == 3
                           and recent[-1][0] - recent[0][0] >= 6
                           and all(0 < b[0] - a[0] <= 6 and b[1] <= a[1]
                                   for a, b in zip(recent, recent[1:]))
                           and recent[-1][1] < recent[0][1])
                accepted = temp + max(0, coherent) * 30
                witness = (host, sensor["id"], sensor["group"])
                evidence[witness] = dict(temp=temp, target=target, hard=hard, rise=coherent,
                                         fall=falling, covered=covered, projected=accepted,
                                         group=group, slope=slope)
                if (temp >= hard - 2 or coherent > 0 and accepted >= hard
                        or group in ("cpu", "gpu") and temp >= 70 and coherent > 0):
                    thermal_risk = True
                    reasons.append(f"[hard] {host} {sensor_name} 实温/连续升温预测触发全风 "
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
                        reasons.append(f"[warming] {host} {sensor_name} slope={slope:.4f}C/s 已开始升温，暂停试降")
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
        if telemetry_fault or thermal_risk or wifi_gap:
            self.power_exposure.clear()
        return self._advance(frame)

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
            strict = (eligible and valid and frame["low_load"] and frame["manual"] is None
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
            operation.update(episode=None, outcome="execution_timeout", status="execution_timeout",
                         elapsed=max(0, now - episode.original_requested_at))
            self._finish_trial("execution_timeout")
            policy.update(startup_required=True, protect_cause="fault", cold_band=False,
                          state=dict(tag="protect"), rebound=None, last_downward=None)
            self._retire_search()
            self.level, self.phase = MAX_LEVEL, "protect"
            self.history.clear()
            self.positive_since.clear()
            reasons.insert(0, "[feedback] 原请求60s执行截止已到期，晚到确认不能消除超时")
            return result("protect", f"execution_timeout original_deadline={episode.original_deadline:g}，请求16并重建启动证据")

        if frame["telemetry_fault"] or not healthy or frame["thermal_risk"]:
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
                reasons.insert(0, "[feedback] 净化器实际反馈未知、过期或异常，有效期<=9s，全风请求尚不代表执行")
            return result("protect", "保护请求16，等待当前目的档位匹配读回及当前安全证据")

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
            if reserve > destination:
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
                    policy["last_downward"] = dict(edge=(episode.original_from, actual), reference=temperatures,
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
                    self.gear_window = dict(level=actual, since=now, context=context, reference=means)
                    self.recovery_since = now
                    self.phase = "hold"
                    self._credit("cold_release" if trial["profile"] == "cold" else "recovery", frame, valid)
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
            self.level, self.phase = MAX_LEVEL, "protect"
            if actual != MAX_LEVEL:
                if operation is None or operation["outcome"] != "execution_timeout":
                    self._request(MAX_LEVEL, actual, now, context, purpose="protect", profile="protect")
                return result("protect", "启动或故障恢复，等待实际16档确认")
            startup_ok = (valid and frame["quiet"] and not changed and not frame["power_warning"]
                          and frame["load_event"] != "load_spike")
            state = self._credit("startup", frame, startup_ok)
            self.recovery_since = state["since"] if state["last"] is not None else None
            if not startup_ok or state["earned"] < 30:
                return result("thermal_uncertain" if frame["uncertain"] else "warming_hold" if frame["warming"] else "protect",
                              f"启动新基线 earned={state['earned']:g}/30s，完整匹配确认后才允许下降")
            policy["startup_required"], policy["protect_cause"] = False, None
            policy["state"] = dict(tag="hold")
            if actual > max(MIN_LEVEL, reserve) and now - self.last_drop >= 30:
                profile = "cold" if frame["cold"] and frame["low_load"] and not frame["warming"] else "ordinary"
                policy["cold_band"] = profile == "cold"
                self._request(actual - 1, actual, now, context, purpose="release", profile=profile)
                return result("trial_pending", f"启动证据30s完成，相邻请求{actual - 1} deadline={now + 60:g}")

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
            raise OSError("日志维护超出时间预算")

    @classmethod
    def _inventory(cls, root, work):
        paths, owned, index_bytes = [], 0, 0
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
                        if index_bytes > cls.INDEX_TOTAL_BYTES:
                            raise OSError("日志索引超出预算")
                    if raw:
                        paths.append(Path(entry.path))
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
                                        ("decision", ("level", "reasons", "scenario", "scenario_detail",
                                                      "learning_eligible", "trial", "execution_trial", "retry_blocked",
                                                      "trend_complete")),
                                        ("fan", ("configured", "confirmed", "level", "rpm", "power", "mode",
                                                 "verified_at", "observed_mono", "execution", "fault", "attempts", "error",
                                                 "ambient_c", "ambient_source", "ambient_verified_at")),
                                        ("learning", ("mono", "healthy", "dropped_records"))):
                        if isinstance(row.get(key), dict):
                            sample[key] = {field: row[key][field] for field in fields if field in row[key]}
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
        _, size, _ = self._inventory(self.root, work)
        paired = sum(self._identity(path)[2] + self._sidecar_size(path) for path in paths)
        if size - paired > max_bytes:
            raise OSError("日志孤立索引或工作文件超出容量")
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
