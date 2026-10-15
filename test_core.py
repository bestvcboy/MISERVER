import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from copy import deepcopy
from functools import lru_cache

from core import Controller, HistoryLimit, Journal, normalize


HOSTS = [f"192.168.3.{n}" for n in (180, 181, 183, 184)]


def node(host, now, cpu=45, gpu=40, board=50, load=0, power=30):
    return normalize(host, {
        "hostname": "DGX", "timestamp": now,
        "cpu_percent": load, "gpu_percent": load, "system_power_w": power,
        "system_power_source": "spbm" if host in HOSTS[:2] else "estimate:gpu",
        "temperatures": {"gpu": gpu, "board_max": board,
                         "acpi": [{"zone": "0", "type": "acpitz", "temp": cpu}],
                         "mlx": [{"label": "asic", "temp": board, "chip": "mlx5"}]},
    }, now, now)


class ControlTests(unittest.TestCase):
    def fleet(self, now, **kw):
        return [node(host, now, **kw) for host in HOSTS]

    def sensor_fleet(self, now, readings=(("wifi", 50),), **kw):
        kw.setdefault("cpu", 51)
        fleet = self.fleet(now, **kw)
        for index in (2, 3):
            sample = fleet[index]
            temperatures = {"gpu": sample["gpu_temp"],
                            "acpi": [{"zone": "0", "type": "acpitz", "temp": sample["cpu_temp"]}],
                            "mlx": [{"label": "asic", "temp": sample["board_temp"], "chip": "mlx5"}]}
            hwmon = []
            for number, (group, temperature) in enumerate(readings):
                label = f"{group}{number}"
                temperatures.setdefault(group, []).append({"chip": group, "label": label, "temp": temperature})
                hwmon.append({"kind": "temp", "chip": group, "label": label, "index": 1,
                              "device": f"/sys/devices/{label}", "value": temperature})
            fleet[index] = normalize(sample["host"], {
                "hostname": "DGX", "timestamp": now, "temperatures": temperatures,
                "hwmon_sensors": hwmon, "cpu_percent": sample["cpu_load"],
                "gpu_percent": sample["gpu_load"], "system_power_w": sample["power_w"],
                "system_power_source": sample["power_source"],
            }, now, now)
        return fleet

    @staticmethod
    @lru_cache(maxsize=128)
    def qualified_fixture(inventory):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1200, 3):
            nodes = []
            for host, sensors in inventory:
                sample = node(host, now)
                sample['sensors'] = [dict(id=identity, group=group, label=identity, calibrated=calibrated,
                    temp=45 if group in ('cpu', 'acpi') else 40 if group == 'gpu' else 50)
                    for identity, group, calibrated in sensors]
                nodes.append(sample)
            decision = controller.decide(nodes, now, minimum_level=0,
                feedback=dict(configured=True, confirmed=True, level=actual, rpm=900, power="on",
                              mode="favorite", fault=False, execution="matched", observed_mono=now))
            actual = decision["level"]
            if actual == 10 and decision["trial"] and decision["trial"]["outcome"] == "recovery_released":
                controller.fixture_start = now + 3
                controller.fixture_origin = None
                return controller
        raise AssertionError("public recovery fixture did not confirm10")

    @staticmethod
    def holding():
        controller = Controller(HOSTS)
        controller.fixture_pending = True
        return controller

    @staticmethod
    def decide(controller, nodes, now, *, actual=10, **options):
        if getattr(controller, 'fixture_pending', False):
            by_host = {sample['host']: sample for sample in nodes}
            inventory = tuple((host, tuple((s['id'], s['group'], s['calibrated'])
                for s in by_host.get(host, node(host, 0))['sensors'])) for host in HOSTS)
            del controller.fixture_pending
            controller.__dict__.update(deepcopy(ControlTests.qualified_fixture(inventory).__dict__))
        if hasattr(controller, "fixture_start"):
            if controller.fixture_origin is None:
                controller.fixture_origin = now
            offset = controller.fixture_start - controller.fixture_origin
            nodes = deepcopy(nodes)
            for sample in nodes:
                for key in ("requested_mono", "sampled_at", "source_ts"):
                    if sample.get(key) is not None:
                        sample[key] += offset
            now += offset
        return controller.decide(nodes, now, feedback={
            "configured": True, "confirmed": True, "level": actual, "rpm": 900,
            "power": "on", "mode": "favorite", "fault": False,
            "execution": "matched", "observed_mono": now,
        }, **options)

    def test_previous_unsafe_wifi_reading_cannot_be_hidden_by_absence(self):
        for case, readings, present_level in (
                ("raw", [(0, 64)], 10),
                ("projection", [(0, 62), (3, 62.2)], 10),
                ("rapid", [(0, 20), (3, 21), (6, 22)], 10),
                ("hard", [(0, 75)], 16)):
            with self.subTest(case=case):
                controller = self.holding()
                controller.level = 0
                for now, temperature in readings:
                    decision = self.decide(controller, self.sensor_fleet(now, (("wifi", temperature),)),
                                                 now, minimum_level=0)
                self.assertEqual(decision["level"], present_level)
                controller.level = 0
                decision = self.decide(controller, self.sensor_fleet(now + 3, ()), now + 3, minimum_level=0)
                self.assertEqual(decision["level"], 16)
                self.assertIn("传感器消失，保护风力请求", " ".join(decision["reasons"]))

    def test_every_critical_group_and_mixed_wifi_loss_still_force_full_wind(self):
        readings = (("wifi", 50), ("cpu", 45), ("nvme", 40), ("nic", 40), ("other", 40))
        for group in ("acpi", "cpu", "gpu", "nvme", "nic", "mlx", "other", "unrecognized"):
            for mixed in (False, True):
                with self.subTest(group=group, mixed=mixed):
                    controller = self.holding()
                    controller.level = 0
                    for now in (0, 3):
                        fleet = self.sensor_fleet(now, readings)
                        if group == "unrecognized":
                            for sensor in fleet[2]["sensors"]:
                                if sensor["group"] == "other":
                                    sensor["group"] = "unrecognized"
                        if now:
                            fleet[2]["sensors"] = [sensor for sensor in fleet[2]["sensors"]
                                if sensor["group"] != group and not (mixed and sensor["group"] == "wifi")]
                        decision = self.decide(controller, fleet, now, minimum_level=0)
                        self.assertEqual(decision["level"], 16 if now else 10)
                    self.assertIn("传感器消失，保护风力请求", " ".join(decision["reasons"]))

    def test_hot_wifi_reappearance_refreshes_observation_and_keeps_full_guard(self):
        controller = self.holding()
        controller.level = 0
        self.decide(controller, self.sensor_fleet(0), 0, minimum_level=0)
        self.assertEqual(self.decide(controller, self.sensor_fleet(3, ()), 3, minimum_level=0)["level"], 10)
        decision = self.decide(controller, self.sensor_fleet(6, (("wifi", 75),)), 6, minimum_level=0)
        self.assertEqual(decision["level"], 16)
        self.assertIn("实温/连续升温预测", " ".join(decision["reasons"]))
        observation = controller.expected[HOSTS[2]]["wifi:/sys/devices/wifi0:1:wifi0"]
        self.assertEqual(observation['seen_at'], controller.fixture_start + 6)
        self.assertEqual(observation['temp'], 75)
        self.assertFalse(observation['guard_safe'])
        controller.level = 0
        decision = self.decide(controller, self.sensor_fleet(9, ()), 9, minimum_level=0)
        self.assertEqual(decision["level"], 16)
        self.assertIn("传感器消失，保护风力请求", " ".join(decision["reasons"]))

    def test_present_normalized_wifi_keeps_thermal_branches(self):
        for readings, expected in (([(0, 63)], 10), ([(0, 63.000001)], 10),
                ([(0, 75)], 16), ([(0, 73), (3, 73.3)], 16),
                ([(0, 20), (3, 21)], 10),
                ([(now, 50 + now * .06) for now in range(0, 34, 3)], 10)):
            with self.subTest(readings=readings):
                controller = self.holding()
                controller.level = 0
                for now, temperature in readings:
                    decision = self.decide(controller, self.sensor_fleet(now, (("wifi", temperature),)),
                                                 now, minimum_level=0)
                self.assertEqual(decision["level"], expected)

    def test_ambiguous_or_throttled_cool_wifi_cannot_clear_hot_obligation(self):
        for fault in ("ambiguous_sensors", "thermal_throttle"):
            with self.subTest(fault=fault):
                controller = self.holding()
                controller.level = 0
                self.assertEqual(self.decide(controller, self.sensor_fleet(0, (("wifi", 75),)),
                                                   0, minimum_level=0)["level"], 16)
                fleet = self.sensor_fleet(3)
                for sample in fleet[2:]:
                    sample[fault] = True
                self.assertEqual(self.decide(controller, fleet, 3, minimum_level=0)["level"], 16)
                controller.level = 0
                decision = self.decide(controller, self.sensor_fleet(6, ()), 6, minimum_level=0)
                self.assertEqual(decision["level"], 16)
                self.assertIn("传感器消失，保护风力请求", " ".join(decision["reasons"]))

    def test_safe_wifi_gap_does_not_bypass_other_guards(self):
        for fault in ("throttle", "ambiguous", "source", "gap", "power", "learned_power",
                      "offline", "stale", "future", "missing_node", "missing_cpu", "missing_load"):
            with self.subTest(fault=fault):
                controller = self.holding()
                controller.level = 0
                self.decide(controller, self.sensor_fleet(0), 0, minimum_level=0)
                now = 10 if fault == "gap" else 3
                fleet, policy = self.sensor_fleet(now, ()), None
                if fault in ("throttle", "ambiguous"):
                    fleet[2]["thermal_throttle" if fault == "throttle" else "ambiguous_sensors"] = True
                elif fault == "source":
                    fleet[2]["source_ts"] = 0
                elif fault == "load":
                    fleet[2]["cpu_load"] = 30
                elif fault == "power":
                    fleet[2]["power_w"] = 80
                elif fault == "learned_power":
                    fleet[0].update(power_w=55, power_source="spbm")
                    policy = {HOSTS[0]: {"source": "spbm", "threshold_w": 50}}
                elif fault == "offline":
                    fleet[2]["online"] = False
                elif fault == "stale":
                    fleet[2]["requested_mono"] = -10
                elif fault == "future":
                    fleet[2]["requested_mono"] = now + .000001
                elif fault == "missing_node":
                    fleet.pop()
                elif fault == "missing_cpu":
                    fleet[2]["cpu_temp"] = None
                elif fault == "missing_load":
                    fleet[2]["cpu_load"] = None
                decision = self.decide(controller, fleet, now, minimum_level=0, power_thresholds=policy)
                self.assertEqual(decision["level"], 10 if fault in ("power", "learned_power") else 16)

    def test_wifi_gap_warning_survives_existing_history_projection(self):
        controller = self.holding()
        controller.level = 0
        self.decide(controller, self.sensor_fleet(0), 0, minimum_level=0)
        fleet = self.sensor_fleet(3, ())
        decision = self.decide(controller, fleet, 3, minimum_level=0)
        self.assertEqual(decision["level"], 10)
        self.assertIn("Wi-Fi", " ".join(decision["reasons"]))
        self.assertEqual(fleet[2]["board_temp"], 51)
        self.assertEqual([sensor["group"] for sensor in fleet[2]["sensors"]], ["acpi", "mlx", "gpu"])
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 3, "decision": decision, "nodes": fleet})
            sample = journal.history(0, 6)["samples"][0]
        self.assertEqual(sample["decision"]["level"], 10)
        self.assertIn("Wi-Fi", " ".join(sample["decision"]["reasons"]))

    def test_wifi_warning_survives_saturated_thermal_reasons_and_history(self):
        for temperature, level in ((64, 10), (75, 16)):
            with self.subTest(temperature=temperature):
                controller = self.holding()
                controller.level = 0
                self.decide(controller, self.sensor_fleet(0), 0, minimum_level=0)
                fleet = self.sensor_fleet(3, ())
                fleet[0] = normalize(HOSTS[0], {
                    "timestamp": 3, "cpu_percent": 0, "gpu_percent": 0, "system_power_w": 30,
                    "temperatures": {"acpi": [{"zone": "0", "type": "acpitz", "temp": 45}],
                                     "gpu": 40, "mlx": [{"chip": "mlx5", "label": "asic", "temp": 50}],
                                     "nvme": [{"chip": "nvme", "label": f"SSD-{index}",
                                               "temp": temperature} for index in range(16)]},
                }, 3, 3)
                decision = self.decide(controller, fleet, 3, minimum_level=0)
                self.assertEqual(decision["level"], level)
                self.assertLessEqual(len(decision["reasons"]), 16)
                self.assertIn("[target]" if temperature == 64 else "[hard]", " ".join(decision["reasons"]))
                warnings = [reason for reason in decision["reasons"] if "[wifi]" in reason]
                self.assertEqual(len(warnings), 1)
                for host in HOSTS[2:]:
                    self.assertIn(host, warnings[0])
                self.assertIn('age=3/30s', warnings[0])
                with tempfile.TemporaryDirectory() as root:
                    journal = Journal(root)
                    journal.append({"ts": 3, "decision": decision, "nodes": fleet})
                    reasons = journal.history(0, 6)["samples"][0]["decision"]["reasons"]
                    self.assertEqual([reason for reason in reasons if "[wifi]" in reason], warnings)

    def test_low_manual_warming_uses_ten_anchor(self):
        for minimum in range(10):
            controller = self.holding()
            controller.level = minimum
            for now in range(0, 33, 3):
                decision = self.decide(controller, self.fleet(now, cpu=45 + now * .06), now, minimum_level=minimum)
            self.assertEqual(decision["level"], 10)
        controller = self.holding()
        controller.level = 0
        self.assertEqual(self.decide(controller, self.fleet(0, cpu=58), 0, minimum_level=0)["level"], 10)
        self.assertEqual(self.decide(controller, self.fleet(3, cpu=58.000001), 3, minimum_level=0)["level"], 10)

    def test_manual_low_gears_keep_all_full_wind_protections(self):
        for minimum in range(17):
            for fault in ("missing", "stale", "gpu", "cpu", "board", "power", "missing_load",
                          "throttle", "ambiguous", "lost_sensor", "source", "gap", "rapid", "projected",
                          "offline", "future", "missing_cpu", "missing_board", "missing_gpu_load",
                          "missing_source", "repeated_clock", "reverse_clock"):
                with self.subTest(minimum=minimum, fault=fault):
                    controller = self.holding()
                    controller.level = minimum
                    self.decide(controller, self.fleet(0), 0, minimum_level=minimum)
                    now, fleet = 3, self.fleet(3)
                    if fault == "missing":
                        fleet.pop()
                    elif fault == "stale":
                        fleet[0]["requested_mono"] = -10
                    elif fault == "offline":
                        fleet[0]["online"] = False
                    elif fault == "future":
                        fleet[0]["requested_mono"] = now + .001
                    elif fault in ("missing_cpu", "missing_board"):
                        fleet[0][fault[len("missing_"):] + "_temp"] = None
                    elif fault == "missing_gpu_load":
                        fleet[0]["gpu_load"] = None
                    elif fault == "missing_source":
                        fleet[0]["source_ts"] = None
                    elif fault in ("repeated_clock", "reverse_clock"):
                        now = 0 if fault == "repeated_clock" else -1
                        for item in fleet:
                            item["requested_mono"] = now
                    elif fault == "gpu":
                        fleet[0]["gpu_temp"] = None
                    elif fault in ("cpu", "board", "load", "power"):
                        fleet = self.fleet(3, **{fault: {"cpu": 73, "board": 75, "load": 30, "power": 80}[fault]})
                    elif fault == "missing_load":
                        fleet[0]["cpu_load"] = None
                    elif fault in ("throttle", "ambiguous"):
                        fleet[0]["thermal_throttle" if fault == "throttle" else "ambiguous_sensors"] = True
                    elif fault == "lost_sensor":
                        fleet[0]["sensors"].pop()
                    elif fault == "source":
                        fleet[0]["source_ts"] = 0
                    elif fault == "gap":
                        now, fleet = 10, self.fleet(10)
                    elif fault == "rapid":
                        fleet = self.fleet(3, cpu=46)
                    else:
                        controller = self.holding()
                        controller.level = minimum
                        self.decide(controller, self.fleet(0, cpu=54), 0, minimum_level=minimum)
                        fleet = self.fleet(3, cpu=54.6)
                    expected = max(10, minimum) if fault in ("power", "rapid", "projected") else 16
                    self.assertEqual(self.decide(controller, fleet, now, minimum_level=minimum)["level"], expected)

    def test_warming_below_target_raises_airflow(self):
        controller = self.holding()
        controller.level = 10
        for now in range(0, 33, 3):
            decision = self.decide(controller, self.fleet(now, cpu=45 + now * .06), now)
        self.assertEqual(decision["level"], 10)

    def test_hot_gpu_or_cpu_immediately_max(self):
        for kw in ({"cpu": 73}, {"gpu": 73}, {"board": 75}):
            controller = self.holding()
            controller.level = 10
            self.assertEqual(self.decide(controller, self.fleet(0, **kw), 0)["level"], 16)

    def test_coherent_fast_rise_to_hard_limit_max(self):
        controller = self.holding()
        controller.level = 10
        self.decide(controller, self.fleet(0, cpu=63), 0)
        self.assertEqual(self.decide(controller, self.fleet(3, cpu=64), 3)["level"], 10)
        self.assertEqual(self.decide(controller, self.fleet(6, cpu=65), 6)["level"], 16)

    def test_missing_node_stale_or_missing_gpu_max(self):
        controller = self.holding()
        controller.level = 10
        for fleet, now in ((self.fleet(0)[:-1], 0), (self.fleet(0), 10),
                           (self.fleet(0, gpu=None), 0)):
            self.assertEqual(self.decide(controller, fleet, now)["level"], 16)

    def test_cold_sustained_ramp_then_plateau_is_not_a_hard_projection(self):
        controller = self.holding()
        for now in range(0, 46, 3):
            controller.level = 0
            decision = self.decide(controller, self.fleet(now, cpu=5 + min(now, 36) * .6), now, minimum_level=0)
        self.assertEqual(decision["level"], 10)
        self.assertGreaterEqual(decision["rise_c_per_min"][HOSTS[0]], 18)
        self.assertNotEqual(decision['scenario'], 'protect')

    def test_confirmed_slow_cold_ramp_proactively_requests_ten(self):
        controller = self.holding()
        controller.level = 0
        for now in range(0, 61, 3):
            decision = self.decide(controller, self.fleet(now, cpu=45 + now * .03), now, minimum_level=0)
            if now in (45, 60):
                self.assertEqual(decision["level"], 10)
        self.assertLess(45 + 60 * .03, 55)

    def test_cooling_trend_cannot_hide_raw_near_target_demand(self):
        controller = self.holding()
        for now in range(0, 31, 3):
            self.decide(controller, self.fleet(now, cpu=61 - now / 15), now, minimum_level=0)
        controller.level = 0
        decision = self.decide(controller, self.fleet(33, cpu=59), 33, minimum_level=0)
        self.assertEqual(decision["level"], 10)

    def test_escalation_boundaries_all_sensor_groups_and_hard_guard(self):
        for group in ("acpi", "cpu", "gpu", "nvme", "nic", "mlx", "wifi", "other"):
            target, hard = (60, 75) if group in ("acpi", "cpu", "gpu") else (65, 75)
            boundaries = [(target - 2, 10), (target - 2 + .000001, 10), (target, 10)]
            for step in range(1, 5):
                for temp in (target - 2 + 1.5 * step, target - 2 + 1.5 * step + .000001):
                    boundaries.append((temp, 16 if temp >= hard - 2 else 10))
            boundaries.extend(((hard - 2 - .000001, 10), (hard - 2, 16), (hard - .000001, 16), (hard, 16)))
            for temperature, expected in boundaries:
                with self.subTest(group=group, temperature=temperature):
                    controller = self.holding()
                    controller.level = 0
                    fleet = self.fleet(0)
                    fleet[-1]["sensors"].append({"id": "band:" + group, "group": group,
                                               "label": "band", "temp": temperature, "calibrated": True})
                    decision = self.decide(controller, fleet, 0, minimum_level=0)
                    self.assertEqual(decision["level"], expected)

    def test_learned_exact_power_only_strengthens_fixed_guard(self):
        for host, source, threshold, expected in ((HOSTS[0], "spbm", 50, 10),
                (HOSTS[2], "spbm", 50, 10), (HOSTS[0], "estimate:gpu", 50, 10),
                (HOSTS[0], "spbm", 81, 10), (HOSTS[0], "spbm", float("nan"), 10)):
            controller = self.holding()
            controller.level = 0
            fleet = self.fleet(0, power=55)
            for item in fleet:
                item["power_source"] = source
            policy = {host: {"source": source, "threshold_w": threshold}}
            self.assertEqual(self.decide(controller, fleet, 0, minimum_level=0, power_thresholds=policy)["level"], expected)
        controller.level = 0
        self.assertEqual(self.decide(controller, self.fleet(3, power=80), 3, minimum_level=0,
                         power_thresholds={HOSTS[0]: {"source":"spbm", "threshold_w":100}})["level"], 10)

    def test_sampling_gap_resets_recovery(self):
        controller = self.holding()
        self.decide(controller, self.fleet(0), 0)
        decision = self.decide(controller, self.fleet(120), 120)
        self.assertEqual(decision["level"], 16)
        self.assertIsNone(controller.cool_since)

    def test_source_clock_step_rebuilds_baseline(self):
        controller = self.holding()
        self.decide(controller, self.fleet(100), 100)
        fleet = self.fleet(103)
        fleet[0]["source_ts"] = 20
        self.assertIn("来源温度时间过期", " ".join(self.decide(controller, fleet, 103)["reasons"]))
        fleet = self.fleet(106)
        fleet[0]["source_ts"] = 106
        self.assertNotIn("来源温度时间过期", " ".join(self.decide(controller, fleet, 106)["reasons"]))
        self.assertEqual(controller.history[HOSTS[0]][fleet[0]["sensors"][0]["id"]][-1][0], controller.fixture_start + 6)

    def test_thermal_throttling_and_lost_sensor_max(self):
        controller = self.holding()
        fleet = self.fleet(0)
        self.decide(controller, fleet, 0)
        controller.level = 10
        fleet = self.fleet(3)
        fleet[0]["thermal_throttle"] = True
        self.assertEqual(self.decide(controller, fleet, 3)["level"], 16)

    def test_late_sensor_disappearance_max(self):
        controller = self.holding()
        self.decide(controller, self.fleet(0), 0)
        fleet = self.fleet(3)
        fleet[0]["sensors"].append({"id": "nvme:new", "group": "nvme", "label": "SSD",
                                     "temp": 40, "calibrated": True})
        self.decide(controller, fleet, 3)
        controller.level = 10
        decision = self.decide(controller, self.fleet(6), 6)
        self.assertEqual(decision["level"], 16)
        self.assertIn("传感器消失", " ".join(decision["reasons"]))


    def test_direct_hwmon_identity_and_raw_thermal_throttle(self):
        data = {"timestamp": 0, "temperatures": {"gpu": 40, "acpi": [{"temp": 50}],
            "nvme": [{"chip": "nvme", "label": "Composite", "temp": temp} for temp in (40, 70)]},
            "hwmon_sensors": [{"kind": "temp", "chip": "nvme", "label": "Composite",
                               "device": path, "index": 1, "value": temp}
                              for path, temp in (("/device/a", 40), ("/device/b", 70))],
            "gpu_throttle": [{"active_reasons": ["sw_thermal_slowdown"]}]}
        sample = normalize(HOSTS[0], data, 0, 0)
        self.assertFalse(sample["ambiguous_sensors"])
        self.assertTrue(sample["thermal_throttle"])
        data["temperatures"]["nvme"].reverse()
        repeated = normalize(HOSTS[0], data, 3, 3)
        self.assertEqual({s["id"]: s["temp"] for s in sample["sensors"]},
                         {s["id"]: s["temp"] for s in repeated["sensors"]})
        data["hwmon_sensors"] = []
        self.assertTrue(normalize(HOSTS[0], data, 3, 3)["ambiguous_sensors"])

    def test_spbm_never_masks_acpi_peak(self):
        sample = node(HOSTS[0], 0, cpu=60)
        self.assertEqual(sample["cpu_temp"], 60)
        self.assertTrue(sample["cpu_proxy"])

    def test_empty_cpu_sensor_still_uses_proxy(self):
        sample = normalize(HOSTS[0], {"timestamp": 0, "temperatures": {
            "cpu": [{"temp": None}], "acpi": [{"temp": 55}], "gpu": 40}}, 0, 0)
        self.assertTrue(sample["cpu_proxy"])
        self.assertEqual(sample["cpu_temp"], 55)

    def test_invalid_boundary_values_rejected(self):
        for value in (True, float("nan"), float("inf"), "55", -20, 200):
            with self.assertRaises(ValueError):
                node(HOSTS[0], 0, cpu=value)


