import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

from aiohttp.test_utils import TestClient, TestServer

from service import (DEVICE_ID, DEVICE_IP, DEVICE_MAC, DEVICE_MODEL, MOTOR_IDENTITY, STATE,
                     MotorSchedule, atomic_write, create_app, device_from_token,
                     fan_apply, settings_checked, trusted_peer, drain_owned, settings_from_disk)
from qr_connector import QRConnector, Phase, TARGET, START_CONSENT, BIND_CONSENT
from test_qr_connector import FakeCloud, OWNER

class SettingsSchemaTests(unittest.TestCase):
    def test_canonical_strict_range_and_disk_only_migration(self):
        base = {"retention_days": 3, "max_log_mb": 2048}
        for level in [None] + list(range(17)):
            self.assertEqual(settings_checked({**base, "manual_level": level}), {**base, "manual_level": level})
        for level in (True, False, -1, 17, 0.0, "0", [], float("nan")):
            with self.assertRaises(ValueError):
                settings_checked({**base, "manual_level": level})
        for legacy, expected in ((True, 16), (False, None)):
            value = {**base, "full_speed": legacy}
            self.assertEqual(settings_from_disk(value), {**base, "manual_level": expected})
            with self.assertRaises(ValueError):
                settings_checked(value)
        for value in ({**base, "full_speed": 1}, {**base, "manual_level": 0, "full_speed": False},
                      base, {**base, "manual_level": None, "extra": 0}):
            with self.assertRaises(ValueError):
                settings_from_disk(value)


class FakeFan:
    def __init__(self, initial=None):
        self.actual = initial or ["on", "favorite", 16, 2080]
        self.writes = []
        self.fail = False
        self.ignore_writes = False
        self.lose_ack = False

    def send(self, command, properties):
        if self.fail:
            raise RuntimeError("secret device detail")
        return list(self.actual)

    def on(self):
        self.writes.append("on")
        self.actual[0] = "on"

    def set_mode(self, mode):
        self.writes.append("favorite")
        self.actual[1] = "favorite"

    def set_favorite_level(self, level):
        self.writes.append(level)
        if not self.ignore_writes:
            self.actual[2] = level
        if self.lose_ack:
            raise RuntimeError("lost acknowledgement")


class OfflineSession:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, *args, **kwargs):
        class Response:
            status = 200
            async def __aenter__(self):
                self.content = self
                return self
            async def __aexit__(self, *args):
                pass
            async def iter_chunked(self, size):
                yield json.dumps({"timestamp": time.time(), "cpu_percent": 0, "gpu_percent": 0,
                    "temperatures": {"gpu": 40, "acpi": [{"zone": "0", "temp": 45}],
                                     "mlx": [{"chip": "mlx5", "label": "asic", "temp": 50}]}}).encode()
        return Response()


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FanTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.schedule = MotorSchedule()
        self.timer = patch("service.time.monotonic", self.clock)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def test_optional_ambient_is_independent_of_motor_validity(self):
        fields = ("ambient_c", "ambient_source", "ambient_observed_mono", "ambient_verified_at")
        for wire, expected in ((308, 30.8), (0, 0), (-200, -20), (800, 80), (308.5, 30.85),
                               (None, None), (True, None), ("308", None), ([], None),
                               (float("nan"), None), (float("inf"), None),
                               (-201, None), (801, None), (10 ** 400, None)):
            for rpm in (1699, 0):
                with self.subTest(wire=wire, rpm=rpm), patch("service.time.time", return_value=1000):
                    fan = FakeFan(["on", "favorite", 10, rpm, wire])
                    with patch.object(fan, "send", wraps=fan.send) as send:
                        result = fan_apply(fan, lambda: 10, MotorSchedule())
                    send.assert_called_once_with("get_prop", ["power", "mode", "favorite_level", "motor1_speed", "temp_dec"])
                    self.assertEqual((result["confirmed"], result["level"], result["rpm"]), (rpm > 0, 10, rpm))
                    self.assertEqual((result["attempts"], result["fault"], fan.writes), (0, False, []))
                    self.assertEqual(tuple(result[key] for key in fields), (None,) * 4 if expected is None
                                     else (expected, "purifier:70632677:temp_dec", 100, 1000))
                    json.dumps(result, allow_nan=False)

    def test_ambient_accepts_only_four_or_five_value_lists(self):
        for actual in (["on", "favorite", 16, 2080], ["on", "favorite", 16],
                       ["on", "favorite", 16, 2080, 308, 1], ("on", "favorite", 16, 2080, 308),
                       ["on", "favorite", True, 2080, 308], ["on", "favorite", 16, float("nan"), 308]):
            with self.subTest(actual=actual):
                fan = FakeFan()
                with patch.object(fan, "send", return_value=actual) as send:
                    result = fan_apply(fan, lambda: 16, MotorSchedule())
                self.assertEqual(result["confirmed"], isinstance(actual, list) and len(actual) == 4)
                self.assertEqual([result[key] for key in ("ambient_c", "ambient_source",
                                 "ambient_observed_mono", "ambient_verified_at")], [None] * 4)
                self.assertEqual((send.call_count, result["attempts"], fan.writes), (1, 0, []))

    def test_ambient_stamp_precedes_target_callback_and_changes_only_on_read(self):
        calls = []
        def target():
            calls.append(True)
            if len(calls) == 2:
                self.clock.now = 120
            return 16
        fan = FakeFan(["on", "favorite", 16, 2080, 308])
        with patch("service.time.time", side_effect=lambda: self.clock.now + 900):
            result = fan_apply(fan, target, self.schedule)
        self.assertEqual((result["ambient_observed_mono"], result["ambient_verified_at"]), (100, 1000))
        self.assertEqual(result["observed_mono"], 100)

        for failure in ("write", "read", None):
            with self.subTest(failure=failure):
                self.clock.now = 100
                fan = FakeFan(["on", "favorite", 14, 1500, 308])
                def mutate(level):
                    self.clock.now = 120
                    fan.actual[2], fan.actual[4] = level, 312
                    fan.fail = failure == "read"
                    if failure == "write":
                        raise RuntimeError("secret device detail")
                with patch.object(fan, "set_favorite_level", side_effect=mutate), \
                        patch.object(fan, "send", wraps=fan.send) as send, \
                        patch("service.time.time", side_effect=lambda: self.clock.now + 900):
                    result = fan_apply(fan, lambda: 16, MotorSchedule())
                self.assertEqual(send.call_count, 1 if failure == "write" else 2)
                self.assertNotIn("secret device detail", json.dumps(result))
                self.assertEqual(tuple(result[key] for key in ("ambient_c", "ambient_source",
                                 "ambient_observed_mono", "ambient_verified_at")), (None,) * 4 if failure
                                 else (31.2, "purifier:70632677:temp_dec", 120, 1020))
                self.assertEqual(result["attempts"], 1 if failure else 0)

    def test_post_write_missing_ambient_does_not_reuse_pre_write_observation(self):
        for suffix in ([], [None], [True], ["bad"], [float("nan")]):
            with self.subTest(suffix=suffix):
                fan = FakeFan(["on", "favorite", 14, 1500, 308])
                with patch.object(fan, "send", side_effect=[list(fan.actual), ["on", "favorite", 16, 1500] + suffix]):
                    result = fan_apply(fan, lambda: 16, MotorSchedule())
                self.assertEqual((result["confirmed"], result["attempts"], result["fault"], fan.writes), (True, 0, False, [16]))
                self.assertEqual([result[key] for key in ("ambient_c", "ambient_source",
                                 "ambient_observed_mono", "ambient_verified_at")], [None] * 4)
                json.dumps(result, allow_nan=False)

    def test_readback_and_external_changes(self):
        fan = FakeFan(["off", "idle", 10, 1200])
        state = fan_apply(fan, lambda: 16, self.schedule)
        self.assertTrue(state["confirmed"])
        self.assertEqual(fan.writes, ["on", "favorite", 16])
        fan.writes.clear()
        self.assertTrue(fan_apply(fan, lambda: 16, self.schedule)["confirmed"])
        self.assertFalse(fan.writes)

    def test_monotonic_readback_is_physical_and_invalidated_before_writes(self):
        fan = FakeFan()
        result = fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual(result.get("observed_mono"), 100)
        self.assertEqual(fan.writes, [])
        self.clock.now = 110
        fan.actual[2] = 14
        fan.lose_ack = True
        result = fan_apply(fan, lambda: 16, self.schedule)
        self.assertIsNone(result.get("observed_mono"))
        self.assertFalse(result["confirmed"])
        self.assertEqual(fan.writes, [16])
        self.clock.now = 120
        result = fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual(result.get("observed_mono"), 120)
        self.assertTrue(result["confirmed"])

    def test_target_callback_cannot_refresh_an_old_physical_readback(self):
        calls = []
        def target():
            calls.append(True)
            if len(calls) == 2:
                self.clock.now += 20
            return 16
        result = fan_apply(FakeFan(), target, self.schedule)
        self.assertEqual(result["observed_mono"], 100)
        self.assertEqual(self.clock.now, 120)

    def test_recovered_second_attempt_retains_protection_interruption(self):
        self.schedule.attempts, self.schedule.expected_level = 1, 16
        fan = FakeFan(["on", "favorite", 14, 1500])
        result = fan_apply(fan, lambda: 16 if self.schedule.fault else 15, self.schedule)
        self.assertTrue(result["protection_interrupted"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["level"], 16)
        self.assertEqual(fan.writes, [16])
        self.assertEqual(self.schedule.attempts, 0)
        self.assertFalse(self.schedule.fault)

    def test_zero_rpm_not_confirmed(self):
        fan = FakeFan(["on", "favorite", 16, 0])
        result = fan_apply(fan, lambda: 16, self.schedule)
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["rpm"], 0)
        self.assertEqual(fan.writes, [])

    def test_pending_reduction_superseded(self):
        targets = iter([10, 16])
        fan = FakeFan(["on", "favorite", 16, 2080])
        result = fan_apply(fan, lambda: next(targets), self.schedule)
        self.assertTrue(result["confirmed"])
        self.assertEqual(fan.writes, [])

    def test_level_only_never_rewrites_power_or_mode(self):
        fan = FakeFan(["on", "favorite", 10, 1200])
        self.assertTrue(fan_apply(fan, lambda: 16, self.schedule)["confirmed"])
        self.assertEqual(fan.writes, [16])
        self.clock.now += 7.99
        fan.actual[2] = 10
        self.assertEqual(fan_apply(fan, lambda: 16, self.schedule)["execution"], "pending")
        self.assertEqual(fan.writes, [16])
        self.clock.now += .01
        fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual(fan.writes, [16, 16])

    def test_latest_level_coalesces_in_window(self):
        fan = FakeFan(["on", "favorite", 10, 1200])
        fan_apply(fan, lambda: 12, self.schedule)
        self.clock.now += 3
        fan_apply(fan, lambda: 14, self.schedule)
        fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual(fan.writes, [12])
        self.clock.now += 5
        fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual(fan.writes, [12, 16])

    def test_maximum_raised_during_power_or_mode_never_writes_stale_reduction(self):
        for method in ("on", "set_mode"):
            for actual_level in (12, 16):
                with self.subTest(method=method, actual_level=actual_level):
                    target = [10]
                    fan = FakeFan(["off" if method == "on" else "on", "auto", actual_level, 1200])
                    original = getattr(fan, method)
                    def change(*args):
                        original(*args)
                        target[0] = 16
                    setattr(fan, method, change)
                    schedule = MotorSchedule()
                    result = fan_apply(fan, lambda: target[0], schedule)
                    self.assertTrue(result["confirmed"])
                    self.assertEqual([item for item in fan.writes if type(item) is int], [16] if actual_level < 16 else [])
                    self.assertIsNone(schedule.expected_level)

    def test_round_retains_higher_request_if_demand_decreases_during_mode(self):
        for elapsed in (20, 30):
            with self.subTest(elapsed=elapsed):
                target = [16]
                fan = FakeFan(["on", "auto", 12, 1200])
                original = fan.set_mode
                def change(mode):
                    original(mode)
                    target[0] = 0
                fan.set_mode = change
                schedule = MotorSchedule(last_decrease=self.clock.now - elapsed)
                result = fan_apply(fan, lambda: target[0], schedule)
                self.assertFalse(result["confirmed"])
                self.assertEqual([item for item in fan.writes if type(item) is int], [16])
                self.assertEqual(result["level"], 16)
                self.assertEqual(schedule.last_decrease, self.clock.now - elapsed)

    def test_final_expected_level_matches_raised_target_even_with_lost_ack(self):
        target = [10]
        fan = FakeFan(["on", "auto", 12, 1200])
        fan.lose_ack = True
        original = fan.set_mode
        def change(mode):
            original(mode)
            target[0] = 16
        fan.set_mode = change
        result = fan_apply(fan, lambda: target[0], self.schedule)
        self.assertFalse(result["confirmed"])
        self.assertEqual(self.schedule.expected_level, 16)
        self.assertEqual(fan.writes, ["favorite", 16])

    def test_blocking_power_and_mode_exceptions_stamp_completion(self):
        for method in ("on", "set_mode"):
            with self.subTest(method=method):
                self.clock.now = 100
                fan = FakeFan(["off" if method == "on" else "on", "auto", 12, 1200])
                schedule = MotorSchedule()
                original = getattr(fan, method)
                def fail(*args):
                    original(*args)
                    self.clock.now += 10
                    raise RuntimeError("lost acknowledgement")
                setattr(fan, method, fail)
                result = fan_apply(fan, lambda: 16, schedule)
                self.assertFalse(result["confirmed"])
                self.assertEqual(schedule.last_attempt, 110)
                self.clock.now = 117.99
                fan_apply(fan, lambda: 16, schedule)
                self.assertEqual(fan.writes, ["on"] if method == "on" else ["favorite"])

    def test_attempts_stamped_before_write_even_when_ack_lost(self):
        fan = FakeFan(["on", "favorite", 10, 1200])
        original = fan.set_favorite_level
        def write(level):
            self.assertEqual(self.schedule.last_attempt, self.clock.now)
            self.assertEqual(self.schedule.attempts, 1)
            original(level)
        fan.set_favorite_level = write
        fan.lose_ack = True
        result = fan_apply(fan, lambda: 16, self.schedule)
        self.assertFalse(result["confirmed"])
        self.assertIsNone(result["verified_at"])
        self.assertEqual(self.schedule.attempts, 1)
        self.clock.now += 1
        self.assertTrue(fan_apply(fan, lambda: 16, self.schedule)["confirmed"])
        self.assertEqual(fan.writes, [16])

    def test_two_unresolved_attempts_survive_new_targets_and_credentials(self):
        fan = FakeFan(["on", "favorite", 10, 1200])
        fan.ignore_writes, fan.lose_ack = True, True
        fan_apply(fan, lambda: 16, self.schedule)
        self.clock.now += 8
        result = fan_apply(fan, lambda: 15, self.schedule)
        self.assertTrue(result["fault"])
        replacement = FakeFan(["off", "auto", 10, 0])
        self.clock.now += 100
        self.assertEqual(fan_apply(replacement, lambda: 14, self.schedule)["execution"], "fault")
        self.assertEqual(replacement.writes, [])
        replacement.actual = ["on", "favorite", 14, 0]
        self.assertTrue(fan_apply(replacement, lambda: 14, self.schedule)["fault"])
        replacement.actual[3] = 1200
        self.assertTrue(fan_apply(replacement, lambda: 14, self.schedule)["confirmed"])
        self.assertFalse(self.schedule.fault)
        self.assertEqual(self.schedule.attempts, 0)
        self.assertEqual(replacement.writes, [])

    def test_ignored_writes_exhaust_budget(self):
        fan = FakeFan(["on", "favorite", 10, 1200])
        fan.ignore_writes = True
        fan_apply(fan, lambda: 16, self.schedule)
        self.clock.now += 8
        self.assertTrue(fan_apply(fan, lambda: 16, self.schedule)["fault"])
        self.clock.now += 100
        fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual(fan.writes, [16, 16])

    def test_missing_or_malformed_readback_never_writes(self):
        fan = FakeFan()
        fan.fail = True
        self.assertFalse(fan_apply(fan, lambda: 16, self.schedule)["confirmed"])
        fan.fail = False
        for actual in ([], ["on", "auto", True, 10], ["unknown", "favorite", 10, 100],
                       ["on", "favorite", 10, float("nan")], ["on", "favorite", 10, -1]):
            fan.actual = actual
            self.assertFalse(fan_apply(fan, lambda: 16, self.schedule)["confirmed"])
        self.assertEqual(fan.writes, [])
        self.assertEqual(self.schedule.attempts, 0)

    def test_observed_state_is_unclamped(self):
        fan = FakeFan(["on", "favorite", 25, 6500])
        result = fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual((result["level"], result["rpm"]), (25, 6500))
        self.assertEqual(fan.writes, [])

    def test_actual_decrease_one_step_and_thirty_seconds(self):
        fan = FakeFan()
        result = fan_apply(fan, lambda: 10, self.schedule)
        self.assertEqual((result["level"], fan.writes), (15, [15]))
        self.clock.now += 29.99
        fan_apply(fan, lambda: 10, self.schedule)
        self.assertEqual(fan.writes, [15])
        self.clock.now += .01
        fan_apply(fan, lambda: 10, self.schedule)
        self.assertEqual(fan.writes, [15, 14])
        fan.actual[2] = 12
        self.clock.now += 30
        fan_apply(fan, lambda: 0, self.schedule)
        self.assertEqual(fan.writes, [15, 14, 11])

    def test_decrease_ack_loss_stamps_actual_attempt_time(self):
        fan = FakeFan()
        fan.lose_ack = True
        fan_apply(fan, lambda: 10, self.schedule)
        self.assertEqual(self.schedule.last_decrease, 100)
        self.clock.now += 8
        fan_apply(fan, lambda: 10, self.schedule)
        self.assertEqual(fan.writes, [15])

    def test_local_zero_and_final_integer_range(self):
        fan = FakeFan(["on", "favorite", 1, 200])
        self.assertTrue(fan_apply(fan, lambda: 0, self.schedule)["confirmed"])
        self.assertEqual(fan.writes, [0])
        for invalid in (True, 1.0, -1, 17, None):
            with self.assertRaises(ValueError):
                fan_apply(fan, lambda: invalid, self.schedule)
        changing = iter([10, 10, 17])
        with self.assertRaises(ValueError):
            fan_apply(FakeFan(), lambda: next(changing), MotorSchedule())

    def test_shutdown_request_has_no_spacing_or_fault_bypass(self):
        fan = FakeFan(["on", "favorite", 10, 1200])
        self.schedule.last_attempt = self.clock.now
        fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual(fan.writes, [])
        self.schedule.fault = True
        self.clock.now += 100
        fan_apply(fan, lambda: 16, self.schedule)
        self.assertEqual(fan.writes, [])

    def test_settings_type_boundaries(self):
        with self.assertRaises(ValueError):
            settings_checked({"retention_days": True, "max_log_mb": 256, "manual_level": 16})
        with self.assertRaises(ValueError):
            settings_checked({"retention_days": 3, "max_log_mb": 1, "manual_level": 16})


