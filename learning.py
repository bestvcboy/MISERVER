import hashlib
import json
import math
from copy import deepcopy

from core import CONTROLLER_VERSION, LIMITS, POWER_HOSTS, POWER_SOURCE

BUCKET = 900
SLOT = 21600
WINDOW = 72 * 3600
REPORT_LIMIT = 64 * 1024
MAX_BUCKETS = 4 * 30 * 96
REASONS = ("health", "gap", "source", "identity", "temperature", "load", "trend",
           "motor", "stabilizing", "descriptive", "unknown")
BUCKET_FIELDS = {"start", "host", "context", "gear", "source", "total", "n", "seconds", "sum",
                 "min", "max", "rpm_sum", "rpm_min", "rpm_max", "load_min", "load_max",
                 "peaks", "counters", "gears", "last"}
PROOF_FIELDS = {"train_buckets", "train_seconds", "holdout_buckets", "holdout_seconds", "reason", "source", "gear"}
MARGIN_FIELDS = {"baseline_w", "holdout_w", "candidate_w", "threshold_w", "previous_w"}
VALIDATED_FIELDS = {"keys", "evidence_from", "evidence_to", "context", "generated_at"}


def finite(value, low=0, high=1e12):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def context_of(nodes):
    identity = [(node.get("host"), node.get("hostname"), node.get("power_source"),
                 sorted((sensor["id"], sensor["group"]) for sensor in node.get("sensors", [])
                        if sensor.get("calibrated"))) for node in nodes]
    return hashlib.sha256(json.dumps([CONTROLLER_VERSION, sorted(identity)],
                                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def checkpoint_limit(settings):
    return min(4 * 1024**2, settings["max_log_mb"] * 1024**2 // 16)


def q95(values):
    return sorted(values)[math.ceil(len(values) * .95) - 1]


def strict_checkpoint(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate checkpoint key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("nonfinite checkpoint value")
    def integer(value):
        if len(value) > 18:
            raise ValueError("checkpoint integer size")
        return int(value)
    def decimal(value):
        number = float(value)
        if len(value) > 32 or not math.isfinite(number):
            raise ValueError("checkpoint decimal size")
        return number
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant,
                      parse_int=integer, parse_float=decimal)


class Learning:
    def __init__(self, hosts):
        self.hosts = tuple(sorted(hosts))
        self.buckets = {}
        self.published = {}
        self.report = {}
        self.last_slot = None
        self.last_ts = None
        self.sources = {}
        self.eligibility = {"phase": "blocked", "reason": "health", "since": None,
                            "context": None, "gear": None}
        self.loss = {"retention_buckets": 0, "cap_buckets": 0, "raw_append": 0, "crash_bytes": 0}
        self.counters = {reason: 0 for reason in REASONS}
        self.peaks = {}
        self.error = ""
        self.bytes = 0
        self.dropped = 0

    def fail(self):
        self.error = "学习检查点无效或保存失败，固定保护继续生效"
        self.published = {}
        self.eligibility.update(phase="blocked", reason="health", since=None)

    def observe(self, row):
        stamp = row["ts"]
        if not finite(stamp):
            raise ValueError("sample time")
        nodes = row.get("nodes", [])
        fan = row.get("fan", {})
        admission = row.get("learning", {})
        decision = row.get("decision", {})
        reason = ""
        dropped = admission.get("dropped_records", 0)
        if dropped > self.dropped:
            reason = "gap"
            self.dropped = dropped
            for bucket in self.buckets.values():
                bucket["last"] = None
        delta = stamp - self.last_ts if self.last_ts is not None else 0
        if self.last_ts is not None and not 0 < delta <= 6:
            reason = "gap"
        elapsed = admission.get("elapsed", delta)
        if self.last_ts is not None and (delta <= 0 or elapsed > 0 and abs(delta - elapsed) > 2):
            reason = "gap"
            self.published = {}
        self.last_ts = stamp
        if (len(nodes) != len(self.hosts) or {n.get("host") for n in nodes} != set(self.hosts)
                or not admission.get("healthy")):
            reason = reason or "health"
        gear = fan.get("level")
        rpm = fan.get("rpm")
        if (not fan.get("configured") or not fan.get("confirmed") or fan.get("fault")
                or fan.get("execution") in ("fault", "pending") or fan.get("power") != "on"
                or fan.get("mode") != "favorite" or type(gear) is not int or not 0 <= gear <= 16
                or gear != row.get("requested_level") or not finite(rpm, .001, 100000)
                or not finite(fan.get("verified_at"))
                or not 0 <= row.get("snapshot_at", stamp) - fan["verified_at"] <= 9):
            reason = reason or "motor"
        for node in nodes:
            host, source = node.get("host"), node.get("source_ts")
            credible = [s for s in node.get("sensors", []) if s.get("calibrated")]
            ids = [s.get("id") for s in credible]
            if (not node.get("online") or not finite(node.get("sampled_at"))
                    or not 0 <= row.get("snapshot_at", stamp) - node["sampled_at"] <= 6
                    or not finite(admission.get("mono")) or not finite(node.get("requested_mono"))
                    or not 0 <= admission["mono"] - node["requested_mono"] <= 6):
                reason = reason or "health"
            if not finite(source) or source <= self.sources.get(host, -math.inf):
                reason = reason or "source"
                self.published = {}
            if finite(source):
                self.sources[host] = source
            if (node.get("ambiguous_sensors") or len(ids) != len(set(ids)) or not credible
                    or any(not isinstance(i, str) or not i or len(i) > 256 for i in ids)
                    or any(not finite(node.get(k), 0, 150) for k in ("cpu_temp", "gpu_temp", "board_temp"))):
                reason = reason or "identity"
            loads = [node.get("cpu_load"), node.get("gpu_load")]
            if any(not finite(load, 0, 100) or load >= 10 for load in loads):
                reason = reason or "load"
            if (node.get("thermal_throttle") or any(not finite(s.get("temp"), 0, 150)
                    or s["temp"] >= LIMITS["cpu" if s.get("group") in ("cpu", "acpi") else
                                          "gpu" if s.get("group") == "gpu" else "board"][0] - 2
                    for s in credible)):
                reason = reason or "temperature"
            if (not decision.get("trend_complete", {}).get(host)
                    or not finite(decision.get("rise_c_per_min", {}).get(host), -1000, 1000)
                    or decision["rise_c_per_min"][host] >= 1.2):
                reason = reason or "trend"
            if host in POWER_HOSTS and (node.get("power_source") != POWER_SOURCE
                    or not finite(node.get("power_w"), 0, 1000)):
                reason = reason or "source"
                self.published.pop(host, None)
            elif host in POWER_HOSTS and node["power_w"] >= 80:
                reason = reason or "health"
        context = context_of(nodes)
        self.published = {host: policy for host, policy in self.published.items() if policy["context"] == context}
        previous = dict(self.eligibility)
        if previous["context"] is not None and previous["context"] != context:
            self.published = {}
            reason = reason or "identity"
        if "scenario" in decision and decision.get("learning_eligible") is not True:
            reason = reason or "stabilizing"
        stable = (not reason and previous["context"] == context and previous["gear"] == gear
                  and previous["since"] is not None and 0 < delta <= 6)
        since = previous["since"] if stable else stamp if not reason else None
        if not reason and stamp - since < 120:
            reason = "stabilizing"
        self.eligibility = {"phase": "eligible" if not reason else
                            "stabilizing" if reason == "stabilizing" else "blocked",
                            "reason": reason, "since": since, "context": context, "gear": gear}
        if reason:
            self.counters[reason] += 1
        for node in nodes:
            host = node.get("host")
            if host not in self.hosts:
                continue
            index = int(stamp // BUCKET)
            key = "{}|{}".format(index, host)
            bucket = self.buckets.setdefault(key, {"start": index * BUCKET, "host": host,
                "context": context, "gear": gear if type(gear) is int and 0 <= gear <= 16 else None,
                "source": node.get("power_source", "不可用"), "total": 0, "n": 0, "seconds": 0,
                "sum": 0, "min": None, "max": None, "rpm_sum": 0, "rpm_min": None,
                "rpm_max": None, "load_min": None, "load_max": None, "peaks": {},
                "counters": {}, "gears": {}, "last": None})
            bucket["total"] += 1
            gear_key = str(gear) if type(gear) is int and 0 <= gear <= 16 else "unknown"
            bucket["gears"][gear_key] = bucket["gears"].get(gear_key, 0) + 1
            for name in ("cpu_temp", "gpu_temp", "board_temp"):
                value = node.get(name)
                if finite(value, 0, 150):
                    bucket["peaks"][name] = max(bucket["peaks"].get(name, value), value)
                    self.peaks[host + "|" + name] = max(self.peaks.get(host + "|" + name, value), value)
            blocked = reason
            if bucket["context"] != context or bucket["gear"] != gear:
                blocked = "identity"
            if not blocked and (host not in POWER_HOSTS or node.get("power_source") != POWER_SOURCE):
                blocked = "descriptive"
            if blocked:
                bucket["counters"][blocked] = bucket["counters"].get(blocked, 0) + 1
                bucket["last"] = None
                continue
            power = node["power_w"]
            load = max(node["cpu_load"], node["gpu_load"])
            if bucket["last"] is not None and 0 < stamp - bucket["last"] <= 6:
                bucket["seconds"] += stamp - bucket["last"]
            bucket["last"] = stamp
            bucket["n"] += 1
            bucket["sum"] += power
            bucket["rpm_sum"] += rpm
            for name, value in (("min", power), ("rpm_min", rpm), ("load_min", load)):
                bucket[name] = min(bucket[name], value) if bucket[name] is not None else value
            for name, value in (("max", power), ("rpm_max", rpm), ("load_max", load)):
                bucket[name] = max(bucket[name], value) if bucket[name] is not None else value
        slot = int(stamp // SLOT)
        if self.last_slot is None:
            self.last_slot = slot
        elif slot > self.last_slot:
            self.last_slot = slot
            self.evaluate(slot * SLOT)

    @staticmethod
    def baseline(bucket):
        n = bucket["n"]
        if n < 270 or bucket["seconds"] < 810 or bucket["counters"]:
            return False
        mean = bucket["sum"] / n
        rpm = bucket["rpm_sum"] / n
        return (bucket["max"] < 80 and bucket["load_max"] < 10
                and all(bucket["peaks"].get(name, 150) < target - 2 for name, target in
                        (("cpu_temp", 55), ("gpu_temp", 55), ("board_temp", 65)))
                and bucket["max"] - bucket["min"] <= max(10, .1 * mean)
                and bucket["rpm_max"] - bucket["rpm_min"] <= .1 * rpm
                and bucket["load_max"] - bucket["load_min"] <= 5)

    def fit(self, host, end, context, gear, previous):
        train, hold = [], []
        for key, bucket in sorted(self.buckets.items(), key=lambda item: item[1]["start"]):
            if (bucket["host"] != host or bucket["context"] != context or bucket["gear"] != gear
                    or bucket["source"] != POWER_SOURCE or not self.baseline(bucket)):
                continue
            start = bucket["start"]
            if end - WINDOW <= start and start + BUCKET <= end - 3600:
                train.append((key, bucket))
            elif end - 3600 <= start and start + BUCKET <= end:
                hold.append((key, bucket))
        proof = {"train_buckets": len(train), "train_seconds": sum(b["seconds"] for _, b in train),
                 "holdout_buckets": len(hold), "holdout_seconds": sum(b["seconds"] for _, b in hold),
                 "reason": "insufficient", "source": POWER_SOURCE, "gear": gear}
        if (len(train) < 8 or proof["train_seconds"] < 7200 or len(hold) < 4
                or proof["holdout_seconds"] < 3300):
            return proof
        b = q95([bucket["sum"] / bucket["n"] for _, bucket in train])
        values = [bucket["sum"] / bucket["n"] for _, bucket in hold]
        v = q95(values)
        candidate = min(80, max(40, math.ceil(b + 15)))
        step = max(previous - 5, min(previous + 5, candidate))
        proof.update(baseline_w=b, holdout_w=v, candidate_w=candidate, threshold_w=step,
                     previous_w=previous, reason="margin")
        if (b + 15 > 80 or abs(v - b) > 5 or v + 15 > candidate
                or sum(value >= candidate for value in values) / len(values) > .05
                or b + 15 > step or v + 15 > step
                or sum(value >= step for value in values) / len(values) > .05):
            return proof
        proof.update(reason="validated", keys=[key for key, _ in train + hold],
                     evidence_from=min(bucket["start"] for _, bucket in train),
                     evidence_to=end, context=context, generated_at=end)
        return proof

    def valid_policy(self, policy, now, cutoff):
        return (isinstance(policy, dict) and policy.get("host") in POWER_HOSTS
                and policy.get("source") == POWER_SOURCE
                and type(policy.get("version")) is int and policy["version"] > 0
                and finite(policy.get("generated_at")) and 0 <= now - policy["generated_at"] <= WINDOW
                and policy["generated_at"] % SLOT == 0
                and finite(policy.get("evidence_from")) and policy["evidence_from"] >= cutoff
                and type(policy.get("gear")) is int and 0 <= policy["gear"] <= 16
                and finite(policy.get("previous_w"), 40, 80)
                and finite(policy.get("threshold_w"), 40, 80)
                and policy.get("reason") == "validated"
                and isinstance(policy.get("keys"), list) and bool(policy["keys"])
                and all(key in self.buckets for key in policy["keys"]))

    def evaluate(self, end):
        context, gear = self.eligibility["context"], self.eligibility["gear"]
        results = {}
        for host in POWER_HOSTS:
            old = self.published.get(host, {})
            previous = old.get("threshold_w", 80) if self.valid_policy(old, end, 0) else 80
            proof = self.fit(host, end, context, gear, previous)
            results[host] = proof
            if proof["reason"] == "validated" and self.eligibility["phase"] == "eligible":
                self.published[host] = dict(proof, host=host, version=int(end // SLOT))
                self.error = ""
        self.report = {"generated_at": end, "next_at": end + SLOT, "window_from": end - WINDOW,
                       "window_to": end, "hosts": results,
                       "description": "稳定空闲整机功耗基线；不推断低档散热能力"}

    def trim(self, now, settings, raw_since):
        cutoff = max(now - settings["retention_days"] * 86400, raw_since)
        for key in list(self.buckets):
            if self.buckets[key]["start"] < cutoff:
                del self.buckets[key]
                self.loss["retention_buckets"] += 1
        self.published = {host: policy for host, policy in self.published.items()
                          if self.valid_policy(policy, now, cutoff)}

    def checkpoint(self, now, settings, raw_since):
        self.trim(now, settings, raw_since)
        if len(json.dumps(self.report, ensure_ascii=False).encode()) > REPORT_LIMIT:
            self.report = {"generated_at": now, "next_at": (int(now // SLOT) + 1) * SLOT,
                           "hosts": {}, "description": "报告超过预算"}
        while True:
            data = {"schema": 1, "controller": CONTROLLER_VERSION, "hosts": list(self.hosts),
                    "buckets": self.buckets, "published": self.published, "report": self.report,
                    "last_slot": self.last_slot, "last_ts": self.last_ts, "loss": self.loss,
                    "counters": self.counters, "peaks": self.peaks}
            text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            self.bytes = len(text.encode())
            if self.bytes <= checkpoint_limit(settings) and len(self.buckets) <= MAX_BUCKETS:
                return text
            count = max(1, len(self.buckets) - MAX_BUCKETS,
                        math.ceil(len(self.buckets) * (1 - checkpoint_limit(settings) / self.bytes)))
            for key in sorted(self.buckets, key=lambda key: self.buckets[key]["start"])[:count]:
                del self.buckets[key]
                self.loss["cap_buckets"] += 1
            self.trim(now, settings, raw_since)

    def restore(self, text, now, settings):
        self.__init__(self.hosts)
        try:
            if len(text.encode()) > checkpoint_limit(settings):
                raise ValueError("checkpoint size")
            data = strict_checkpoint(text)
            if (not isinstance(data, dict) or set(data) != {"schema", "controller", "hosts", "buckets",
                    "published", "report", "last_slot", "last_ts", "loss", "counters", "peaks"}
                    or type(data["schema"]) is not int or data["schema"] != 1
                    or type(data["controller"]) is not int or data["controller"] not in (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14)
                    or data["hosts"] != list(self.hosts) or not isinstance(data["buckets"], dict)
                    or len(data["buckets"]) > MAX_BUCKETS
                    or data["last_ts"] is not None and (not finite(data["last_ts"]) or data["last_ts"] > now)
                    or data["last_slot"] is not None and (type(data["last_slot"]) is not int
                        or not 0 <= data["last_slot"] <= int(now // SLOT))
                    or len(json.dumps(data["report"]).encode()) > REPORT_LIMIT):
                raise ValueError("checkpoint schema")
            for key, bucket in data["buckets"].items():
                if (not isinstance(bucket, dict) or set(bucket) != BUCKET_FIELDS
                        or bucket["host"] not in self.hosts or key != "{}|{}".format(int(bucket["start"] // BUCKET), bucket["host"])
                        or not finite(bucket["start"], 0, now) or bucket["start"] % BUCKET
                        or not isinstance(bucket["context"], str) or len(bucket["context"]) != 64
                        or not isinstance(bucket["source"], str) or len(bucket["source"]) > 80
                        or bucket["gear"] is not None and (type(bucket["gear"]) is not int or not 0 <= bucket["gear"] <= 16)
                        or any(type(bucket[name]) is not int or not 0 <= bucket[name] <= 360 for name in ("n", "total"))
                        or bucket["n"] > bucket["total"] or not finite(bucket["seconds"], 0, 900)
                        or bucket["seconds"] > max(0, bucket["n"] - 1) * 6
                        or not finite(bucket["sum"], 0, bucket["n"] * 1000)
                        or not finite(bucket["rpm_sum"], 0, bucket["n"] * 100000)
                        or any(reason not in REASONS or type(count) is not int or count < 0
                               for reason, count in bucket["counters"].items())
                        or sum(bucket["counters"].values()) + bucket["n"] != bucket["total"]
                        or sum(bucket["gears"].values()) != bucket["total"]
                        or any(gear not in [str(i) for i in range(17)] + ["unknown"] or type(count) is not int or count < 0
                               for gear, count in bucket["gears"].items())
                        or any(name not in ("cpu_temp", "gpu_temp", "board_temp") or not finite(value, 0, 150)
                               for name, value in bucket["peaks"].items())):
                    raise ValueError("bucket schema")
                if bucket["n"]:
                    for lower, upper, ceiling in (("min", "max", 1000), ("rpm_min", "rpm_max", 100000),
                                                  ("load_min", "load_max", 100)):
                        if not finite(bucket[lower], 0, ceiling) or not finite(bucket[upper], bucket[lower], ceiling):
                            raise ValueError("bucket extrema")
                    if not bucket["min"] <= bucket["sum"] / bucket["n"] <= bucket["max"]:
                        raise ValueError("bucket mean")
                    if not bucket["rpm_min"] <= bucket["rpm_sum"] / bucket["n"] <= bucket["rpm_max"]:
                        raise ValueError("bucket rpm")
                    if bucket["rpm_min"] <= 0:
                        raise ValueError("bucket rpm")
                elif any(bucket[name] is not None for name in ("min", "max", "rpm_min", "rpm_max", "load_min", "load_max")):
                    raise ValueError("empty bucket extrema")
                if bucket["last"] is not None and not finite(bucket["last"], bucket["start"], min(now, bucket["start"] + BUCKET - .000001)):
                    raise ValueError("bucket time")
                bucket["last"] = None
            for name in ("loss", "counters"):
                expected = set(self.loss if name == "loss" else self.counters)
                if (set(data[name]) != expected or any(type(value) is not int or value < 0 for value in data[name].values())):
                    raise ValueError("counter schema")
            self.buckets = data["buckets"]
            self.last_ts, self.last_slot = data["last_ts"], data["last_slot"]
            self.loss, self.counters, self.peaks = data["loss"], data["counters"], data["peaks"]
            self.report = data["report"]
            if (not isinstance(self.report, dict) or not isinstance(self.peaks, dict)
                    or any(key not in {host + "|" + name for host in self.hosts for name in
                                      ("cpu_temp", "gpu_temp", "board_temp")} or not finite(value, 0, 150)
                    for key, value in self.peaks.items())):
                raise ValueError("report schema")
            if self.report:
                allowed = {"generated_at", "next_at", "hosts", "description"}
                if (set(self.report) not in (allowed, allowed | {"window_from", "window_to"})
                        or not finite(self.report["generated_at"], 0, now)
                        or self.report["next_at"] != (int(self.report["generated_at"] // SLOT) + 1) * SLOT
                        or self.report["description"] not in ("稳定空闲整机功耗基线；不推断低档散热能力", "报告超过预算")
                        or not isinstance(self.report["hosts"], dict) or not set(self.report["hosts"]) <= set(POWER_HOSTS)):
                    raise ValueError("report schema")
                if "window_from" in self.report and (self.report["window_from"] != self.report["generated_at"] - WINDOW
                        or self.report["window_to"] != self.report["generated_at"]):
                    raise ValueError("report window")
                for proof in self.report["hosts"].values():
                    reason = proof.get("reason")
                    fields = PROOF_FIELDS | (MARGIN_FIELDS if reason in ("margin", "validated") else set())
                    fields |= VALIDATED_FIELDS if reason == "validated" else set()
                    if (reason not in ("insufficient", "margin", "validated") or set(proof) != fields
                            or proof["source"] != POWER_SOURCE
                            or any(type(proof[name]) is not int or not 0 <= proof[name] <= high for name, high in
                                   (("train_buckets", 284), ("holdout_buckets", 4)))
                            or not finite(proof["train_seconds"], 0, WINDOW - 3600)
                            or not finite(proof["holdout_seconds"], 0, 3600)
                            or proof["gear"] is not None and (type(proof["gear"]) is not int or not 0 <= proof["gear"] <= 16)
                            or reason != "insufficient" and any(not finite(proof[name], 0, 1000) for name in MARGIN_FIELDS)):
                        raise ValueError("report proof")
                    if reason == "validated" and (not isinstance(proof["keys"], list) or len(proof["keys"]) > 288
                            or any(not isinstance(key, str) or len(key) > 80 for key in proof["keys"])
                            or len(set(proof["keys"])) != len(proof["keys"])
                            or not isinstance(proof["context"], str) or len(proof["context"]) != 64
                            or not finite(proof["evidence_from"], 0, self.report["generated_at"])
                            or proof["generated_at"] != self.report["generated_at"]
                            or proof["evidence_to"] != proof["generated_at"]):
                        raise ValueError("report evidence")
            if not isinstance(data["published"], dict) or not set(data["published"]) <= set(POWER_HOSTS):
                raise ValueError("policy schema")
            cutoff = now - settings["retention_days"] * 86400
            for host, policy in data["published"].items():
                if (not isinstance(policy, dict)
                        or set(policy) != PROOF_FIELDS | MARGIN_FIELDS | VALIDATED_FIELDS | {"host", "version"}):
                    raise ValueError("policy fields")
                if (not isinstance(policy["host"], str) or not isinstance(policy["source"], str)
                        or not isinstance(policy["reason"], str)
                        or type(policy["version"]) is not int or policy["version"] <= 0
                        or type(policy["gear"]) is not int or not 0 <= policy["gear"] <= 16
                        or any(type(policy[name]) is not int or not 0 <= policy[name] <= high for name, high in
                               (("train_buckets", 284), ("holdout_buckets", 4)))
                        or not finite(policy["train_seconds"], 0, WINDOW - 3600)
                        or not finite(policy["holdout_seconds"], 0, 3600)
                        or any(not finite(policy[name], 0, 1000) for name in MARGIN_FIELDS)
                        or not isinstance(policy["keys"], list) or len(policy["keys"]) > 288
                        or any(not isinstance(key, str) or len(key) > 80 for key in policy["keys"])
                        or len(set(policy["keys"])) != len(policy["keys"])
                        or not isinstance(policy["context"], str) or len(policy["context"]) != 64
                        or not finite(policy["generated_at"], 0, now)
                        or not finite(policy["evidence_from"], 0, policy["generated_at"])
                        or not finite(policy["evidence_to"], 0, now)
                        or policy["evidence_to"] != policy["generated_at"]):
                    raise ValueError("policy schema")
                if not self.valid_policy(policy, now, cutoff) or host != policy["host"]:
                    continue
                proof = self.fit(host, policy["generated_at"], policy["context"], policy["gear"], policy["previous_w"])
                if policy != dict(proof, host=host, version=int(policy["generated_at"] // SLOT)):
                    raise ValueError("policy proof")
                self.published[host] = policy
            self.bytes = len(text.encode())
            self.trim(now, settings, cutoff)
            if data["controller"] != CONTROLLER_VERSION:
                self.__init__(self.hosts)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, UnicodeError):
            self.__init__(self.hosts)
            self.fail()

    def status(self, settings):
        report = deepcopy(self.report)
        for proof in report.get("hosts", {}).values():
            if proof.get("reason") == "validated" and any(key not in self.buckets for key in proof["keys"]):
                proof["reason"] = "evidence_lost"
        return deepcopy({"published": self.published, "candidate": report,
                         "eligibility": self.eligibility, "error": self.error,
                         "next_at": (self.last_slot + 1) * SLOT if self.last_slot is not None else None,
                         "checkpoint_bytes": self.bytes, "checkpoint_limit": checkpoint_limit(settings),
                         "reserved_bytes": 2 * checkpoint_limit(settings), "loss": self.loss,
                         "counters": self.counters, "temperature_peaks": self.peaks,
                         "bucket_count": len(self.buckets), "fixed_power_w": 80})
