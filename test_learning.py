import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from learning import BUCKET, SLOT, WINDOW, Learning, checkpoint_limit, context_of, q95
from test_core import HOSTS, node

SETTINGS = {"retention_days": 7, "max_log_mb": 4096, "manual_level": None}


def frame(now, power=35, gear=0, healthy=True):
    nodes = [node(host, now, power=power) for host in HOSTS]
    for item in nodes:
        item["power_source"] = "spbm" if item["host"] in HOSTS[:2] else "estimate:gpu"
    return {"ts": now, "snapshot_at": now, "nodes": nodes, "requested_level": gear,
            "fan": {"configured": True, "confirmed": True, "level": gear, "rpm": 800,
                    "power": "on", "mode": "favorite", "verified_at": now},
            "decision": {"trend_complete": dict.fromkeys(HOSTS, True), "rise_c_per_min": dict.fromkeys(HOSTS, 0)},
            "learning": {"healthy": healthy, "mono": now, "elapsed": 3, "dropped_records": 0}}


def populated_learning(version, end=SLOT):
    learner = Learning(HOSTS)
    with patch("learning.CONTROLLER_VERSION", version):
        for now in range(end - SLOT, end + 1, 3):
            row = frame(now)
            if version in (4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
                row["decision"].update(scenario="steady", learning_eligible=True)
            learner.observe(row)
    return learner


class LearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checkpoints = {}
        for version in (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
            learner = populated_learning(version)
            with patch("learning.CONTROLLER_VERSION", version):
                cls.checkpoints[version] = learner.checkpoint(SLOT, SETTINGS, 0)
            if version == 14:
                cls.complete = learner

    def test_populated_legacy_restore_resets_every_state_benignly_and_persists_fourteen(self):
        for version in (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13):
            with self.subTest(version=version):
                text = self.checkpoints[version]
                data = json.loads(text)
                self.assertEqual(data["controller"], version)
                self.assertEqual(len(data["published"]), 2)
                self.assertEqual(data["published"][HOSTS[0]]["threshold_w"], 75)
                with patch("learning.CONTROLLER_VERSION", version):
                    context = context_of(frame(0)["nodes"])
                self.assertTrue(all(bucket["context"] == context for bucket in data["buckets"].values()))
                self.assertTrue(all(proof["context"] == context for proof in data["report"]["hosts"].values()))
                self.assertTrue(all(policy["context"] == context for policy in data["published"].values()))
                destination = deepcopy(self.complete)
                destination.observe(frame(SLOT + 3))
                destination.fail()
                for _ in range(2):
                    destination.restore(text, SLOT + 6, SETTINGS)
                    self.assertEqual(destination.__dict__, Learning(HOSTS).__dict__)
                durable = destination.checkpoint(SLOT + 6, SETTINGS, 0)
                self.assertEqual(json.loads(durable)["controller"], 14)
                restarted = Learning(HOSTS)
                restarted.restore(durable, SLOT + 9, SETTINGS)
                self.assertFalse(restarted.error)
                self.assertEqual(restarted.published, {})
                self.assertEqual(restarted.buckets, {})
                destination.observe(frame(SLOT + 9))
                resumed = json.loads(destination.checkpoint(SLOT + 9, SETTINGS, 0))
                self.assertEqual(resumed["controller"], 14)
                self.assertEqual(resumed["published"], {})
                self.assertNotEqual(next(iter(resumed["buckets"].values()))["context"], context)

    def test_version_fourteen_roundtrip_keeps_proofs_and_clears_old_runtime_state(self):
        original = deepcopy(self.complete)
        text = original.checkpoint(SLOT, SETTINGS, 0)
        self.assertEqual(json.loads(text)["controller"], 14)
        self.assertEqual(len(original.published), 2)
        restored = deepcopy(self.complete)
        restored.published[HOSTS[2]] = {"obsolete": True}
        restored.fail()
        restored.restore(text, SLOT + 3, SETTINGS)
        self.assertFalse(restored.error)
        self.assertEqual(restored.published, original.published)
        self.assertEqual(restored.report, original.report)
        self.assertEqual(restored.loss, original.loss)
        self.assertEqual(restored.counters, original.counters)
        self.assertEqual(restored.peaks, original.peaks)
        self.assertEqual(restored.last_slot, original.last_slot)
        self.assertEqual(restored.last_ts, original.last_ts)
        self.assertEqual(restored.buckets, {key: dict(bucket, last=None)
                                          for key, bucket in original.buckets.items()})
        self.assertEqual(restored.sources, {})
        self.assertEqual(restored.eligibility, Learning(HOSTS).eligibility)
        durable = restored.checkpoint(SLOT + 3, SETTINGS, 0)
        restarted = Learning(HOSTS)
        restarted.restore(durable, SLOT + 6, SETTINGS)
        self.assertFalse(restarted.error)
        self.assertEqual(restarted.checkpoint(SLOT + 6, SETTINGS, 0), durable)

    def test_recognized_versions_fully_validate_before_restore_or_discard(self):
        for version in (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
            text = self.checkpoints[version]
            for corruption in ("duplicate", "nonfinite", "overflow", "report", "count", "time",
                               "proof", "extra", "extrema", "report_proof", "policy_fields", "hosts", "size"):
                with self.subTest(version=version, corruption=corruption):
                    data = json.loads(text)
                    if corruption == "duplicate":
                        corrupted = text.replace('"schema":1', '"schema":1,"schema":1', 1)
                    elif corruption == "nonfinite":
                        corrupted = text.replace('"last_ts":21600', '"last_ts":NaN', 1)
                    elif corruption == "overflow":
                        corrupted = text.replace('"last_ts":21600', '"last_ts":1e400', 1)
                    elif corruption == "size":
                        corrupted = " " * (checkpoint_limit(SETTINGS) + 1)
                    else:
                        if corruption == "report":
                            data["report"]["extra"] = True
                        elif corruption == "count":
                            next(iter(data["buckets"].values()))["total"] = 1000000
                        elif corruption == "time":
                            next(iter(data["buckets"].values()))["last"] = SLOT * 2
                        elif corruption == "proof":
                            data["published"][HOSTS[0]]["threshold_w"] = 40
                        elif corruption == "extra":
                            data["extra"] = True
                        elif corruption == "extrema":
                            next(bucket for bucket in data["buckets"].values() if bucket["n"])["max"] = 0
                        elif corruption == "report_proof":
                            data["report"]["hosts"][HOSTS[0]]["keys"].append("missing")
                            data["report"]["hosts"][HOSTS[0]]["keys"].append("missing")
                        elif corruption == "policy_fields":
                            del data["published"][HOSTS[0]]["previous_w"]
                        else:
                            data["hosts"] = []
                        corrupted = json.dumps(data)
                    invalid = deepcopy(self.complete)
                    invalid.restore(corrupted, SLOT + 3, SETTINGS)
                    self.assertTrue(invalid.error)
                    self.assertEqual(invalid.published, {})
                    self.assertEqual(invalid.buckets, {})
                    self.assertEqual(invalid.report, {})
        for version in (True, False, 1, 11, 15, "2", "3", "4", "5", "6", "7", "8", "9", "10", "12", "13", 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 13.0, None):
            with self.subTest(version=version):
                data = json.loads(self.checkpoints[10])
                data["controller"] = version
                invalid = Learning(HOSTS)
                invalid.restore(json.dumps(data), SLOT + 3, SETTINGS)
                self.assertTrue(invalid.error)
                self.assertEqual(invalid.published, {})

    def test_ineligible_policy_skip_semantics_survive_version_upgrade(self):
        for version in (2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
            for now, threshold in ((SLOT + 3, 39), (SLOT + WINDOW + 1, 75)):
                with self.subTest(version=version, now=now):
                    data = json.loads(self.checkpoints[version])
                    for policy in data["published"].values():
                        policy["threshold_w"] = threshold
                    restored = Learning(HOSTS)
                    restored.restore(json.dumps(data), now, SETTINGS)
                    self.assertFalse(restored.error)
                    self.assertEqual(restored.published, {})

    def test_scenario_requires_literal_true_to_admit_samples(self):
        for eligible in ("missing", False, None, 0, 1, "true", [], {}):
            with self.subTest(eligible=eligible):
                learner = Learning(HOSTS)
                for now in range(0, 151, 3):
                    row = frame(now)
                    row["decision"]["scenario"] = "downshift_observation"
                    if eligible != "missing":
                        row["decision"]["learning_eligible"] = eligible
                    learner.observe(row)
                self.assertEqual(learner.eligibility["reason"], "stabilizing")
                self.assertIsNone(learner.eligibility["since"])
                self.assertEqual(learner.counters["stabilizing"], 51)
                self.assertFalse(learner.published)
                for bucket in learner.buckets.values():
                    self.assertEqual((bucket["n"], bucket["seconds"], bucket["sum"]), (0, 0, 0))
                    self.assertEqual(bucket["counters"], {"stabilizing": 51})
                    self.assertEqual(bucket["total"], 51)
                    self.assertEqual(bucket["peaks"]["cpu_temp"], 45)

    def test_old_ten_empty_checkpoint_is_benign_and_resets_every_state(self):
        with patch("learning.CONTROLLER_VERSION", 10):
            text = Learning(HOSTS).checkpoint(SLOT, SETTINGS, 0)
        restored = deepcopy(self.complete)
        restored.restore(text, SLOT + 3, SETTINGS)
        self.assertEqual(restored.__dict__, Learning(HOSTS).__dict__)

    def test_old_ten_malformed_policy_values_fail_even_when_expired_or_ineligible(self):
        corruptions = [("policy", []), ("policy", None), ("policy", "fields"),
                       ("threshold_w", "39"), ("threshold_w", True), ("previous_w", []),
                       ("keys", "missing"), ("keys", [{}]), ("keys", ["duplicate", "duplicate"]),
                       ("context", []), ("context", "short"), ("generated_at", "21600"),
                       ("evidence_from", None), ("evidence_to", "21600"), ("host", []),
                       ("source", {}), ("reason", []), ("version", True), ("gear", False),
                       ("train_buckets", "19"), ("holdout_seconds", [])]
        for now in (SLOT + 3, SLOT + WINDOW + 1):
            for field, value in corruptions:
                with self.subTest(now=now, field=field, value=value):
                    data = json.loads(self.checkpoints[10])
                    policy = data["published"][HOSTS[0]]
                    policy["threshold_w"] = 39
                    if field == "policy":
                        data["published"][HOSTS[0]] = value
                    else:
                        policy[field] = value
                    restored = deepcopy(self.complete)
                    restored.restore(json.dumps(data), now, SETTINGS)
                    expected = Learning(HOSTS)
                    expected.fail()
                    self.assertEqual(restored.__dict__, expected.__dict__)

    def test_scenario_true_and_legacy_rows_keep_idle_learning(self):
        for scenario in (True, False):
            with self.subTest(scenario=scenario):
                learner = Learning(HOSTS)
                for now in range(0, 151, 3):
                    row = frame(now)
                    row["decision"]["learning_eligible"] = scenario
                    if scenario:
                        row["decision"]["scenario"] = "steady"
                    learner.observe(row)
                self.assertEqual(learner.eligibility["phase"], "eligible")
                for host in HOSTS[:2]:
                    bucket = learner.buckets["0|" + host]
                    self.assertEqual((bucket["n"], bucket["seconds"], bucket["sum"]), (11, 30, 385))
                for host in HOSTS[2:]:
                    self.assertEqual(learner.buckets["0|" + host]["n"], 0)

    def test_scenario_refusal_restarts_stability_without_bridging_or_republishing(self):
        learner = deepcopy(self.complete)
        policies = deepcopy(learner.published)
        row = frame(SLOT + 3)
        row["decision"].update(scenario="downshift_observation", learning_eligible=False)
        before = {key: (bucket["n"], bucket["seconds"]) for key, bucket in learner.buckets.items()}
        learner.observe(row)
        self.assertEqual(learner.published, policies)
        self.assertEqual(learner.eligibility["reason"], "stabilizing")
        self.assertEqual({key: (bucket["n"], bucket["seconds"]) for key, bucket in learner.buckets.items()}, before)
        learner.evaluate(SLOT)
        self.assertEqual(learner.published, policies)
        for now in range(SLOT + 6, SLOT + 126, 3):
            row = frame(now)
            row["decision"].update(scenario="steady", learning_eligible=True)
            learner.observe(row)
        self.assertEqual(learner.eligibility["reason"], "stabilizing")
        self.assertEqual({key: (bucket["n"], bucket["seconds"]) for key, bucket in learner.buckets.items()}, before)
        row = frame(SLOT + 126)
        row["decision"].update(scenario="steady", learning_eligible=True)
        learner.observe(row)
        self.assertEqual(learner.eligibility["phase"], "eligible")
        for host in HOSTS[:2]:
            bucket = learner.buckets["24|" + host]
            self.assertEqual((bucket["n"], bucket["seconds"]), (2, 0))

    def test_scenario_gate_preserves_health_reasons_and_never_overrides_safety(self):
        for eligible in (False, True):
            for fault in ("health", "motor", "load", "trend", "temperature", "source"):
                with self.subTest(eligible=eligible, fault=fault):
                    learner = deepcopy(self.complete)
                    row = frame(SLOT + 3)
                    row["decision"].update(scenario="steady", learning_eligible=eligible)
                    if fault == "health":
                        row["learning"]["healthy"] = False
                    elif fault == "motor":
                        row["fan"]["confirmed"] = False
                    elif fault == "load":
                        row["nodes"][0]["cpu_load"] = 40
                    elif fault == "trend":
                        row["decision"]["trend_complete"][HOSTS[0]] = False
                    elif fault == "temperature":
                        row["nodes"][0]["sensors"][0]["temp"] = 70
                    else:
                        row["nodes"][0]["power_source"] = "estimate:gpu"
                    before = sum(bucket["n"] for bucket in learner.buckets.values())
                    learner.observe(row)
                    self.assertEqual(learner.eligibility["reason"], fault)
                    self.assertEqual(sum(bucket["n"] for bucket in learner.buckets.values()), before)

    def test_six_hour_chronology_numeric_step_and_no_gpu_publication(self):
        learner = deepcopy(self.complete)
        self.assertEqual(set(learner.published), set(HOSTS[:2]))
        policy = learner.published[HOSTS[0]]
        self.assertEqual((policy["baseline_w"], policy["holdout_w"], policy["candidate_w"], policy["threshold_w"]), (35, 35, 50, 75))
        self.assertEqual((policy["train_buckets"], policy["train_seconds"], policy["holdout_buckets"], policy["holdout_seconds"]), (19, 17043, 4, 3588))
        self.assertEqual(learner.status(SETTINGS)["next_at"], 43200)
        old = deepcopy(learner.published)
        learner.observe(frame(SLOT + 3))
        self.assertEqual(learner.published, old)
        learner.evaluate(43200)
        self.assertEqual(learner.published, old)
        self.assertEqual(learner.report["hosts"][HOSTS[0]]["reason"], "insufficient")
        self.assertEqual(q95(list(range(1, 21))), 19)

    def test_insufficient_window_never_publishes(self):
        learner = Learning(HOSTS)
        learner.observe(frame(0))
        learner.observe(frame(SLOT))
        self.assertEqual(learner.published, {})
        self.assertEqual(learner.report["hosts"][HOSTS[0]]["reason"], "insufficient")

    def test_margin_drift_range_and_nuisance_failures(self):
        for change in ("drift", "margin", "power_range", "rpm_range", "load_range", "nuisance"):
            learner = deepcopy(self.complete)
            for bucket in learner.buckets.values():
                if bucket["host"] != HOSTS[0] or not bucket["n"]:
                    continue
                if change == "margin":
                    bucket.update(sum=66 * bucket["n"], min=66, max=66)
                elif bucket["start"] >= SLOT - 3600:
                    if change == "drift":
                        bucket.update(sum=41 * bucket["n"], min=41, max=41)
                    elif change == "nuisance":
                        bucket.update(sum=75 * bucket["n"], min=75, max=75)
                    elif change == "power_range":
                        bucket.update(min=25, max=46)
                    elif change == "rpm_range":
                        bucket.update(rpm_min=700, rpm_max=900)
                    else:
                        bucket.update(load_min=0, load_max=6)
            proof = learner.fit(HOSTS[0], SLOT, learner.eligibility["context"], 0, 80)
            self.assertNotEqual(proof["reason"], "validated", change)

    def test_minimum_evidence_seconds_and_step_up_stay_bounded(self):
        learner = deepcopy(self.complete)
        context = learner.eligibility["context"]
        for bucket in learner.buckets.values():
            if bucket["host"] == HOSTS[0] and bucket["start"] >= SLOT - 3600:
                bucket["seconds"] = 824
        self.assertEqual(learner.fit(HOSTS[0], SLOT, context, 0, 80)["reason"], "insufficient")
        proof = self.complete.fit(HOSTS[0], SLOT, context, 0, 40)
        self.assertEqual(proof["threshold_w"], 45)
        self.assertEqual(proof["reason"], "margin")
        proof = self.complete.fit(HOSTS[0], SLOT, context, 0, 55)
        self.assertEqual((proof["threshold_w"], proof["reason"]), (50, "validated"))

    def test_unsafe_unknown_and_dropped_frames_never_bridge_coverage(self):
        learner = Learning(HOSTS)
        for now in range(0, 151, 3):
            learner.observe(frame(now))
        key = "0|" + HOSTS[0]
        seconds = learner.buckets[key]["seconds"]
        bad = frame(153, healthy=False)
        bad["nodes"][0]["cpu_temp"] = 70
        learner.observe(bad)
        for now in range(156, 277, 3):
            learner.observe(frame(now))
        self.assertEqual(learner.buckets[key]["seconds"], seconds)
        self.assertGreater(learner.buckets[key]["counters"]["health"], 0)
        self.assertEqual(learner.buckets[key]["peaks"]["cpu_temp"], 70)
        dropped = frame(279)
        dropped["learning"]["dropped_records"] = 1
        learner.observe(dropped)
        self.assertEqual(learner.eligibility["reason"], "gap")
        self.assertEqual(learner.buckets[key]["seconds"], seconds)

    def test_source_identity_clock_invalidate_but_gear_and_pending_retain(self):
        for field in ("source", "identity", "clock", "gear", "pending", "load"):
            learner = deepcopy(self.complete)
            row = frame(SLOT + 3)
            if field == "source":
                row["nodes"][0]["power_source"] = "estimate:gpu"
            elif field == "identity":
                row["nodes"][0]["hostname"] = "other"
            elif field == "clock":
                row["nodes"][0]["source_ts"] = 0
            elif field == "gear":
                row = frame(SLOT + 3, gear=16)
            elif field == "pending":
                row["fan"].update(confirmed=False, execution="pending")
            else:
                row["nodes"][0]["cpu_load"] = 40
            learner.observe(row)
            self.assertEqual(bool(learner.published), field in ("gear", "pending", "load"), field)

    def test_bounded_checkpoint_restart_proof_ttl_retention_and_corruption(self):
        learner = deepcopy(self.complete)
        text = learner.checkpoint(SLOT, SETTINGS, 0)
        restored = Learning(HOSTS)
        restored.restore(text, SLOT + 3, SETTINGS)
        self.assertFalse(restored.error)
        self.assertEqual(restored.published, learner.published)
        self.assertEqual(restored.eligibility["phase"], "blocked")
        self.assertTrue(all(bucket["last"] is None for bucket in restored.buckets.values()))
        restored.trim(SLOT + WINDOW + 1, SETTINGS, 0)
        self.assertFalse(restored.published)
        shrunk = deepcopy(learner)
        shrunk.trim(SLOT, SETTINGS, 1000)
        self.assertFalse(shrunk.published)
        shorter = deepcopy(learner)
        shorter.trim(86400 + 901, {**SETTINGS, "retention_days": 1}, 0)
        self.assertFalse(shorter.published)
        for corruption in ("duplicate", "nonfinite", "overflow", "report", "count", "time", "proof", "extra"):
            data = json.loads(text)
            if corruption == "duplicate":
                corrupted = text.replace('"schema":1', '"schema":1,"schema":1', 1)
            elif corruption == "nonfinite":
                corrupted = text.replace('"last_ts":21600', '"last_ts":NaN', 1)
            elif corruption == "overflow":
                corrupted = text.replace('"last_ts":21600', '"last_ts":1e400', 1)
            else:
                if corruption == "report":
                    data["report"]["credential"] = "must not expose"
                elif corruption == "count":
                    next(iter(data["buckets"].values()))["total"] = 1000000
                elif corruption == "time":
                    next(iter(data["buckets"].values()))["last"] = SLOT * 2
                elif corruption == "proof":
                    data["published"][HOSTS[0]]["threshold_w"] = 40
                else:
                    data["extra"] = "unknown"
                corrupted = json.dumps(data)
            invalid = Learning(HOSTS)
            invalid.restore(corrupted, SLOT + 3, SETTINGS)
            self.assertTrue(invalid.error, corruption)
            self.assertFalse(invalid.published, corruption)
            self.assertEqual(invalid.report, {})
        tiny = {**SETTINGS, "max_log_mb": 8}
        for bucket in learner.buckets.values():
            bucket["counters"]["unknown"] = 1
        for index in range(2000):
            bucket = deepcopy(next(iter(learner.buckets.values())))
            bucket["start"] = index * BUCKET
            learner.buckets[str(index) + "|" + HOSTS[0]] = bucket
        output = learner.checkpoint(2000 * BUCKET, {**tiny, "retention_days": 30}, 0)
        self.assertLessEqual(len(output.encode()), checkpoint_limit(tiny))
        self.assertGreater(learner.loss["cap_buckets"], 0)

    def test_small_wall_clock_jump_invalidates_but_restart_gap_retains_proof(self):
        learner = deepcopy(self.complete)
        jumped = frame(SLOT + 5.5)
        jumped["learning"]["elapsed"] = 3
        learner.observe(jumped)
        self.assertEqual(learner.eligibility["reason"], "gap")
        self.assertFalse(learner.published)

        original = deepcopy(self.complete)
        text = original.checkpoint(SLOT, SETTINGS, 0)
        restored = Learning(HOSTS)
        restored.restore(text, SLOT + 900, SETTINGS)
        restarted = frame(SLOT + 900)
        restarted["learning"].update(elapsed=0, mono=3)
        for node in restarted["nodes"]:
            node["requested_mono"] = 3
        restored.observe(restarted)
        self.assertEqual(restored.published, original.published)
        self.assertEqual(restored.eligibility["reason"], "gap")
        self.assertTrue(all(bucket["seconds"] == 0 for bucket in restored.buckets.values()
                            if bucket["start"] == SLOT + 900))


if __name__ == "__main__":
    unittest.main()