@unittest.skipUnless(os.name == "posix", "Native Linux durable file and directory sync required")
class MotorDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path, self.token = self.root / "motor-budget.json", self.root / "device-token"
        self.clock = FakeClock()
        self.clock.now = 70
        self.timer = patch("service.time.monotonic", self.clock)
        self.timer.start()

    def tearDown(self):
        self.timer.stop()
        self.temp.cleanup()

    def load(self):
        return MotorSchedule.load(self.path, self.token)

    def test_exhaustion_100_108_111_survives_restart_and_false_fault_bit(self):
        schedule = self.load()
        fan = FakeFan(["on", "favorite", 10, 1200])
        fan.ignore_writes, fan.lose_ack = True, True
        self.clock.now = 100
        fan_apply(fan, lambda: 16, schedule)
        self.clock.now = 108
        fan_apply(fan, lambda: 16, schedule)
        record = json.loads(self.path.read_text())
        self.assertEqual(set(record), {"version", "identity", "attempts", "expected_level", "fault"})
        self.assertEqual(record["identity"], MOTOR_IDENTITY)
        self.assertEqual(record["attempts"], 2)
        record["fault"] = False
        self.path.write_text(json.dumps(record))
        self.clock.now = 111
        restored = self.load()
        self.assertEqual((restored.attempts, restored.fault), (2, True))
        self.assertEqual(fan_apply(fan, lambda: 16, restored)["execution"], "fault")
        self.assertEqual(fan.writes, [16, 16])

    def test_one_attempt_restart_imposes_full_eight_and_thirty_second_windows(self):
        schedule = self.load()
        self.assertTrue(schedule.persist_debt_then_set_budget(1, 16))
        self.clock.now = 103
        restored = self.load()
        fan = FakeFan(["on", "favorite", 10, 1200])
        self.clock.now = 110.99
        fan_apply(fan, lambda: 16, restored)
        self.assertEqual(fan.writes, [])
        self.clock.now = 111
        fan_apply(fan, lambda: 0, restored)
        self.assertEqual(fan.writes, [])
        self.assertEqual(restored.attempts, 1)
        self.clock.now = 132.99
        fan_apply(fan, lambda: 0, restored)
        self.assertEqual(fan.writes, [])
        self.clock.now = 133
        fan_apply(fan, lambda: 0, restored)
        self.assertEqual(fan.writes, [9])

    def test_interrupted_reservations_consume_budget_without_any_physical_write(self):
        schedule = self.load()
        self.assertTrue(schedule.persist_debt_then_set_budget(1, 16))
        restored = self.load()
        self.assertEqual(restored.attempts, 1)
        self.assertTrue(restored.persist_debt_then_set_budget(2, 16, True))
        exhausted = self.load()
        self.clock.now += 1000
        fan = FakeFan(["on", "favorite", 10, 1200])
        self.assertTrue(fan_apply(fan, lambda: 16, exhausted)["fault"])
        self.assertEqual(fan.writes, [])

    def test_wall_clock_jumps_never_replenish_or_shorten_restart_windows(self):
        schedule = self.load()
        schedule.persist_debt_then_set_budget(1, 16)
        for wall in (-1e12, 1e12):
            with patch("service.time.time", return_value=wall):
                restored = self.load()
                fan = FakeFan(["on", "favorite", 10, 1200])
                fan_apply(fan, lambda: 16, restored)
                self.assertEqual(fan.writes, [])
                self.assertEqual(restored.attempts, 1)
                self.assertEqual(restored.last_attempt, self.clock.now)
                self.assertEqual(restored.last_decrease, self.clock.now)

    def test_missing_budget_with_token_or_dangling_entries_never_seeds(self):
        self.token.write_text("a1" * 16)
        self.assertTrue(self.load().fault)
        self.assertFalse(self.path.exists())
        self.token.unlink()
        self.token.symlink_to(self.root / "missing-token")
        self.assertTrue(self.load().fault)
        self.assertFalse(self.path.exists())
        self.token.unlink()
        self.path.symlink_to(self.root / "missing-budget")
        self.assertTrue(self.load().fault)

    def test_corrupt_mismatched_duplicate_and_unsafe_budget_remains_read_only(self):
        valid = {"version": 1, "identity": MOTOR_IDENTITY, "attempts": 1, "expected_level": 16, "fault": False}
        cases = [b"broken", json.dumps({**valid, "identity": {}}).encode(),
                 json.dumps({**valid, "attempts": True}).encode(), json.dumps({**valid, "expected_level": 17}).encode(),
                 b'{"version":1,"version":1}', b'[' * 2000 + b']' * 2000, b'x' * 4097]
        for raw in cases:
            self.path.write_bytes(raw)
            self.path.chmod(0o600)
            restored = self.load()
            self.assertTrue(restored.fault)
            self.assertTrue(restored.persistence_error)
            fan = FakeFan(["on", "favorite", 10, 1200])
            self.assertTrue(fan_apply(fan, lambda: 10, restored)["fault"])
            self.assertEqual(fan.writes, [])
        self.path.write_text(json.dumps(valid))
        self.path.chmod(0o666)
        self.assertTrue(self.load().fault)
        self.path.chmod(0o600)
        with patch("service.os.open", side_effect=PermissionError("private path")):
            self.assertTrue(self.load().fault)

    def test_seed_and_absence_permission_failures_never_grant_fresh_budget(self):
        with patch("service.atomic_write", side_effect=OSError("private storage")):
            self.assertTrue(self.load().fault)
        self.assertFalse(self.path.exists())
        original = Path.lstat
        def lstat(path):
            if path == self.token:
                raise PermissionError("private token entry")
            return original(path)
        with patch("service.Path.lstat", lstat), patch("service.atomic_write") as writer:
            self.assertTrue(self.load().fault)
            writer.assert_not_called()

    def test_reservation_and_directory_sync_failures_issue_no_commands(self):
        schedule = self.load()
        self.clock.now = 100
        for failure in (patch("service.atomic_write", side_effect=OSError("private storage")),
                        patch("service.fsync_directory", side_effect=OSError("private directory"))):
            schedule = self.load()
            self.clock.now += 30
            fan = FakeFan(["off", "auto", 10, 1200])
            with failure:
                result = fan_apply(fan, lambda: 16, schedule)
            self.assertEqual(fan.writes, [])
            self.assertTrue(result["fault"])
            self.assertFalse(result["confirmed"])
            self.assertIsNone(result["verified_at"])
            self.assertNotIn("private", result["error"])

    def test_failed_recovery_preserves_exhaustion_even_after_replacement_before_dir_sync(self):
        for failure in (patch("service.atomic_write", side_effect=OSError("storage")),
                        patch("service.fsync_directory", side_effect=OSError("directory sync"))):
            schedule = self.load()
            schedule.persist_debt_then_set_budget(2, 16, True)
            fan = FakeFan()
            with failure:
                result = fan_apply(fan, lambda: 16, schedule)
            self.assertTrue(result["fault"])
            self.assertFalse(result["confirmed"])
            self.assertIsNone(result["verified_at"])
            self.assertEqual(schedule.attempts, 2)
            self.assertEqual(self.load().attempts, 2)
            self.assertEqual(fan.writes, [])
        self.assertTrue(fan_apply(fan, lambda: 16, schedule)["confirmed"])
        self.assertEqual(schedule.attempts, 0)
        self.assertEqual(json.loads(self.path.read_text())["attempts"], 2)

    def test_recovery_sync_and_compensating_write_failure_never_allow_third_command(self):
        schedule = self.load()
        fan = FakeFan(["on", "favorite", 10, 1200])
        fan.ignore_writes = True
        for now in (100, 108):
            self.clock.now = now
            fan_apply(fan, lambda: 16, schedule)
        self.assertEqual(fan.writes, [16, 16])
        fan.actual[2] = 16
        write = atomic_write
        replacements = []
        def persist(path, text):
            replacements.append(json.loads(text))
            if len(replacements) > 1:
                raise OSError("compensating write failed")
            write(path, text)
        with patch("service.atomic_write", persist), patch("service.fsync_directory", side_effect=OSError("directory sync failed")):
            result = fan_apply(fan, lambda: 16, schedule)
        self.assertEqual(len(replacements), 2)
        self.assertTrue(result["fault"])
        self.assertFalse(result["confirmed"])
        self.assertIsNone(result["verified_at"])
        self.assertEqual(schedule.attempts, 2)
        self.clock.now = 111
        restored = self.load()
        self.assertEqual((restored.attempts, restored.fault), (2, True))
        fan.actual[2] = 10
        for now in (119, 141):
            self.clock.now = now
            result = fan_apply(fan, lambda: 16, restored)
            self.assertEqual(result["execution"], "fault")
            self.assertFalse(result["confirmed"])
            self.assertEqual(fan.writes, [16, 16])

    def test_successful_recovery_retains_disk_debt_until_new_reservation(self):
        schedule = self.load()
        self.assertTrue(schedule.persist_debt_then_set_budget(2, 16, True))
        fan = FakeFan()
        self.assertTrue(fan_apply(fan, lambda: 16, schedule)["confirmed"])
        self.assertEqual((schedule.attempts, schedule.expected_level, schedule.fault), (0, None, False))
        record = json.loads(self.path.read_text())
        self.assertEqual((record["attempts"], record["expected_level"], record["fault"]), (2, 16, True))
        restored = self.load()
        fan.actual[2] = 10
        self.clock.now += 30
        self.assertEqual(fan_apply(fan, lambda: 16, restored)["execution"], "fault")
        self.assertEqual(fan.writes, [])
        fan.actual[2] = 16
        self.assertTrue(fan_apply(fan, lambda: 16, restored)["confirmed"])
        self.assertEqual(restored.attempts, 0)
        self.assertEqual(json.loads(self.path.read_text())["attempts"], 2)
        fan.ignore_writes = True
        fan_apply(fan, lambda: 10, restored)
        self.assertEqual(fan.writes, [15])
        record = json.loads(self.path.read_text())
        self.assertEqual((record["attempts"], record["expected_level"], record["fault"]), (1, 15, False))
        self.assertEqual(self.load().attempts, 1)

    def test_recovery_preserves_one_attempt_and_unknown_expected_debt(self):
        for attempts, expected, fault in ((1, 15, False), (2, None, False)):
            with self.subTest(attempts=attempts):
                schedule = MotorSchedule(attempts=attempts, expected_level=expected, fault=fault, budget_path=self.path)
                self.assertTrue(fan_apply(FakeFan(), lambda: 16, schedule)["confirmed"])
                self.assertEqual((schedule.attempts, schedule.expected_level, schedule.fault), (0, None, False))
                restored = self.load()
                self.assertEqual((restored.attempts, restored.expected_level, restored.fault),
                                 (attempts, expected if expected is not None else 16, attempts == 2))

    def test_reservations_and_raised_expected_level_are_durable_before_commands(self):
        schedule = self.load()
        self.clock.now = 100
        target = [10]
        fan = FakeFan(["off", "auto", 12, 1200])
        write = atomic_write
        writes = []
        def persist(path, text):
            record = json.loads(text)
            writes.append((record["attempts"], record["expected_level"]))
            write(path, text)
            if record["attempts"] and record["expected_level"] == 11:
                target[0] = 14
            elif record["attempts"] and record["expected_level"] == 14:
                target[0] = 16
            elif record["attempts"] and record["expected_level"] == 16:
                target[0] = 0
        power = fan.on
        gear = fan.set_favorite_level
        def on():
            self.assertEqual(json.loads(self.path.read_text())["attempts"], 1)
            power()
        def level(value):
            self.assertEqual(value, 16)
            self.assertEqual(json.loads(self.path.read_text())["expected_level"], value)
            gear(value)
        fan.on, fan.set_favorite_level = on, level
        with patch("service.atomic_write", persist):
            result = fan_apply(fan, lambda: target[0], schedule)
        self.assertFalse(result["confirmed"])
        self.assertEqual(fan.writes, ["on", "favorite", 16])
        self.assertEqual(writes[:3], [(1, 11), (1, 14), (1, 16)])

    def test_downward_eligibility_rechecked_after_blocking_persistence(self):
        schedule = self.load()
        self.clock.now = 100
        fan = FakeFan()
        write = atomic_write
        def persist(path, text):
            write(path, text)
            if json.loads(text)["attempts"]:
                schedule.last_decrease = self.clock.now
        with patch("service.atomic_write", persist):
            fan_apply(fan, lambda: 10, schedule)
        self.assertEqual(fan.writes, [])

    def test_blocking_mutation_completion_and_exception_start_full_windows(self):
        for lost_ack in (False, True):
            schedule = MotorSchedule()
            self.clock.now = 100
            fan = FakeFan()
            original = fan.set_favorite_level
            def delayed(level):
                self.clock.now += 20
                original(level)
                if lost_ack:
                    raise RuntimeError("lost acknowledgement")
            fan.set_favorite_level = delayed
            fan_apply(fan, lambda: 10, schedule)
            self.assertEqual((schedule.last_attempt, schedule.last_decrease), (120, 120))
            self.clock.now = 127.99
            fan_apply(fan, lambda: 16, schedule)
            self.assertEqual(fan.writes, [15])
            self.clock.now = 149.99
            fan_apply(fan, lambda: 10, schedule)
            self.assertEqual(fan.writes, [15])
            self.clock.now = 150
            fan_apply(fan, lambda: 10, schedule)
            self.assertEqual(fan.writes, [15, 14])