class JournalTests(unittest.TestCase):
    def test_restart_prune_retains_samples_without_reopening_segment_bodies(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            for stamp in (1000, 2000, 3000, 4000):
                journal.append({"ts": stamp, "nodes": []})
            original = Path.open
            body_reads = []

            def observed(path, mode="r", *args, **kwargs):
                if Journal.FILE.fullmatch(path.name) and "r" in mode:
                    body_reads.append(path.name)
                return original(path, mode, *args, **kwargs)

            restarted = Journal(root)
            with patch.object(Path, "open", observed):
                self.assertEqual(restarted.prune(5000, 7, 1048576), 0)
            self.assertEqual(len(body_reads), 0)
            self.assertEqual([row["ts"] for row in restarted.read(0, 20)], [1000, 2000, 3000, 4000])

    def test_segments_and_crash_workspace_are_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            for index in range(30):
                journal.append({"ts": index, "padding": "x" * 60000})
            self.assertTrue(all(p.stat().st_size <= journal.SEGMENT_BYTES for p in journal.files()))
            scratch = Path(root) / "thermal-work-12345678"
            scratch.write_bytes(b"partial replacement")
            other = Path(root) / "thermal-work-user-document"
            other.write_text("keep")
            index_bytes = sum(p.stat().st_size for p in Path(root).glob("*.bounds.json"))
            self.assertEqual(journal.size(), sum(p.stat().st_size for p in journal.files()) + index_bytes + scratch.stat().st_size)
            recovered = Journal(root)
            self.assertFalse(scratch.exists())
            self.assertTrue(other.exists())
            self.assertEqual(len(recovered.read(0, 100)), 30)

    def test_retention_capacity_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root))
            other = Path(root) / "keep.txt"
            other.write_text("user data")
            journal.append({"ts": 0, "nodes": [], "padding": "x" * 10000})
            journal.append({"ts": 100000, "nodes": []})
            journal.prune(now=100001, retention_days=1, max_bytes=100000)
            rows = journal.read(since=0, limit=100)
            self.assertEqual([r["ts"] for r in rows], [100000])
            self.assertTrue(other.exists())
            for index in range(10):
                journal.append({"ts": 100002 + index, "padding": "x" * 2000})
            journal.prune(now=100020, retention_days=3, max_bytes=4096)
            self.assertLessEqual(journal.size(), 4096)
            self.assertEqual(journal.read(0, 100)[-1]["ts"], 100011)
            self.assertEqual(other.read_text(), "user data")

    def test_truncated_record_does_not_hide_good_records(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root))
            journal.append({"ts": 100, "nodes": []})
            journal.append({"ts": 101, "nodes": []})
            with journal.current.open("ab") as stream:
                stream.write(b'{"ts":')
            self.assertEqual([r["ts"] for r in journal.read(0, 100)], [100, 101])
            self.assertTrue(all(isinstance(r, dict) for r in journal.read(0, 100)))

    def test_chart_keeps_peak_and_missing_slot(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root))
            for now, temp in ((100, 40), (103, 60), (106, 45)):
                journal.append({"ts": now, "nodes": [node(HOSTS[0], now, cpu=temp)]})
            journal.append({"ts": 109, "nodes": [{"host": HOSTS[0], "online": False}]})
            chart = journal.history(0, 200, points=1)["rows"]
            self.assertEqual(chart[0]["nodes"][0]["cpu_temp"], 60)
            self.assertTrue(chart[0]["nodes"][0]["gap"])

    def test_absent_records_interrupt_chart(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            for now in (100, 103, 700, 703):
                journal.append({"ts": now, "nodes": [node(HOSTS[0], now)]})
            chart = journal.history(0, 800)["rows"]
            self.assertFalse(chart[1]["nodes"][0]["gap"])
            self.assertTrue(chart[2]["nodes"][0]["gap"])
            self.assertFalse(chart[3]["nodes"][0]["gap"])

    def test_history_exact_pairs_projection_and_single_scan(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            for stamp, temp, gear in ((100, 60, 16), (103, 40, 0)):
                journal.append({"ts": stamp, "snapshot_at": stamp + 2, "control": {"manual_level": None, "token": "secret"},
                    "requested_level": gear, "decision": {"level": gear, "reasons": [f"reason {gear}"], "token": "secret"},
                    "fan": {"level": gear, "rpm": gear * 100, "verified_at": stamp + 1, "token": "secret"},
                    "nodes": [node(HOSTS[0], stamp, cpu=temp)], "token": "secret"})
            original_open = Path.open
            opened = []
            def counted(path, *args, **kwargs):
                if Journal.FILE.fullmatch(path.name):
                    opened.append(path)
                return original_open(path, *args, **kwargs)
            cached = dict(journal.ranges)
            with patch.object(Path, "open", counted), patch.object(journal, "bounds", side_effect=AssertionError("second scan")):
                result = journal.history(0, 200, points=1)
            self.assertEqual(opened, journal.files())
            self.assertEqual(journal.ranges, cached)
            self.assertEqual(result["rows"][0]["level"], 16)
            self.assertEqual(result["rows"][0]["nodes"][0]["cpu_temp"], 60)
            self.assertEqual([(s["requested_level"], s["nodes"][0]["cpu_temp"]) for s in result["samples"]], [(0, 40), (16, 60)])
            self.assertEqual(result["samples"][0]["fan"]["verified_at"], 104)
            self.assertEqual(result["samples"][0]["snapshot_at"], 105)
            self.assertEqual(result["samples"][0]["control"], {"manual_level": None})
            self.assertNotIn("secret", json.dumps(result))
            self.assertFalse(result["samples_truncated"])

    def test_history_cap_journal_order_inclusive_until_and_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            for stamp in range(201):
                journal.append({"ts": stamp, "nodes": []})
            result = journal.history(0, 200)
            self.assertEqual([r["ts"] for r in result["samples"]], list(range(200, 0, -1)))
            self.assertTrue(result["samples_truncated"])
            older = journal.history(0, 199)
            self.assertEqual(len(older["samples"]), 200)
            self.assertFalse(older["samples_truncated"])
            journal.append({"ts": 50, "nodes": [{"host": HOSTS[0], "online": False, "cpu_temp": None}]})
            latest = journal.history(0, 200)["samples"][0]
            self.assertEqual(latest["ts"], 50)
            self.assertNotIn("snapshot_at", latest)
            self.assertNotIn("control", latest)
            self.assertNotIn("requested_level", latest)
            self.assertIsNone(latest["nodes"][0]["cpu_temp"])
            self.assertEqual(journal.history(300, 200), {"rows": [], "samples": [], "samples_truncated": False})

    def test_history_rejects_invalid_stamps_and_counts_malformed_lines(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 10, "nodes": []})
            with journal.current.open("ab") as stream:
                for stamp in (True, "10", None, float("nan"), float("inf")):
                    stream.write((json.dumps({"ts": stamp}) + "\n").encode())
                stream.write(b"garbage\n" * 10)
            self.assertEqual([r["ts"] for r in journal.history(0, 20)["samples"]], [10])
            with patch.object(journal, "HISTORY_ENTRIES", 5), self.assertRaises(HistoryLimit):
                journal.history(0, 20)

    def test_history_limits_bytes_time_directory_entries_and_oversized_segments(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 10, "nodes": []})
            with patch.object(journal, "HISTORY_BYTES", 1), self.assertRaises(HistoryLimit):
                journal.history(0, 20)
            with patch("core.time.monotonic", side_effect=[0, 3]), self.assertRaises(HistoryLimit):
                journal.history(0, 20)
            with patch.object(journal, "HISTORY_ENTRIES", 0), self.assertRaises(HistoryLimit):
                journal.history(0, 20)
            journal.current.write_bytes(b"x" * (Journal.SEGMENT_BYTES + 1))
            with self.assertRaises(HistoryLimit):
                journal.history(0, 20)

    def test_history_skips_closed_cached_ranges_but_reads_growing_segment(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 1, "nodes": []})
            journal.append({"ts": 1000, "nodes": []})
            old, active = journal.files()
            record = journal.ranges[active]
            journal.ranges[active] = ((0, 1), record[1], record[2])
            original_open = Path.open
            opened = []
            def counted(path, *args, **kwargs):
                if Journal.FILE.fullmatch(path.name):
                    opened.append(path)
                return original_open(path, *args, **kwargs)
            with patch.object(Path, "open", counted):
                result = journal.history(999, 1001)
            self.assertEqual(opened, [active])
            self.assertEqual(result["samples"][0]["ts"], 1000)
            journal.ranges[active] = record
            journal.append({"ts": 2000, "nodes": []})
            with patch.object(Path, "open", counted):
                journal.history(0, 2)
            self.assertEqual(opened[1:], [old, journal.current])

    def test_uncached_reader_leaves_ranges_and_crash_workspace_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 1, "nodes": []})
            journal.ranges.clear()
            scratch = Path(root) / "thermal-work-12345678"
            scratch.write_text("still owned by writer")
            self.assertEqual(journal.history(0, 2)["samples"][0]["ts"], 1)
            self.assertEqual(journal.ranges, {})
            self.assertEqual(scratch.read_text(), "still owned by writer")

    def test_readiness_and_uncached_history_are_readonly_without_old_body_scan(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            for stamp in (1000, 2000, 3000, 4000):
                journal.append({"ts": stamp, "nodes": []})
            restarted = Journal(root)
            scratch = Path(root) / "thermal-index-work-12345678"
            scratch.write_bytes(b"unfinished index")
            original = Path.open
            reads = []
            def observed(path, mode="r", *args, **kwargs):
                if Journal.FILE.fullmatch(path.name):
                    reads.append(path)
                return original(path, mode, *args, **kwargs)
            with patch.object(Path, "open", observed), patch.object(Journal, "__init__", side_effect=AssertionError("constructor")):
                report = Journal.index_ready(root)
                self.assertEqual(reads, [])
                self.assertEqual([row["ts"] for row in restarted.history(3999, 4001)["samples"]], [4000])
            self.assertEqual(reads, [restarted.files()[-1]])
            self.assertEqual(restarted.ranges, {})
            self.assertEqual(scratch.read_bytes(), b"unfinished index")
            self.assertEqual(report, {"ready": True, "segments": 4,
                "raw_bytes": sum(p.stat().st_size for p in restarted.files()),
                "index_bytes": sum(p.stat().st_size for p in Path(root).glob("*.bounds.json"))})

    def test_native_legacy_277392_byte_segment_retains_all_rows_on_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'thermal-00000000000000000001.jsonl'
            rows = []
            for index in range(5):
                size = 277392 // 5 + (index < 277392 % 5)
                base = json.dumps({'ts': 100 + index, 'padding': ''}, separators=(',', ':')) + '\n'
                rows.append((json.dumps({'ts': 100 + index, 'padding': 'x' * (size - len(base))},
                                       separators=(',', ':')) + '\n').encode())
            self.assertTrue(all(len(row) <= 65536 for row in rows))
            original = b''.join(rows)
            self.assertEqual(len(original), 277392)
            path.write_bytes(original)
            journal = Journal(root)
            self.assertEqual(journal.bounds(path), (100, 104))
            self.assertTrue(Journal.index_ready(root)['ready'])
            restarted = Journal(root)
            with patch.object(Journal, '_raw', side_effect=AssertionError('restart body read')):
                restarted.prune(105, 7, 1024 * 1024)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(restarted.retained_since, 100)
            self.assertEqual([row['ts'] for row in restarted.history(99, 105)['samples']], list(range(104, 99, -1)))

    def test_missing_stale_corrupt_metadata_rebuilds_exact_bounds(self):
        for fault in ("missing", "stale", "corrupt", "nonfinite", "wrong_identity", "wrong_segment"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                journal = Journal(root)
                journal.append({"ts": 100, "nodes": []})
                journal.append({"ts": 101, "nodes": []})
                path = journal.current
                sidecar = Path(str(path) + ".bounds.json")
                if fault == "missing":
                    sidecar.unlink()
                elif fault == "stale":
                    with path.open("ab") as stream:
                        stream.write(b'{"ts":50}\n{"ts":150}\n{"ts":')
                elif fault == "corrupt":
                    sidecar.write_bytes(b"not json")
                else:
                    record = json.loads(sidecar.read_bytes())
                    if fault == "nonfinite":
                        record["min_ts"] = float("nan")
                    elif fault == "wrong_identity":
                        record["identity"][1] += 1
                    else:
                        record["segment"] = "thermal-00000000000000000000.jsonl"
                    sidecar.write_text(json.dumps(record))
                original = path.read_bytes()
                with self.assertRaises(OSError):
                    Journal.index_ready(root)
                restarted = Journal(root)
                self.assertEqual(restarted.bounds(path), (50, 150) if fault == "stale" else (100, 101))
                self.assertEqual(path.read_bytes(), original)
                self.assertTrue(Journal.index_ready(root)["ready"])

    def test_symlink_metadata_refuses_and_preserves_target_and_link(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 10})
            path = journal.current
            sidecar = Path(str(path) + ".bounds.json")
            target = Path(root) / "keep.txt"
            target.write_bytes(sidecar.read_bytes())
            sidecar.unlink()
            try:
                sidecar.symlink_to(target)
            except OSError as exc:
                self.skipTest("filesystem symlink permission unavailable: " + str(exc))
            with self.assertRaises(OSError):
                Journal.index_ready(root)
            with self.assertRaises(OSError):
                Journal(root).prune(100000, 1, 1048576)
            self.assertTrue(path.exists())
            self.assertTrue(sidecar.is_symlink())
            self.assertEqual(json.loads(target.read_bytes())["min_ts"], 10)

    def test_raw_append_failure_invalidates_certificate_and_keeps_actual_rows(self):
        for fault in ("raw_fsync", "sidecar"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                journal = Journal(root)
                journal.append({"ts": 100})
                path = journal.current
                operation = patch("core.os.fsync", side_effect=OSError("fsync failed")) if fault == "raw_fsync" else patch.object(
                    journal, "_replace", side_effect=OSError("sidecar failed"))
                with operation, self.assertRaises(OSError):
                    journal.append({"ts": 101})
                self.assertNotIn(path, journal.ranges)
                self.assertEqual([json.loads(line)["ts"] for line in path.read_bytes().splitlines()], [100, 101])
                with self.assertRaises(OSError):
                    Journal.index_ready(root)
                self.assertEqual(Journal(root).bounds(path), (100, 101))

    def test_partial_tail_refuses_append_without_concatenating_or_losing_valid_rows(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 100})
            path = journal.current
            with path.open("ab") as stream:
                stream.write(b'{"ts":')
            content = path.read_bytes()
            with self.assertRaises(OSError):
                journal.append({"ts": 101})
            self.assertEqual(path.read_bytes(), content)
            self.assertNotIn(path, journal.ranges)
            self.assertEqual([row["ts"] for row in journal.read(0, 10)], [100])

    def test_nonmonotonic_mixed_cutoff_and_conditional_malformed_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            path = Path(root) / "thermal-00000000000000000001.jsonl"
            content = b'{"ts":200}\n{"ts":20}\n{"ts":150}\ngarbage\n{"ts":'
            path.write_bytes(content)
            self.assertEqual(journal.bounds(path), (20, 200))
            journal.prune(86410, 1, 1048576)
            self.assertEqual(path.read_bytes(), content)
            journal.prune(86500, 1, 1048576)
            self.assertEqual(path.read_bytes(), b'{"ts":200}\n{"ts":150}\n')
            self.assertEqual(journal.bounds(path), (150, 200))
            self.assertEqual(journal.retained_since, 150)
            self.assertTrue(Journal.index_ready(root)["ready"])

    def test_global_minimum_includes_later_named_file_and_ignores_invalid_stamps(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 4000})
            journal.append({"ts": 1000})
            invalid = Path(root) / "thermal-00000000000000000001.jsonl"
            invalid.write_bytes(b'{"ts":true}\n{"ts":"5"}\n{"ts":null}\n{"ts":NaN}\n{"ts":Infinity}\n')
            journal.prune(5000, 7, 1048576)
            self.assertEqual(journal.retained_since, 1000)
            self.assertEqual(journal.bounds(invalid), (0, 0))
            self.assertEqual([row["ts"] for row in journal.read(0, 10)], [4000, 1000])

    def test_all_bounds_precede_deletion_and_repair_budget_is_aggregate(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            old = Path(root) / "thermal-00000000000000000000.jsonl"
            old.write_bytes(b'{"ts":0}\n')
            journal.bounds(old)
            unknown = []
            for index in (1, 2):
                path = Path(root) / ("thermal-%020d.jsonl" % index)
                path.write_bytes(((json.dumps({"ts": 100000, "padding": "x" * 60000}) + "\n") * 3).encode())
                unknown.append(path)
            expected = {path: path.read_bytes() for path in [old] + unknown}
            with self.assertRaises(OSError):
                Journal(root).prune(100001, 1, 1048576)
            self.assertEqual({path: path.read_bytes() for path in expected}, expected)
            self.assertEqual(Journal.SEGMENT_BYTES, 327680)

    def test_prune_raw_read_and_rewrite_budget_and_time_refuse(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            path = Path(root) / "thermal-00000000000000000001.jsonl"
            path.write_bytes(b'{"ts":0}\n{"ts":100}\n')
            journal.bounds(path)
            original = path.read_bytes()
            with patch.object(journal, "PRUNE_BYTES", len(original)), self.assertRaises(OSError):
                journal.prune(86450, 1, 1048576)
            self.assertEqual(path.read_bytes(), original)
            with patch("core.time.monotonic", side_effect=[0, 3]), self.assertRaises(OSError):
                journal.prune(86450, 1, 1048576)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(Journal.PRUNE_BYTES, 8388608)

    def test_replacement_sync_failure_keeps_new_raw_and_invalidates_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            path = Path(root) / "thermal-00000000000000000001.jsonl"
            path.write_bytes(b'{"ts":0}\n{"ts":100}\n')
            journal.bounds(path)
            with patch.object(Journal, "_sync_directory", side_effect=OSError("directory fsync failed")), self.assertRaises(OSError):
                journal.prune(86450, 1, 1048576)
            self.assertNotIn(path, journal.ranges)
            self.assertEqual(path.read_bytes(), b'{"ts":100}\n')
            with self.assertRaises(OSError):
                Journal.index_ready(root)
            self.assertEqual(Journal(root).bounds(path), (100, 100))

    def test_orphan_and_scratch_bytes_count_without_unrelated_deletion(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 0})
            path = journal.current
            orphan = Path(root) / "thermal-00000000000000000000.jsonl.bounds.json"
            scratch = Path(root) / "thermal-index-work-12345678"
            other = Path(root) / "thermal-work-user-document"
            orphan.write_bytes(b"orphan")
            scratch.write_bytes(b"scratch")
            other.write_bytes(b"user")
            self.assertEqual(journal.size(), sum(p.stat().st_size for p in Path(root).iterdir() if p != other))
            with self.assertRaises(OSError):
                journal.prune(100000, 1, 12)
            self.assertTrue(path.exists())
            journal.prune(100000, 1, 13)
            self.assertFalse(path.exists())
            self.assertFalse(Path(str(path) + ".bounds.json").exists())
            self.assertEqual(journal.size(), 13)
            self.assertEqual(orphan.read_bytes(), b"orphan")
            self.assertEqual(scratch.read_bytes(), b"scratch")
            self.assertEqual(other.read_bytes(), b"user")

    def test_readiness_directory_metadata_time_and_oversized_raw_caps(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 100})
            with patch.object(Journal, "DIRECTORY_ENTRIES", 1), self.assertRaises(OSError):
                Journal.index_ready(root)
            with patch.object(Journal, "INDEX_TOTAL_BYTES", 1), self.assertRaises(OSError):
                Journal.index_ready(root)
            with patch("core.time.monotonic", side_effect=[0, 11]), self.assertRaises(OSError):
                Journal.index_ready(root)
            sidecar = Path(str(journal.current) + ".bounds.json")
            sidecar.write_bytes(b"x" * 1025)
            with self.assertRaises(OSError):
                Journal.index_ready(root)
            journal.current.write_bytes(b"x" * 262145)
            with self.assertRaises(OSError):
                Journal(root).bounds(journal.current)

    def test_same_size_replacement_invalidates_history_skip_without_mutating_cache(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 1000, "nodes": []})
            old = journal.current
            journal.append({"ts": 2000, "nodes": []})
            replacement = Path(root) / "replacement"
            replacement.write_bytes(old.read_bytes().replace(b"1000", b"1900"))
            self.assertEqual(replacement.stat().st_size, old.stat().st_size)
            replacement.replace(old)
            cached = dict(journal.ranges)
            self.assertEqual([row["ts"] for row in journal.history(1899, 1901)["samples"]], [1900])
            self.assertEqual(journal.ranges, cached)
            with self.assertRaises(OSError):
                Journal.index_ready(root)
            self.assertEqual(journal.bounds(old), (1900, 1900))

    def test_index_publish_fsync_rename_and_directory_failure_preserve_raw(self):
        for stage in ("fsync", "rename", "directory"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                journal = Journal(root)
                journal.append({"ts": 100})
                path = journal.current
                original_replace = Journal._replace
                def interrupted(target, content):
                    tool = "core.os.fsync" if stage == "fsync" else "core.os.replace" if stage == "rename" else None
                    operation = patch(tool, side_effect=OSError("interrupted index")) if tool else patch.object(
                        Journal, "_sync_directory", side_effect=OSError("interrupted index directory"))
                    with operation:
                        original_replace(target, content)
                with patch.object(journal, "_replace", interrupted), self.assertRaises(OSError):
                    journal.append({"ts": 101})
                self.assertNotIn(path, journal.ranges)
                self.assertEqual([json.loads(line)["ts"] for line in path.read_bytes().splitlines()], [100, 101])
                self.assertEqual(Journal(root).bounds(path), (100, 101))

    def test_interrupted_delete_leaves_accounted_orphan_without_other_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            journal.append({"ts": 0})
            path = journal.current
            sidecar = Path(str(path) + ".bounds.json")
            expected = sidecar.read_bytes()
            with patch.object(Journal, "_sync_directory", side_effect=OSError("unlink directory failed")), self.assertRaises(OSError):
                journal.prune(100000, 1, 1048576)
            self.assertFalse(path.exists())
            self.assertEqual(sidecar.read_bytes(), expected)
            self.assertEqual(journal.size(), len(expected))
            self.assertEqual(Journal.index_ready(root), {"ready": True, "segments": 0, "raw_bytes": 0,
                                                        "index_bytes": len(expected)})


if __name__ == "__main__":
    unittest.main()