class BoundaryTests(unittest.TestCase):
    def test_peer_comes_only_from_transport(self):
        for ip, expected in (("127.0.0.1", (True, True)), ("::1", (True, True)),
                             ("192.168.3.99", (True, False)), ("10.0.0.1", (False, False)),
                             ("::ffff:192.168.3.24", (False, False)), ("invalid", (False, False))):
            transport = SimpleNamespace(get_extra_info=lambda name: (ip, 1000))
            request = SimpleNamespace(transport=transport, remote="192.168.3.24",
                                      headers={"X-Forwarded-For": "192.168.3.24"})
            self.assertEqual(trusted_peer(request), expected)
        self.assertEqual(trusted_peer(SimpleNamespace(transport=None)), (False, False))

    def test_fixed_identity_and_token_shape(self):
        info = SimpleNamespace(model=DEVICE_MODEL, mac_address=DEVICE_MAC)
        device = SimpleNamespace(device_id=DEVICE_ID, info=lambda **kwargs: info)
        with patch("service.AirPurifier", return_value=device) as factory:
            self.assertIs(device_from_token("a1" * 16), device)
            self.assertEqual(factory.call_args.kwargs["ip"], DEVICE_IP)
            self.assertEqual(device.retry_count, 0)
            for field, bad in (("model", "other.model"), ("mac_address", "00:00:00:00:00:00")):
                original = getattr(info, field)
                setattr(info, field, bad)
                with self.assertRaises(ValueError):
                    device_from_token("a1" * 16)
                setattr(info, field, original)
            device.device_id = 1
            with self.assertRaises(ValueError):
                device_from_token("a1" * 16)
            factory.reset_mock()
            for bad in (None, "0" * 32, "f" * 32, "x" * 32, "a1" * 15):
                with self.assertRaises(ValueError):
                    device_from_token(bad)
            factory.assert_not_called()

    def test_html_callers_and_sole_qr_entry(self):
        import re
        root = Path(__file__).parent
        html, script = (root / "ui.html").read_text(encoding="utf-8"), (root / "ui.js").read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([^"]+)"', html))
        self.assertFalse(set(re.findall(r"\$\('([^']+)'\)", script)) - ids)
        for retired in ("/api/scan", "/api/device-token", "/api/xiaomi/devices", "/api/xiaomi/bind", "/api/ha-certificate.cer"):
            self.assertNotIn(retired, html + script)
        self.assertIn('id="qr-devices"', html)
        self.assertIn("/api/xiaomi/qr/bind", script)
        self.assertNotIn("X-Admin-Key", script)

    @unittest.skipUnless(shutil.which("node"), "Node is optional for offline UI interaction checks")
    def test_settings_ui_confirmation_and_failure_offline(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.events = {}; this.value = ''; this.disabled = false; this.checked = false; this.style = {}; this.children = []; }
  addEventListener(name, handler) { this.events[name] = handler; }
  querySelectorAll() { return []; }
  replaceChildren(...children) { this.children = children; this.value = ''; }
  append(child) { this.children.push(child); }
}
const elements = new Map([...fs.readFileSync('ui.html', 'utf8').matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
elements.get('hours').value = '1';
const get = id => elements.get(id);
const status = {controller_age_seconds:0, server_time:100, updated_at:100, nodes:[], requested_level:16,
  settings:{manual_level:16, retention_days:3, max_log_mb:8}, decision:{reasons:[]}, fan:{configured:false},
  log_bytes:0, dropped_records:0};
let saveMode = 'success', finishSave;
const calls = [];
const context = {document:{getElementById:get, querySelectorAll:()=>[], createElement:()=>new Element()},
  window:{addEventListener(){}},
  Option:function(text, value) { this.textContent = text; this.value = value; },
  setTimeout:()=>{}, setInterval:()=>{}, AbortSignal:{timeout:()=>({})},
  fetch:async(path, options)=>{
    calls.push({path, options});
    if(path === '/api/status') return {ok:true, json:async()=>status};
    if(path.startsWith('/api/history')) return {ok:true, json:async()=>({rows:[]})};
    if(path === '/api/settings') {
      if(saveMode === 'timeout') throw Object.assign(Error('timeout'), {name:'TimeoutError'});
      if(saveMode === 'wait') return new Promise(resolve=>{finishSave=()=>resolve({ok:true, json:async()=>({settings:JSON.parse(options.body), maintenance_error:''})});});
      return {ok:true, json:async()=>({settings:JSON.parse(options.body), maintenance_error:''})};
    }
    throw Error('unexpected request ' + path);
  }};
vm.runInNewContext(fs.readFileSync('ui.js', 'utf8'), context);
(async()=>{
  for(let index=0; index<10; index++) await Promise.resolve();
  const event = {preventDefault(){}};
  const controls = ['retention', 'capacity', 'control-mode', 'settings-save', 'settings-cancel'];
  assert.equal(get('control-mode').children.length, 18);
  assert.deepEqual(Array.from(get('control-mode').children, option=>option.value), ['automatic', ...Array.from({length:17}, (_,level)=>String(level))]);
  const saves = () => calls.filter(call=>call.path === '/api/settings').length;
  get('control-mode').value = '0';
  get('control-mode').events.input();
  const before = saves();
  status.settings.manual_level = 7;
  context.statusSnapshot = status;
  vm.runInNewContext('render(statusSnapshot)', context);
  assert.equal(get('control-mode').value, '0');
  get('settings-cancel').events.click();
  assert.equal(get('control-mode').value, '7');
  assert.equal(saves(), before);
  get('control-mode').value = '0';
  get('retention').value = '3';
  get('capacity').value = '2048';
  get('control-mode').events.input();
  saveMode = 'wait';
  const saving = get('settings-form').events.submit(event);
  await Promise.resolve();
  controls.forEach(id=>assert.equal(get(id).disabled, true));
  finishSave();
  await saving;
  controls.forEach(id=>assert.equal(get(id).disabled, false));
  assert.deepEqual(JSON.parse(calls.filter(call=>call.path === '/api/settings').at(-1).options.body), {retention_days:3, max_log_mb:2048, manual_level:0});
  assert.match(get('settings-result').textContent, /设置已保存/);
  assert.match(get('settings-result').textContent, /尚需/);
  status.settings_pending = true;
  saveMode = 'timeout';
  await get('settings-form').events.submit(event);
  assert.match(get('settings-result').textContent, /未知/);
  get('control-mode').value = '2';
  get('control-mode').events.input();
  status.settings_pending = false;
  status.settings.manual_level = 5;
  vm.runInNewContext('render(statusSnapshot)', context);
  assert.equal(get('control-mode').value, '2');
  assert.match(get('settings-result').textContent, /新草稿仍未保存/);
  assert.match(get('policy').textContent, /手动 5 档/);
  get('settings-cancel').events.click();
  assert.equal(get('control-mode').value, '5');
  assert.equal(get('control-mode').children.length, 18);
  status.requested_level = 5;
  status.fan = {configured:true, confirmed:true, level:5, power:'on', mode:'favorite', verified_at:100, rpm:0, execution:'matched'};
  for(const rpm of [0, NaN, Infinity, -1]) {
    status.fan.rpm = rpm;
    vm.runInNewContext('render(statusSnapshot)', context);
    assert.notEqual(get('execution').textContent, '实际匹配');
  }
  status.fan.rpm = 1200;
  vm.runInNewContext('render(statusSnapshot)', context);
  assert.equal(get('execution').textContent, '实际匹配');
  for(const verified of [90, 101, null]) {
    status.fan.verified_at = verified;
    vm.runInNewContext('render(statusSnapshot)', context);
    assert.notEqual(get('execution').textContent, '实际匹配');
  }
})().catch(error=>{console.error(error); process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).parent,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_drains_tasks_and_closes_pools_after_settings_body_failure(self):
        for failure in (ValueError, ConnectionResetError):
            with self.subTest(failure=failure.__name__), tempfile.TemporaryDirectory() as directory:
                app = await create_app(Path(directory), {"http://127.0.0.1:8910"})
                state = app[STATE]
                entered, release = asyncio.Event(), asyncio.Event()
                tasks_release, tasks_drained = asyncio.Event(), asyncio.Event()
                final_attempts = []
                state["device"] = object()

                async def body():
                    entered.set()
                    await release.wait()
                    if failure is ConnectionResetError:
                        raise ConnectionResetError("settings body disconnected")
                    return {}

                async def running():
                    await tasks_release.wait()
                    tasks_drained.set()

                def final_fan(device, target, schedule, *, read_only=None):
                    self.assertTrue(tasks_drained.is_set())
                    self.assertTrue(state["fan_lock"].locked())
                    self.assertIs(device, state["device"])
                    self.assertIs(schedule, state["motor_schedule"])
                    final_attempts.append(target())
                    return {}

                handler = next(route.handler for route in app.router.routes()
                               if route.method == "POST" and route.resource.canonical == "/api/settings")
                running_task = asyncio.create_task(running())
                state["tasks"] = [running_task]
                saving = asyncio.create_task(handler(SimpleNamespace(json=body)))
                stopping = None
                with patch("service.fan_apply", final_fan):
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        owner = state["settings_task"]
                        stopping = asyncio.create_task(app.on_cleanup[-1](app))
                        await asyncio.sleep(0)
                        await asyncio.sleep(0)
                        self.assertTrue(state["stop"])
                        self.assertTrue(state["force_max"])
                        self.assertFalse(stopping.done())
                        self.assertIs(state["settings_task"], owner)
                        release.set()
                        with self.assertRaises(failure):
                            await saving
                        self.assertFalse(stopping.done())
                        tasks_release.set()
                        await asyncio.wait_for(stopping, 3)
                        self.assertTrue(owner.done())
                        self.assertIsNone(state["settings_task"])
                        self.assertTrue(running_task.done())
                        self.assertTrue(tasks_drained.is_set())
                        self.assertEqual(final_attempts, [16])
                        for key in ("disk",):
                            with self.assertRaises(RuntimeError):
                                state[key].submit(lambda: None)
                    finally:
                        release.set()
                        tasks_release.set()
                        pending = [saving, running_task] + ([stopping] if stopping is not None else [])
                        await asyncio.gather(*pending, return_exceptions=True)
                        for key in ("disk",):
                            state[key].shutdown(wait=True, cancel_futures=True)


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.origin = "http://127.0.0.1:8910"
        self.app = await create_app(self.root, {self.origin})
        self.startup = self.app.on_startup[-1]
        self.app.on_startup.clear()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"Host": "127.0.0.1:8910", "Origin": self.origin}

    async def asyncTearDown(self):
        self.app[STATE]["device"] = None
        await self.client.close()
        self.temp.cleanup()

    def handler(self, path, method="POST"):
        return next(route.handler for route in self.app.router.routes()
                    if route.method == method and route.resource.canonical == path)

    async def test_status_distinguishes_saved_auto_fresh_control_and_committed_history(self):
        state = self.app[STATE]
        state['settings']['manual_level'] = None
        response = await self.client.get('/api/status', headers=self.headers)
        status = await response.json()
        self.assertEqual(status['control_mode'], 'auto')
        self.assertFalse(status['control_active'])
        self.assertEqual(status['history_recording']['writes'], 0)
        state.update(updated_at=time.time(), last_poll=time.monotonic())
        response = await self.client.get('/api/status', headers=self.headers)
        status = await response.json()
        self.assertTrue(status['control_active'])
        self.assertFalse(status['read_only'])
        self.assertFalse(status['fan']['confirmed'])
        self.assertEqual(status['history_recording']['source'], 'control')
        state['last_poll'] = time.monotonic() - 9
        response = await self.client.get('/api/status', headers=self.headers)
        self.assertFalse((await response.json())['control_active'])

    async def test_failed_append_does_not_claim_committed_control_history(self):
        state = self.app[STATE]
        with patch('service.aiohttp.ClientSession', OfflineSession), patch.object(state['journal'], 'append', side_effect=OSError('storage')):
            await self.startup(self.app)
            deadline = time.monotonic() + 2
            while not state['history_recording']['failed_writes'] and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            self.assertEqual(state['history_recording']['writes'], 0)
            self.assertEqual(state['history_recording']['failed_writes'], 1)
            self.assertEqual(state['history_recording']['state'], 'failed')
            self.assertTrue(state['storage_error'])

    async def test_post_append_failure_preserves_real_committed_history_count(self):
        state = self.app[STATE]
        journal = state['journal']
        appended = False
        append, size = journal.append, journal.size
        def raw(row):
            nonlocal appended
            append(row)
            appended = True
        def measured():
            if appended:
                raise OSError('post-append maintenance')
            return size()
        with patch('service.aiohttp.ClientSession', OfflineSession), patch.object(journal, 'append', raw), patch.object(journal, 'size', measured):
            await self.startup(self.app)
            deadline = time.monotonic() + 2
            while not state['storage_error'] and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            self.assertEqual(state['history_recording']['writes'], 1)
            self.assertEqual(state['history_recording']['failed_writes'], 0)
            self.assertEqual(state['history_recording']['state'], 'recording')
            self.assertIsNone(state['history_recording']['error'])
            self.assertTrue(state['storage_error'])
            lines = [line for path in (self.root/'logs').glob('*.jsonl') for line in path.read_text(encoding='utf8').splitlines()]
            self.assertEqual(len(lines), 1)

    async def test_status_current_request_rejects_actual_r2_and_completed_worker_ack(self):
        rows = json.loads(Path(__file__).with_name(
            "test_fan_snapshot_transitions.json").read_text(encoding="utf-8"))["changes_and_final_rows"]
        state = self.app[STATE]
        state["settings"]["manual_level"] = None
        state["motor_schedule"] = MotorSchedule(attempts=1, expected_level=14,
                                               last_attempt=226790, last_decrease=226780)
        for raw in (rows[0], rows[-1]):
            for producer in ("raw", "completed-worker"):
                with self.subTest(requested=raw["requested"], producer=producer):
                    mono, wall = raw["fan"]["observed_mono"], raw["fan"]["verified_at"]
                    clock = SimpleNamespace(monotonic=lambda: mono, time=lambda: wall)
                    device = FakeFan(["on", "favorite", raw["fan"]["level"], raw["fan"]["rpm"], 296])
                    with patch("service.time", clock):
                        physical = deepcopy(raw["fan"]) if producer == "raw" else await asyncio.to_thread(
                            fan_apply, device, lambda: raw["fan"]["level"], MotorSchedule())
                        self.assertEqual((physical["execution"], physical["confirmed"]), ("matched", True))
                        mono, wall = raw["learning"]["mono"], raw["snapshot_at"]
                        state.update(fan=physical, decision=deepcopy(raw["decision"]), last_poll=mono, last_fan=mono)
                        before, debt = deepcopy(physical), deepcopy(vars(state["motor_schedule"]))
                        response = await self.client.get("/api/status", headers=self.headers)
                        self.assertEqual(response.status, 200)
                        result = await response.json()
                        self.assertEqual((result["requested_level"], result["fan"]["execution"],
                                          result["fan"]["confirmed"]), (raw["requested"], "pending", False))
                        self.assertEqual(result["fan"], dict(before, execution="pending", confirmed=False))
                        self.assertEqual(physical, before)
                        self.assertIs(state["fan"], physical)
                        self.assertEqual(vars(state["motor_schedule"]), debt)
                        self.assertEqual(result["decision"], raw["decision"])
                        mono += .001
                        fresh_device = FakeFan(["on", "favorite", raw["requested"], 2000, 296])
                        state["fan"] = await asyncio.to_thread(fan_apply, fresh_device,
                                                               lambda: raw["requested"], MotorSchedule())
                        response = await self.client.get("/api/status", headers=self.headers)
                        fresh = await response.json()
                        self.assertEqual((fresh["requested_level"], fresh["fan"]["level"],
                                          fresh["fan"]["execution"], fresh["fan"]["confirmed"]),
                                         (raw["requested"], raw["requested"], "matched", True))
                        self.assertEqual(device.writes + fresh_device.writes, [])

    async def test_monitor_current_request_projects_raw_row_and_recomputes_policy_admission(self):
        from service import HOSTS
        from core import normalize
        from learning import Learning, context_of

        self.app[STATE]['device'] = None
        await self.client.close()
        self.app = await create_app(self.root, {self.origin}, learning_enabled=True)
        self.startup = self.app.on_startup[-1]
        self.app.on_startup.clear()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

        rows = json.loads(Path(__file__).with_name(
            "test_fan_snapshot_transitions.json").read_text(encoding="utf-8"))["changes_and_final_rows"]
        state = self.app[STATE]
        state["settings"]["manual_level"] = None
        state["motor_schedule"] = MotorSchedule(attempts=1, expected_level=14,
                                               last_attempt=226790, last_decrease=226780)
        monitor = dict(zip(self.startup.__code__.co_freevars,
                           (cell.cell_contents for cell in self.startup.__closure__)))["monitor"]

        async def stop_after_row(delay):
            state["stop"] = True

        class Response:
            status = 200

            async def __aenter__(self):
                self.content = self
                return self

            async def __aexit__(self, *args):
                pass

            async def iter_chunked(self, size):
                yield json.dumps(payload).encode()

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def get(self, *args, **kwargs):
                return Response()

        for raw in (rows[0], rows[-1]):
            with self.subTest(requested=raw["requested"]):
                mono, wall = raw["learning"]["mono"], raw["snapshot_at"]
                payload = dict(timestamp=wall, cpu_percent=0, gpu_percent=0, system_power_w=35,
                    system_power_source="spbm", temperatures=dict(gpu=40,
                        acpi=[dict(zone="0", temp=45)], mlx=[dict(chip="mlx5", label="asic", temp=50)]))
                nodes = [normalize(host, payload, mono, wall) for host in HOSTS]
                policy = {HOSTS[0]: dict(version=14, threshold_w=75, source="spbm",
                    generated_at=wall, evidence_from=wall - 100, context=context_of(nodes))}
                physical, decision = deepcopy(raw["fan"]), deepcopy(raw["decision"])
                state.update(fan=physical, decision=dict(level=physical["level"], execution_trial=None),
                    nodes=nodes, stop=False, last_poll=mono, last_fan=mono, learning_wall=wall,
                    learning=dict(published=policy, settings_revision=state["settings_revision"], error=""))
                state["queue"] = asyncio.Queue()
                debt = deepcopy(vars(state["motor_schedule"]))
                with patch("service.time", SimpleNamespace(monotonic=lambda: mono, time=lambda: wall)), \
                        patch("service.aiohttp.ClientSession", side_effect=lambda **kw: Session()), \
                        patch("service.asyncio.sleep", stop_after_row), \
                        patch.object(state["controller"], "decide", return_value=decision) as decide:
                    await monitor()
                state["stop"] = False
                row = state["queue"].get_nowait()
                self.assertEqual(decide.call_args.kwargs["feedback"], physical)
                self.assertEqual(decide.call_args.kwargs["power_thresholds"], policy)
                self.assertEqual((row["requested_level"], row["fan"]["execution"], row["fan"]["confirmed"]),
                                 (raw["requested"], "pending", False))
                self.assertEqual(row["fan"], dict(physical, execution="pending", confirmed=False))
                self.assertIs(state["fan"], physical)
                self.assertEqual(state["fan"], raw["fan"])
                self.assertEqual(vars(state["motor_schedule"]), debt)
                self.assertEqual(row["decision"], raw["decision"])
                self.assertEqual(row["active_power_policy"], {})
                learner = Learning(HOSTS)
                learner.observe(row)
                self.assertEqual(learner.eligibility["reason"], "motor")
                state["journal"].append(row)
                stored = json.loads(state["journal"].files()[-1].read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(stored, row)

    async def prepare_qr(self, token="a1" * 16):
        previous = self.app[STATE]["qr"]
        await previous.close()
        cloud = FakeCloud()
        cloud.records = [{**TARGET, "localip": DEVICE_IP, "token": token}]
        connector = QRConnector(previous.pair, lambda: cloud)
        self.app[STATE]["qr"] = connector
        response = await self.client.post("/api/xiaomi/qr/start",
            json={"owner": OWNER, "consent": START_CONSENT}, headers=self.headers)
        self.assertEqual(response.status, 200)
        await connector.session.acquisition
        self.assertEqual(connector.session.phase, Phase.READY)
        return {"owner": OWNER, "consent": BIND_CONSENT, "did": str(DEVICE_ID)}

    async def test_bound_token_restores_in_fresh_app_without_qr_or_network(self):
        token = "a1" * 16
        body = await self.prepare_qr(token)
        state = self.app[STATE]
        schedule = state["motor_schedule"]
        self.assertTrue(schedule.persist_debt_then_set_budget(1, 16))
        schedule.last_attempt, schedule.last_decrease = 10, 20
        budget_path = self.root / "motor-budget.json"
        budget_before = budget_path.read_bytes()
        device = FakeFan()
        device.device_id = DEVICE_ID
        device.info = lambda **kwargs: SimpleNamespace(model=DEVICE_MODEL, mac_address=DEVICE_MAC)
        with patch("service.AirPurifier", return_value=device) as physical:
            response = await self.client.post("/api/xiaomi/qr/bind", json=body, headers=self.headers)
            self.assertEqual((await response.json())["phase"], "bound")
            physical.assert_called_once_with(ip=DEVICE_IP, token=token, timeout=1)
            self.assertIs(state["motor_schedule"], schedule)
            self.assertEqual((schedule.attempts, schedule.expected_level, schedule.last_attempt, schedule.last_decrease),
                             (1, 16, 10, 20))
            self.assertEqual(budget_path.read_bytes(), budget_before)
            token_path = self.root / "device-token"
            self.assertEqual(token_path.read_text(), token)
            if os.name == "posix":
                self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            state["device"] = None
            await self.client.close()
            restored = await create_app(self.root, {self.origin})
            fresh = restored[STATE]
            try:
                self.assertIsNot(fresh, state)
                self.assertIsNone(fresh["qr"].session)
                self.assertEqual((fresh["motor_schedule"].attempts, fresh["motor_schedule"].expected_level), (1, 16))
                self.assertEqual(budget_path.read_bytes(), budget_before)
                startup = restored.on_startup[-1]
                fan_loop = dict(zip(startup.__code__.co_freevars,
                                   (cell.cell_contents for cell in startup.__closure__)))["fan"]
                async def stop_after_readback(delay):
                    self.assertEqual(delay, 3)
                    fresh["stop"] = True
                with patch.object(fresh["qr"], "cloud_factory", side_effect=AssertionError("QR login on restart")) as qr, \
                        patch("service.aiohttp.ClientSession", side_effect=AssertionError("node network")) as network, \
                        patch("service.asyncio.sleep", side_effect=stop_after_readback):
                    await fan_loop()
                qr.assert_not_called()
                network.assert_not_called()
                self.assertIs(fresh["device"], device)
                self.assertTrue(fresh["fan"]["confirmed"])
                self.assertEqual(physical.call_count, 2)
                physical.assert_called_with(ip=DEVICE_IP, token=token, timeout=1)
                self.assertEqual(device.writes, [])
                self.assertIsNone(fresh["qr"].session)
                self.assertEqual(token_path.read_text(), token)
            finally:
                fresh["device"] = None
                await restored.on_cleanup[-1](restored)

    async def test_legacy_disk_is_not_rewritten_until_canonical_save(self):
        for legacy, level in ((True, 16), (False, None)):
            value = {"retention_days": 3, "max_log_mb": 2048, "full_speed": legacy}
            path = self.root / "settings.json"
            path.write_text(json.dumps(value))
            application = await create_app(self.root, {self.origin})
            try:
                self.assertEqual(json.loads(path.read_text()), value)
                self.assertEqual(application[STATE]["settings"], {"retention_days": 3, "max_log_mb": 2048, "manual_level": level})
            finally:
                await application.on_cleanup[-1](application)
        proposed = {"retention_days": 3, "max_log_mb": 2048, "manual_level": 0}
        response = await self.handler("/api/settings")(SimpleNamespace(json=AsyncMock(return_value=proposed)))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(path.read_text()), proposed)

    async def test_owned_save_repeated_cancel_overlap_and_commit_before_prune(self):
        state = self.app[STATE]
        handler = self.handler("/api/settings")
        proposed = {"retention_days": 3, "max_log_mb": 2048, "manual_level": 0}
        schedule = state["motor_schedule"]
        schedule.attempts, schedule.expected_level = 1, 16
        schedule.last_attempt, schedule.last_decrease = 10, 20
        entered, release = threading.Event(), threading.Event()
        order = []
        (self.root / "learning").mkdir(exist_ok=True)
        def write(path, text):
            order.append("commit" if path.name == "settings.json" else "learning_commit")
            atomic_write(path, text)
            entered.set()
            if not release.wait(3):
                raise AssertionError("save barrier release missing")
        def sync(path):
            order.append("sync" if path == self.root else "learning_sync")
        def prune(*args):
            self.assertEqual(json.loads((self.root / "settings.json").read_text()), proposed)
            self.assertEqual(order, ["commit", "sync"])
            order.append("prune")
        with patch("service.atomic_write", write), patch("service.fsync_directory", sync), patch.object(state["journal"], "prune", prune):
            first = asyncio.create_task(handler(SimpleNamespace(json=AsyncMock(return_value=proposed))))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                owned = state["settings_task"]
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(first.done())
                self.assertIs(state["settings_task"], owned)
                with self.assertRaises(web.HTTPConflict):
                    await handler(SimpleNamespace(json=AsyncMock(return_value={**proposed, "manual_level": 16})))
                snapshot = json.loads((await self.handler("/api/status", "GET")(None)).body)
                self.assertTrue(snapshot["settings_pending"])
                self.assertEqual(snapshot["requested_level"], 16)
                self.assertEqual(state["settings"]["manual_level"], 16)
                self.assertEqual(order, ["commit"])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await first
        self.assertEqual(order, ["commit", "sync", "prune"])
        self.assertIsNone(state["settings_task"])
        self.assertEqual(state["settings"], proposed)
        self.assertEqual(state["controller"].level, 16)
        self.assertIs(state["motor_schedule"], schedule)
        self.assertEqual((schedule.attempts, schedule.last_attempt, schedule.last_decrease), (1, 10, 20))

    async def test_save_ownership_installed_before_request_body_await(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def body():
            entered.set()
            await release.wait()
            return {"retention_days": 3, "max_log_mb": 2048, "manual_level": None}
        handler = self.handler("/api/settings")
        first = asyncio.create_task(handler(SimpleNamespace(json=body)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaises(web.HTTPConflict):
                await handler(SimpleNamespace(json=AsyncMock(return_value={})))
        finally:
            release.set()
            self.assertEqual((await first).status, 200)

    async def test_commit_failure_reconciles_validated_disk_and_latches_until_reliable_save(self):
        state = self.app[STATE]
        handler = self.handler("/api/settings")
        original = dict(state["settings"])
        proposed = {**original, "manual_level": 0}
        path = self.root / "settings.json"
        path.write_text(json.dumps(original))
        for failure, expected in ((patch("service.atomic_write", side_effect=OSError("private failure")), original),
                                  (patch("service.fsync_directory", side_effect=OSError("private sync")), proposed)):
            with failure, patch.object(state["journal"], "prune") as prune:
                response = await handler(SimpleNamespace(json=AsyncMock(return_value=proposed)))
                self.assertEqual(response.status, 503)
                self.assertEqual(state["settings"], expected)
                self.assertTrue(json.loads(response.body)["settings_known"])
                self.assertTrue(state["settings_error"])
                self.assertNotIn("private", response.text)
                prune.assert_not_called()
            state["storage_error"] = ""
            snapshot = json.loads((await self.handler("/api/status", "GET")(None)).body)
            self.assertTrue(snapshot["settings_error"])
            self.assertEqual(snapshot["requested_level"], 16)
        self.assertEqual((await handler(SimpleNamespace(json=AsyncMock(return_value=proposed)))).status, 200)
        self.assertFalse(state["settings_error"])

    async def test_unknown_invalid_disk_and_legacy_reread_do_not_fabricate_outcome(self):
        state = self.app[STATE]
        handler = self.handler("/api/settings")
        original = dict(state["settings"])
        path = self.root / "settings.json"
        proposed = {**original, "manual_level": 0}
        for disk in (None, "broken", json.dumps({"retention_days": 3, "max_log_mb": 2048, "full_speed": False})):
            if path.exists():
                path.unlink()
            if disk is not None:
                path.write_text(disk)
            state["settings"] = original
            with patch("service.atomic_write", side_effect=OSError), patch.object(state["journal"], "prune") as prune:
                response = await handler(SimpleNamespace(json=AsyncMock(return_value=proposed)))
            body = json.loads(response.body)
            self.assertEqual(response.status, 503)
            self.assertEqual(body["settings_known"], disk is not None and disk != "broken")
            self.assertEqual(state["settings"]["manual_level"], None if body["settings_known"] else 16)
            self.assertTrue(state["settings_error"])
            prune.assert_not_called()

    async def test_prune_failure_reports_committed_settings_and_partial_maintenance(self):
        state = self.app[STATE]
        proposed = {"retention_days": 3, "max_log_mb": 2048, "manual_level": 5}
        with patch.object(state["journal"], "prune", side_effect=OSError("private prune")):
            response = await self.handler("/api/settings")(SimpleNamespace(json=AsyncMock(return_value=proposed)))
        body = json.loads(response.body)
        self.assertEqual(response.status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["settings"], proposed)
        self.assertTrue(body["maintenance_error"])
        self.assertNotIn("private", response.text)
        self.assertEqual(json.loads((self.root / "settings.json").read_text()), proposed)
        self.assertEqual(state["settings"], proposed)
        self.assertFalse(state["settings_error"])
        self.assertTrue(state["storage_error"])

    async def test_shutdown_drains_save_despite_repeated_cancellation_before_disk_release(self):
        state = self.app[STATE]
        entered, release = threading.Event(), threading.Event()
        proposed = {"retention_days": 3, "max_log_mb": 2048, "manual_level": 0}
        def write(path, text):
            entered.set()
            if not release.wait(3):
                raise AssertionError("shutdown barrier release missing")
            atomic_write(path, text)
        with patch("service.atomic_write", write):
            saving = asyncio.create_task(self.handler("/api/settings")(SimpleNamespace(json=AsyncMock(return_value=proposed))))
            stopping = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                stopping = asyncio.create_task(self.app.on_cleanup[-1](self.app))
                await asyncio.sleep(0)
                stopping.cancel()
                await asyncio.sleep(0)
                stopping.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(stopping.done())
                self.assertIsNotNone(state["settings_task"])
                probe = state["disk"].submit(lambda: 1)
                self.assertFalse(probe.done())
            finally:
                release.set()
                self.assertEqual((await saving).status, 200)
                if stopping is not None:
                    with self.assertRaises(asyncio.CancelledError):
                        await stopping
        self.assertEqual(state["settings"], proposed)
        self.assertIsNone(state["settings_task"])
        with self.assertRaises(RuntimeError):
            state["disk"].submit(lambda: None)

    async def test_collection_and_fan_continue_during_cancelled_settings_save(self):
        state = self.app[STATE]
        state["device"] = FakeFan()
        state["last_identity"] = time.monotonic()
        schedule = state["motor_schedule"]
        entered, release = threading.Event(), threading.Event()
        proposed = {"retention_days": 3, "max_log_mb": 2048, "manual_level": 0}
        def write(path, text):
            entered.set()
            if not release.wait(6):
                raise AssertionError("runtime save release missing")
            atomic_write(path, text)
        with patch("service.aiohttp.ClientSession", OfflineSession), patch("service.notify"), patch("service.atomic_write", write):
            await self.startup(self.app)
            saving = asyncio.create_task(self.handler("/api/settings")(SimpleNamespace(json=AsyncMock(return_value=proposed))))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                first_poll, first_fan = state["last_poll"], state["last_fan"]
                saving.cancel()
                await asyncio.sleep(0)
                saving.cancel()
                await asyncio.sleep(3.15)
                self.assertGreater(state["last_poll"], first_poll)
                self.assertGreater(state["last_fan"], first_fan)
                self.assertEqual(len(state["nodes"]), 4)
                self.assertIsNotNone(state["settings_task"])
                self.assertEqual(state["decision"]["level"], 16)
                self.assertIs(state["motor_schedule"], schedule)
                self.assertEqual(state["device"].writes, [])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await saving
                state["stop"] = True
                await asyncio.gather(*state["tasks"])
        self.assertEqual(state["settings"], proposed)
        state["device"] = None

    async def test_monitor_passes_every_saved_manual_minimum_and_automatic_zero_with_owned_snapshot(self):
        from core import Controller
        from service import HOSTS
        state = self.app[STATE]
        monitor = dict(zip(self.startup.__code__.co_freevars, (cell.cell_contents for cell in self.startup.__closure__)))["monitor"]
        async def stop_after_sample(delay):
            state["stop"] = True
        for level in [None] + list(range(17)):
            state["stop"] = False
            state["queue"] = asyncio.Queue(maxsize=2)
            state["settings"]["manual_level"] = level
            state["controller"] = Controller(HOSTS)
            state["fan"] = {"configured": True, "fault": False, "power": "on", "mode": "favorite",
                            "rpm": 1000, "level": 16, "verified_at": time.time(),
                            "observed_mono": time.monotonic(), "confirmed": True, "execution": "matched"}
            state["last_fan"] = time.monotonic()
            with patch("service.aiohttp.ClientSession", OfflineSession), patch("service.asyncio.sleep", stop_after_sample), \
                    patch.object(state["controller"], "decide", wraps=state["controller"].decide) as decide:
                await monitor()
            self.assertEqual(decide.call_args.kwargs["manual_level"], level)
            self.assertEqual(decide.call_args.kwargs["minimum_level"], 0 if level is None else level)
            self.assertEqual(decide.call_args.kwargs["feedback"], state["fan"])
            self.assertIsNot(decide.call_args.kwargs["feedback"], state["fan"])
            self.assertEqual(state["decision"]["level"], 16)
            row = state["queue"].get_nowait()
            self.assertEqual(row["control"], {"manual_level": level})
            self.assertEqual(row["requested_level"], 16)
            self.assertIn("scenario", row["decision"])
            self.assertGreaterEqual(row["snapshot_at"], row["ts"])
            self.assertGreaterEqual(row["snapshot_at"], row["fan"]["verified_at"])
            before = json.dumps(row)
            state["decision"]["level"] = 16
            state["decision"]["reasons"].append("later reason")
            state["decision"]["rise_c_per_min"].clear()
            state["fan"]["level"] = 7
            state["settings"]["manual_level"] = 7
            self.assertEqual(json.dumps(row), before)
        state["stop"] = False

    async def test_history_until_validation_retention_and_legacy_contract(self):
        state = self.app[STATE]
        now = 1_000_000
        state["settings"]["retention_days"] = 1
        for stamp in (now - 86401, now - 86400, now - 60, now):
            state["journal"].append({"ts": stamp, "nodes": [], "token": "private"})
        with patch("service.time.time", return_value=now):
            for query in ("hours=0", "hours=-1", "hours=721", "hours=nan", "hours=inf", "hours=x",
                          "until=nan", "until=inf", "until=-1", "until=x", "until=1000001", "until="):
                with self.subTest(query=query):
                    response = await self.client.get("/api/history?" + query, headers=self.headers)
                    self.assertEqual(response.status, 400)
            response = await self.client.get("/api/history?hours=720&until=999940", headers=self.headers)
            self.assertEqual(response.status, 200)
            result = await response.json()
            self.assertEqual((result["from"], result["to"]), (now - 86400, now - 60))
            self.assertEqual([r["ts"] for r in result["samples"]], [now - 60, now - 86400])
            self.assertFalse(result["samples_truncated"])
            self.assertNotIn("private", json.dumps(result))
            self.assertNotIn("snapshot_at", result["samples"][0])
            response = await self.client.get("/api/history?hours=1&until=0", headers=self.headers)
            expired = await response.json()
            self.assertEqual(expired["samples"], [])
            self.assertEqual(expired["rows"], [])
            self.assertEqual(expired["to"], 0)
            response = await self.client.get("/api/history", headers=self.headers)
            self.assertEqual((await response.json())["to"], now)

    async def test_history_budget_is_explicit_503_without_partial_results(self):
        journal = self.app[STATE]["journal"]
        journal.append({"ts": time.time(), "nodes": []})
        journal.current.write_bytes(b"x" * (journal.SEGMENT_BYTES + 1))
        response = await self.client.get("/api/history", headers=self.headers)
        self.assertEqual(response.status, 503)
        self.assertIn("缩小时间范围", await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_cancelled_history_owns_reader_until_finished_and_storage_can_append(self):
        state = self.app[STATE]
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        handler = self.handler("/api/history", "GET")
        request = SimpleNamespace(query={"hours": "1"})
        def read(*args):
            entered.set()
            try:
                if not release.wait(5):
                    raise AssertionError("history release missing")
                return {"rows": [], "samples": [], "samples_truncated": False}
            finally:
                finished.set()
        with patch.object(state["journal"], "history", side_effect=read) as reader:
            reading = asyncio.create_task(handler(request))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                reading.cancel()
                await asyncio.sleep(0)
                reading.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(reading.done())
                self.assertTrue(state["history_lock"].locked())
                with self.assertRaises(web.HTTPTooManyRequests):
                    await handler(request)
                self.assertEqual(reader.call_count, 1)
                append = asyncio.get_running_loop().run_in_executor(state["disk"], state["journal"].append,
                    {"ts": time.time(), "nodes": []})
                await asyncio.wait_for(append, 1)
                self.assertGreater(state["journal"].size(), 0)
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await reading
            self.assertTrue(finished.is_set())
            self.assertFalse(state["history_lock"].locked())
            response = await handler(request)
            self.assertEqual(response.status, 200)
        await self.client.close()
        with self.assertRaises(RuntimeError):
            state["history_disk"].submit(lambda: None)

    async def test_repeated_fan_cancellation_keeps_lock_until_real_worker_finishes(self):
        state = self.app[STATE]
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        state["device"] = FakeFan()
        state["last_identity"] = time.monotonic()
        fan_loop = dict(zip(self.startup.__code__.co_freevars, (cell.cell_contents for cell in self.startup.__closure__)))["fan"]
        def worker(device, target, schedule, *, read_only=None):
            entered.set()
            if not release.wait(3):
                raise AssertionError("fan barrier release missing")
            finished.set()
            return {"configured": True}
        with patch("service.fan_apply", worker):
            running = asyncio.create_task(fan_loop())
            contender = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                running.cancel()
                await asyncio.sleep(0)
                running.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(running.done())
                self.assertTrue(state["fan_lock"].locked())
                self.assertFalse(finished.is_set())
                contender = asyncio.create_task(state["fan_lock"].acquire())
                await asyncio.sleep(0.01)
                self.assertFalse(contender.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                self.assertTrue(finished.is_set())
                if contender is not None:
                    await asyncio.wait_for(contender, 1)
                    state["fan_lock"].release()
        state["device"] = None


    async def test_passwordless_but_host_origin_peer_and_https_gated(self):
        settings = {"retention_days": 3, "max_log_mb": 8, "manual_level": 16}
        for remove in ("Origin",):
            headers = dict(self.headers)
            headers.pop(remove)
            result = await self.client.post("/api/settings", json=settings, headers=headers)
            self.assertIn(result.status, (401, 403))
        headers = dict(self.headers, Origin="https://attacker.example")
        self.assertEqual((await self.client.post("/api/settings", json=settings, headers=headers)).status, 403)
        headers = dict(self.headers, Host="attacker.example")
        self.assertEqual((await self.client.post("/api/settings", json=settings, headers=headers)).status, 403)
        headers = dict(self.headers, **{"X-Forwarded-For": "192.168.3.24", "X-Forwarded-Proto": "https"})
        with patch("service.trusted_peer", return_value=(False, False)):
            self.assertEqual((await self.client.post("/api/settings", json=settings, headers=headers)).status, 403)
        with patch("service.trusted_peer", return_value=(True, False)):
            self.assertEqual((await self.client.post("/api/settings", json=settings, headers=headers)).status, 403)
        self.assertEqual((await self.client.post("/api/settings", data="{}", headers=self.headers)).status, 415)
        self.assertEqual((await self.client.post("/api/settings", json={**settings, "manual_level": True}, headers=self.headers)).status, 400)
        self.assertEqual((await self.client.post("/api/settings", data="x" * 8193,
                         headers={**self.headers, "Content-Type": "application/json"})).status, 413)

    async def test_live_settings_and_persistence(self):
        settings = {"retention_days": 2, "max_log_mb": 8, "manual_level": None}
        result = await self.client.post("/api/settings", json=settings, headers=self.headers)
        self.assertEqual(result.status, 200)
        self.assertEqual(json.loads((self.root / "settings.json").read_text()), settings)
        result = await self.client.get("/api/status", headers={"Host":"127.0.0.1:8910"})
        body = await result.json()
        self.assertEqual(body["settings"], settings)
        self.assertEqual(body["requested_level"], 16)
        self.assertFalse((self.root / "admin-key").exists())
        self.assertNotIn("official", body)


    async def test_qr_bind_preserves_schedule_and_remains_unconfirmed(self):
        state = self.app[STATE]
        schedule = state["motor_schedule"]
        schedule.last_attempt, schedule.last_decrease = 10, 20
        self.assertTrue(schedule.persist_debt_then_set_budget(2, 16, True))
        budget_before = (self.root / "motor-budget.json").read_bytes()
        info = SimpleNamespace(model=DEVICE_MODEL, mac_address=DEVICE_MAC)
        device = SimpleNamespace(device_id=DEVICE_ID, info=lambda **kwargs: info)
        body = await self.prepare_qr("b2" * 16)
        with patch("service.AirPurifier", return_value=device) as factory:
            response = await self.client.post("/api/xiaomi/qr/bind", json=body, headers=self.headers)
            self.assertEqual(factory.call_args.kwargs["token"], "b2" * 16)
            self.assertEqual(factory.call_args.kwargs["ip"], DEVICE_IP)
        self.assertEqual((await response.json())["phase"], "bound")
        self.assertEqual((self.root / "device-token").read_text(), "b2" * 16)
        self.assertIs(state["device"], device)
        self.assertIs(state["motor_schedule"], schedule)
        self.assertEqual((schedule.last_attempt, schedule.last_decrease, schedule.attempts, schedule.fault), (10, 20, 2, True))
        self.assertEqual((self.root / "motor-budget.json").read_bytes(), budget_before)
        self.assertTrue(state["fan"]["configured"])
        self.assertFalse(state["fan"]["confirmed"])
        self.assertIsNone(state["fan"]["rpm"])

    async def test_qr_identity_and_persist_failures_preserve_previous_actuator(self):
        state = self.app[STATE]
        previous = FakeFan()
        state["device"] = previous
        state["fan"] = {"configured": True, "confirmed": False, "error": "previous"}
        before = dict(state["fan"])
        (self.root / "device-token").write_text("a1" * 16)
        cases = [patch("service.device_from_token", side_effect=RuntimeError("private identity")),
                 patch("service.atomic_write", side_effect=OSError("private storage"))]
        for failure in cases:
            body = await self.prepare_qr("b2" * 16)
            with patch("service.device_from_token", return_value=FakeFan()), failure:
                response = await self.client.post("/api/xiaomi/qr/bind", json=body, headers=self.headers)
            self.assertEqual((await response.json())["phase"], "failed")
            self.assertNotIn("private", await response.text())
            self.assertEqual((self.root / "device-token").read_text(), "a1" * 16)
            self.assertIs(state["device"], previous)
            self.assertEqual(state["fan"], before)
            self.assertFalse(state["pairing"])

    async def test_cancelled_qr_pair_keeps_ownership_through_verification_save_and_install(self):
        state = self.app[STATE]
        schedule = state["motor_schedule"]
        schedule.attempts, schedule.fault = 2, True
        for stage in ("verify", "save", "install"):
            with self.subTest(stage=stage):
                await self.prepare_qr()
                connector = state["qr"]
                entered, release = threading.Event(), threading.Event()
                device = FakeFan()
                validations = []
                def verify(token):
                    validations.append(token)
                    if stage == "verify":
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError("verify test release missing")
                    return device
                def write(path, token):
                    if stage == "save":
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError("save test release missing")
                    atomic_write(path, token)
                    if stage == "install":
                        entered.set()
                if stage == "install":
                    await state["fan_lock"].acquire()
                with patch("service.device_from_token", verify), patch("service.atomic_write", write):
                    first = asyncio.create_task(connector.bind(OWNER, self.origin, str(DEVICE_ID)))
                    second = None
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        first.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await first
                        second = asyncio.create_task(connector.bind(OWNER, self.origin, str(DEVICE_ID)))
                        await asyncio.sleep(0.02)
                        self.assertFalse(connector.session.binding.done())
                        self.assertFalse(second.done())
                        self.assertTrue(state["pair_lock"].locked())
                        self.assertTrue(state["pairing"])
                        self.assertEqual(validations, ["a1" * 16])
                    finally:
                        release.set()
                        if stage == "install":
                            state["fan_lock"].release()
                        await asyncio.gather(first, return_exceptions=True)
                        if second is not None:
                            self.assertEqual((await second)["phase"], "bound")
                    self.assertIs(state["device"], device)
                    self.assertEqual((self.root / "device-token").read_text(), "a1" * 16)
                    self.assertEqual(validations, ["a1" * 16])
                    self.assertIs(state["motor_schedule"], schedule)
                    self.assertEqual((schedule.attempts, schedule.fault), (2, True))
                    self.assertFalse(state["pair_lock"].locked())
                    self.assertFalse(state["pairing"])

    async def test_expired_fan_readback_forces_max(self):
        state = self.app[STATE]
        state["settings"]["manual_level"] = None
        state["decision"]["level"] = 10
        state["controller"].cool_since = 0
        state["fan"] = {"confirmed": True, "verified_at": time.time() - 100, "error": ""}
        response = await self.client.get("/api/status", headers={"Host": "127.0.0.1:8910"})
        self.assertEqual((await response.json())["requested_level"], 16)
        self.assertEqual(state["controller"].cool_since, 0)
        state["fan"]["verified_at"] = time.time()
        response = await self.client.get("/api/status", headers={"Host": "127.0.0.1:8910"})
        self.assertEqual((await response.json())["requested_level"], 16)

    async def test_fault_cleared_between_frames_starts_new_recovery(self):
        class Response:
            status = 200
            def __init__(self):
                self.content = self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def iter_chunked(self, size):
                yield json.dumps({"timestamp": time.time(), "cpu_percent": 0, "gpu_percent": 0,
                    "temperatures": {"gpu": 40, "acpi": [{"zone": "0", "temp": 45}],
                                     "mlx": [{"chip": "mlx5", "label": "asic", "temp": 50}]}}).encode()
        class Session:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get(self, *args, **kwargs):
                return Response()
        class OverflowOnce(asyncio.Queue):
            overflow = True
            def put_nowait(self, value):
                if self.overflow:
                    self.overflow = False
                    raise asyncio.QueueFull
                return super().put_nowait(value)
        state = self.app[STATE]
        state["queue"] = OverflowOnce(maxsize=2)
        state["settings"]["manual_level"] = None
        state["controller"].cool_since = time.monotonic() - 119
        state["fan"] = {"configured": True, "confirmed": True, "verified_at": time.time(), "error": "",
                        "level": 16, "rpm": 2080, "power": "on", "mode": "favorite",
                        "observed_mono": time.monotonic(), "execution": "matched", "fault": False}
        started = time.monotonic()
        with patch("service.aiohttp.ClientSession", Session):
            await self.startup(self.app)
            try:
                while not state["dropped_records"] and time.monotonic() - started < 1:
                    await asyncio.sleep(0.01)
                self.assertEqual(state["dropped_records"], 1)
                self.assertIsNone(state["controller"].cool_since)
                state["queue"].put_nowait({"ts": time.time(), "nodes": state["nodes"]})
                await asyncio.wait_for(state["queue"].join(), 1)
                self.assertFalse(state["storage_error"])
                await asyncio.sleep(3.1)
                self.assertEqual(state["controller"].level, 16)
                self.assertIsNone(state["controller"].cool_since)
                self.assertTrue(state["controller"].history)
            finally:
                state["stop"] = True

    async def test_pair_disk_wait_does_not_hold_actuator(self):
        entered, release = threading.Event(), threading.Event()
        def delayed_write(path, content):
            if path.name == "device-token":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test release missing")
            atomic_write(path, content)
        body = await self.prepare_qr()
        with patch("service.device_from_token", return_value=FakeFan()), patch("service.atomic_write", delayed_write):
            pending = asyncio.create_task(self.client.post("/api/xiaomi/qr/bind", json=body, headers=self.headers))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assertTrue(self.app[STATE]["pairing"])
                await asyncio.wait_for(self.app[STATE]["fan_lock"].acquire(), timeout=0.2)
                self.app[STATE]["fan_lock"].release()
            finally:
                release.set()
            self.assertEqual((await pending).status, 200)

    async def test_pairing_preserves_privacy_and_identity_failure(self):
        token = "a1" * 16
        schedule = self.app[STATE]["motor_schedule"]
        schedule.last_attempt = time.monotonic()
        self.assertTrue(schedule.persist_debt_then_set_budget(2, 16, True))
        budget_before = (self.root / "motor-budget.json").read_bytes()
        body = await self.prepare_qr(token)
        with patch("service.device_from_token", return_value=FakeFan()):
            result = await self.client.post("/api/xiaomi/qr/bind", json=body, headers=self.headers)
        self.assertEqual(result.status, 200)
        self.assertNotIn(token, await result.text())
        self.assertEqual((self.root / "device-token").read_text(), token)
        self.assertIs(self.app[STATE]["motor_schedule"], schedule)
        self.assertEqual((schedule.attempts, schedule.fault), (2, True))
        self.assertEqual((self.root / "motor-budget.json").read_bytes(), budget_before)
        body = await self.prepare_qr("b2" * 16)
        with patch("service.device_from_token", side_effect=RuntimeError("secret identity detail")):
            result = await self.client.post("/api/xiaomi/qr/bind", json=body, headers=self.headers)
        self.assertEqual((await result.json())["phase"], "failed")
        self.assertEqual((self.root / "device-token").read_text(), token)
        self.assertNotIn("secret", await result.text())

    async def test_legacy_admin_key_is_ignored_and_preserved(self):
        (self.root / "admin-key").write_text("obsolete private content")
        another = await create_app(self.root, {self.origin})
        try:
            self.assertEqual((self.root / "admin-key").read_text(), "obsolete private content")
        finally:
            await another.on_cleanup[-1](another)


    async def test_cleanup_uses_same_physical_schedule_and_guard(self):
        state = self.app[STATE]
        device = FakeFan(["on", "favorite", 10, 1200])
        state["device"] = device
        schedule = state["motor_schedule"]
        schedule.last_attempt = time.monotonic()
        await self.app.on_cleanup[-1](self.app)
        self.assertIs(state["motor_schedule"], schedule)
        self.assertEqual(device.writes, [])

    async def test_healthy_pending_decrease_preserves_lower_request(self):
        state = self.app[STATE]
        state["settings"]["manual_level"] = None
        state["decision"]["level"] = state["controller"].level = 10
        state["controller"].cool_since = 123
        device = FakeFan()
        schedule = state["motor_schedule"]
        schedule.last_attempt -= 8.1
        schedule.last_decrease -= 30.1
        state["fan"] = fan_apply(device, lambda: state["decision"]["level"], schedule)
        self.assertFalse(state["fan"]["confirmed"])
        self.assertEqual(state["fan"]["level"], 15)
        state["last_fan"] = state["last_poll"] = time.monotonic()
        response = await self.client.get("/api/status", headers={"Host": "127.0.0.1:8910"})
        self.assertEqual((await response.json())["requested_level"], 10)
        self.assertEqual(state["controller"].cool_since, 123)
        state["fan"] = fan_apply(device, lambda: state["decision"]["level"], schedule)
        self.assertEqual(device.writes, [15])
        response = await self.client.get("/api/status", headers={"Host": "127.0.0.1:8910"})
        self.assertEqual((await response.json())["requested_level"], 10)
        schedule.last_attempt -= 8.1
        schedule.last_decrease -= 30.1
        state["fan"] = fan_apply(device, lambda: state["decision"]["level"], schedule)
        response = await self.client.get("/api/status", headers={"Host": "127.0.0.1:8910"})
        self.assertEqual((await response.json())["requested_level"], 10)
        self.assertEqual(device.writes, [15, 14])

    async def test_fan_loop_latches_expired_and_in_worker_recovered_faults(self):
        state = self.app[STATE]
        startup_cells = dict(zip(self.startup.__code__.co_freevars,
                                 (cell.cell_contents for cell in self.startup.__closure__)))
        fan_loop = startup_cells["fan"]
        for expired in (True, False):
            with self.subTest(expired=expired):
                now = time.monotonic()
                state["stop"] = False
                state["settings"]["manual_level"] = None
                state["decision"]["level"] = state["controller"].level = 15
                state["controller"].cool_since = 123
                state["last_poll"] = state["last_fan"] = state["last_identity"] = now
                actual = 16 if expired else 14
                state["device"] = FakeFan(["on", "favorite", actual, 1500])
                state["fan"] = {"configured": True, "confirmed": True, "fault": False,
                                "power": "on", "mode": "favorite", "rpm": 1500, "level": actual,
                                "verified_at": time.time(), "execution": "matched",
                                "observed_mono": now - 10 if expired else now}
                schedule = state["motor_schedule"]
                schedule.last_attempt = now - 10
                schedule.last_decrease = now - 31
                self.assertTrue(schedule.persist_debt_then_set_budget(0 if expired else 1,
                                                                      None if expired else 16))
                async def stop_after_frame(delay):
                    state["stop"] = True
                with patch("service.asyncio.sleep", stop_after_frame):
                    await fan_loop()
                self.assertTrue(state["fan"]["confirmed"])
                self.assertFalse(schedule.fault)
                self.assertIsNone(state["controller"].cool_since)
                self.assertEqual(state["decision"]["level"], 16)
                self.assertEqual(state["device"].writes, [] if expired else [16])
                self.assertTrue(state["fan"]["protection_interrupted"])
        state["device"] = None

    async def test_fan_read_expiring_inside_worker_invalidates_recovery(self):
        state = self.app[STATE]
        clock = FakeClock()
        class SlowRead(FakeFan):
            def send(self, command, properties):
                clock.now = 102
                return super().send(command, properties)
        state["settings"]["manual_level"] = None
        state["decision"]["level"] = state["controller"].level = 15
        state["controller"].cool_since = 123
        state["device"] = SlowRead()
        state["last_poll"] = state["last_fan"] = state["last_identity"] = 100
        state["fan"] = {"configured": True, "confirmed": True, "fault": False,
                        "power": "on", "mode": "favorite", "rpm": 1500, "level": 16,
                        "verified_at": time.time(), "execution": "matched", "observed_mono": 92}
        startup_cells = dict(zip(self.startup.__code__.co_freevars,
                                 (cell.cell_contents for cell in self.startup.__closure__)))
        async def stop_after_frame(delay):
            state["stop"] = True
        with patch("service.time", SimpleNamespace(monotonic=clock, time=time.time)), \
                patch("service.asyncio.sleep", stop_after_frame):
            await startup_cells["fan"]()
        self.assertEqual(state["device"].writes, [])
        self.assertTrue(state["fan"]["confirmed"])
        self.assertTrue(state["fan"]["protection_interrupted"])
        self.assertEqual(state["decision"]["level"], 16)
        self.assertIsNone(state["controller"].cool_since)
        state["device"] = None

    async def test_pending_health_never_accepts_fault_unknown_or_invalid_feedback(self):
        state = self.app[STATE]
        healthy = {"configured": True, "confirmed": False, "verified_at": time.time(),
                   "error": "spacing pending", "execution": "pending", "level": 15,
                   "rpm": 1800, "power": "on", "mode": "favorite", "observed_mono": time.monotonic()}
        for change in ({"fault": True}, {"level": 17}, {"level": None}, {"rpm": 0},
                       {"rpm": float("nan")}, {"verified_at": None}, {"verified_at": time.time() - 100},
                       {"configured": False}, {"mode": "auto"}, {"power": "off"},
                       {"observed_mono": None}, {"observed_mono": time.monotonic() - 10},
                       {"observed_mono": time.monotonic() + 10}):
            state["settings"]["manual_level"] = None
            state["decision"]["level"] = state["controller"].level = 10
            state["controller"].cool_since = 123
            state["fan"] = {**healthy, **change}
            state["last_fan"] = state["last_poll"] = time.monotonic()
            response = await self.client.get("/api/status", headers={"Host": "127.0.0.1:8910"})
            self.assertEqual((await response.json())["requested_level"], 16)
            self.assertEqual(state["controller"].cool_since, 123)
            self.assertEqual(state["controller"].level, 10)
        state["fan"] = healthy
        state["motor_schedule"].fault = True
        state["decision"]["level"] = 10
        response = await self.client.get("/api/status", headers={"Host": "127.0.0.1:8910"})
        self.assertEqual((await response.json())["requested_level"], 16)


if __name__ == "__main__":
    unittest.main()
