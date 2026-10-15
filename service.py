import argparse
import asyncio
import ipaddress
import json
import logging
import math
import os
import re
import socket
import ssl
import stat
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from miio import AirPurifier
from miio.exceptions import DeviceException
from miio.integrations.airpurifier.zhimi.airpurifier import OperationMode

from core import Controller, HighWindAuthority, HistoryLimit, Journal, MAX_LEVEL, normalize
from learning import BUCKET, Learning, WINDOW, checkpoint_limit, context_of
from qr_connector import BIND_CONSENT, START_CONSENT, QRError, QRConnector, strict_json

HOSTS = tuple(f"192.168.3.{n}" for n in (180, 181, 183, 184))
DEVICE_IP, DEVICE_MODEL, DEVICE_ID = "192.168.3.24", "zhimi.airpurifier.m2", 70632677
DEVICE_MAC = "78:11:dc:47:87:bf"
STATE = web.AppKey("thermal_state", dict)
MOTOR_IDENTITY = {"ip": DEVICE_IP, "model": DEVICE_MODEL, "did": DEVICE_ID, "mac": DEVICE_MAC}
logging.getLogger("miio").setLevel(100)


def atomic_write(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".pending-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def settings_checked(value):
    if not isinstance(value, dict) or set(value) != {"retention_days", "max_log_mb", "manual_level"}:
        raise ValueError("设置字段不正确")
    for key, low, high in (("retention_days", 1, 30), ("max_log_mb", 8, 4096)):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f"{key} 超出允许范围")
    level = value["manual_level"]
    if level is not None and (type(level) is not int or not 0 <= level <= MAX_LEVEL):
        raise ValueError("手动档位必须为空或 0 到 16 的整数")
    return dict(value)


def settings_from_disk(value):
    if isinstance(value, dict) and set(value) == {"retention_days", "max_log_mb", "full_speed"}:
        if type(value["full_speed"]) is not bool:
            raise ValueError("旧全风选项无效")
        value = {"retention_days": value["retention_days"], "max_log_mb": value["max_log_mb"],
                 "manual_level": MAX_LEVEL if value["full_speed"] else None}
    return settings_checked(value)


async def drain_owned(owned):
    owned = asyncio.ensure_future(owned)
    cancelled = False
    while not owned.done():
        try:
            await asyncio.shield(owned)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not owned.cancelled():
            owned.exception()
        raise asyncio.CancelledError
    return owned.result()


def device_from_token(token):
    if (not isinstance(token, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", token)
            or token.lower() in ("0" * 32, "f" * 32)):
        raise ValueError("设备 token 必须为有效的 32 位十六进制值")
    device = AirPurifier(ip=DEVICE_IP, token=token, timeout=1)
    device.retry_count = 0
    info = device.info(skip_cache=True)
    if (info.model != DEVICE_MODEL or device.device_id != DEVICE_ID
            or info.mac_address.lower() != DEVICE_MAC):
        raise ValueError("目标净化器身份不匹配")
    return device


def trusted_peer(request):
    peer = request.transport.get_extra_info("peername") if request.transport else None
    try:
        address = ipaddress.ip_address(peer[0])
    except (ValueError, TypeError, IndexError):
        return False, False
    return address.is_loopback or address in ipaddress.ip_network("192.168.3.0/24"), address.is_loopback


@dataclass
class MotorSchedule:
    last_attempt: float = -math.inf
    last_decrease: float = -math.inf
    attempts: int = 0
    expected_level: int = None
    fault: bool = False
    budget_path: Path = field(default=None, repr=False)
    persistence_error: str = ""

    def persist_debt_then_set_budget(self, attempts, expected_level, fault=False):
        try:
            if self.budget_path is not None:
                durable_debt = {"version": 1, "identity": MOTOR_IDENTITY, "attempts": attempts,
                          "expected_level": expected_level, "fault": fault}
                if attempts == 0 and (self.attempts or self.fault or self.persistence_error):
                    durable_debt.update(attempts=self.attempts, expected_level=self.expected_level,
                                  fault=self.fault or self.attempts >= 2)
                    if self.attempts and durable_debt["expected_level"] is None:
                        durable_debt["expected_level"] = MAX_LEVEL
                atomic_write(self.budget_path, json.dumps(durable_debt))
                fsync_directory(self.budget_path.parent)
        except (OSError, ValueError):
            self.attempts = max(self.attempts, attempts)
            self.fault = True
            self.persistence_error = "执行预算未能可靠保存，保持只读；仅当前授权安全上限的有效读回及可靠保存可恢复"
            if self.budget_path is not None:
                # Replacement can succeed before directory sync fails; retain the consumed budget.
                try:
                    durable_debt.update(attempts=self.attempts, expected_level=self.expected_level if self.attempts else None, fault=True)
                    if self.attempts and durable_debt["expected_level"] is None:
                        durable_debt["expected_level"] = expected_level if expected_level is not None else MAX_LEVEL
                    atomic_write(self.budget_path, json.dumps(durable_debt))
                    fsync_directory(self.budget_path.parent)
                except (OSError, ValueError):
                    pass
            return False
        self.attempts, self.expected_level, self.fault = attempts, expected_level, fault
        self.persistence_error = ""
        return True

    @classmethod
    def load(cls, budget_path, token_path):
        schedule = cls(budget_path=budget_path)
        try:
            try:
                before = budget_path.lstat()
            except FileNotFoundError:
                try:
                    token_path.lstat()
                except FileNotFoundError:
                    if not schedule.persist_debt_then_set_budget(0, None):
                        raise ValueError
                else:
                    raise ValueError
            else:
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4096 or before.st_mode & 0o022:
                    raise ValueError
                descriptor = os.open(budget_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                try:
                    opened = os.fstat(descriptor)
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise ValueError
                    raw = os.read(descriptor, 4097)
                    after = os.fstat(descriptor)
                    linked = budget_path.lstat()
                    if (len(raw) != before.st_size or before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns
                            or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
                        raise ValueError
                finally:
                    os.close(descriptor)
                def unique(pairs):
                    record = {}
                    for key, value in pairs:
                        if key in record:
                            raise ValueError
                        record[key] = value
                    return record
                record = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
                if (not isinstance(record, dict) or set(record) != {"version", "identity", "attempts", "expected_level", "fault"}
                        or type(record["version"]) is not int or record["version"] != 1 or record["identity"] != MOTOR_IDENTITY
                        or type(record["attempts"]) is not int or not 0 <= record["attempts"] <= 2
                        or type(record["fault"]) is not bool
                        or (record["expected_level"] is not None and (type(record["expected_level"]) is not int
                            or not 0 <= record["expected_level"] <= MAX_LEVEL))
                        or (record["attempts"] == 0) != (record["expected_level"] is None)):
                    raise ValueError
                schedule.attempts, schedule.expected_level = record["attempts"], record["expected_level"]
                schedule.fault = record["fault"] or schedule.attempts == 2
                if schedule.fault and schedule.attempts < 2:
                    schedule.persistence_error = "执行预算处于持久只读故障，需有效全风读回及可靠保存恢复"
        except (OSError, ValueError, UnicodeError, RecursionError):
            schedule.attempts, schedule.fault = 2, True
            schedule.persistence_error = "执行预算缺失、损坏或不安全，保持只读；仅当前授权安全上限的有效读回及可靠保存可恢复"
        finally:
            schedule.last_attempt = schedule.last_decrease = time.monotonic()
        return schedule


@dataclass(frozen=True)
class ReadbackAssessment:
    hold_level: int | None = None
    last_good: float | None = None
    failures: int = 0
    recovering_at: float | None = None
    stage: str = "hold"


@dataclass(frozen=True)
class ActiveRecoveryLease:
    assessment: ReadbackAssessment
    fan: object
    episode: object
    target: int
    deadline: float


class ReadbackEpisode:
    def __init__(self):
        self.published = ReadbackAssessment()
        self.active_recovery = None

    def assessment(self, now):
        value = self.published
        if not value.failures or value.hold_level is None or value.last_good is None:
            return None
        age = now - value.last_good
        if age < 0:
            return None
        stage = "lost" if age >= 30 else "reserve" if age >= 15 or value.failures >= 3 else "hold"
        floor = MAX_LEVEL if stage == "lost" else max(value.hold_level, 10) if stage == "reserve" else value.hold_level
        return ReadbackAssessment(floor, value.last_good, value.failures, value.recovering_at, stage)

    def accept(self, result, now, requested, *, eligible, safe_level=None, limit=MAX_LEVEL):
        old = self.published
        stamp = result.get("observed_mono")
        physical = (eligible and result.get("fault") is False and result.get("power") == "on"
                   and result.get("mode") == "favorite" and type(result.get("level")) is int
                   and 0 <= result["level"] <= MAX_LEVEL
                   and type(result.get("rpm")) in (int, float) and math.isfinite(result["rpm"])
                   and result["rpm"] > 0 and type(stamp) in (int, float)
                   and math.isfinite(stamp) and 0 <= now - stamp < 9)
        matched = physical and result.get("confirmed") is True and result.get("execution") == "matched" and result["level"] == requested
        if physical and old.failures:
            if (matched and old.recovering_at is not None and stamp - old.recovering_at >= 3
                    and result["level"] >= min(limit, self.assessment(now).hold_level)):
                self.published = ReadbackAssessment(requested, stamp)
            elif old.recovering_at is None and stamp > old.last_good:
                self.published = ReadbackAssessment(old.hold_level, old.last_good, old.failures, stamp)
        elif matched:
            self.published = ReadbackAssessment(requested, stamp)
        elif (eligible and result.get("readback_failure") == "initial_transport"
              and not result.get("fault") and not result.get("protection_interrupted")
              and old.hold_level is not None and old.last_good is not None and now >= old.last_good):
            self.published = ReadbackAssessment(max(old.hold_level, safe_level or 0), old.last_good, old.failures + 1)
        elif not physical:
            self.published = ReadbackAssessment()


@dataclass(frozen=True)
class ReleaseAuthority:
    from_level: int
    destination: int
    max_drop: int
    requested_at: float
    original_deadline: float


@dataclass(frozen=True)
class FanRequest:
    level: int
    protected: bool = False
    release: ReleaseAuthority | None = None
    read_only_floor: int | None = None
    upward_only: bool = False
    limit: int = MAX_LEVEL
    high_wind: HighWindAuthority | None = None


def fan_apply(device, wanted, schedule, *, read_only=None, high_wind_gate=True):
    ceiling = MAX_LEVEL
    def target():
        nonlocal ceiling
        value = wanted()
        request = value if isinstance(value, FanRequest) else FanRequest(value)
        if type(request.level) is not int or not 0 <= request.level <= MAX_LEVEL:
            raise ValueError("请求档位必须为 0 到 16 的整数")
        if type(request.limit) is not int or request.limit not in (10, MAX_LEVEL):
            raise ValueError("风力授权上限无效")
        authorized = (isinstance(request.high_wind, HighWindAuthority) and request.high_wind.allowed
            and request.high_wind.observed_mono is not None
            and 0 <= time.monotonic() - request.high_wind.observed_mono < 6)
        ceiling = min(ceiling, request.limit, MAX_LEVEL if not high_wind_gate or authorized else 10)
        return FanRequest(min(request.level, ceiling), request.protected, request.release,
                          request.read_only_floor, request.upward_only, ceiling, request.high_wind)

    def destination(request):
        if actual[2] > request.limit:
            return request.limit
        if request.read_only_floor is not None or request.upward_only:
            return max(request.level, actual[2])
        step, authority = 1, request.release
        if (isinstance(authority, ReleaseAuthority)
                and type(authority.original_deadline) in (int, float)
                and math.isfinite(authority.original_deadline)
                and time.monotonic() >= authority.original_deadline):
            return max(request.level, actual[2])
        if (not request.protected and isinstance(authority, ReleaseAuthority)
                and authority == original_release
                and all(type(value) is int for value in
                        (authority.from_level, authority.destination, authority.max_drop))
                and authority.max_drop in (2, 3) and authority.destination == request.level
                and authority.from_level == actual[2] and running
                and 0 < authority.from_level - request.level <= authority.max_drop
                and all(type(value) in (int, float) and math.isfinite(value) for value in
                        (authority.requested_at, authority.original_deadline))
                and 0 <= time.monotonic() - authority.requested_at < 60
                and authority.original_deadline == authority.requested_at + 60
                and time.monotonic() < authority.original_deadline):
            step = authority.max_drop
        if request.protected and request.level < actual[2]:
            return actual[2]
        return max(request.level, actual[2] - step) if request.level < actual[2] else request.level

    def readback():
        actual = device.send("get_prop", ["power", "mode", "favorite_level", "motor1_speed", "temp_dec"])
        observed_mono = time.monotonic()
        observed_wall = time.time()
        if (not isinstance(actual, list) or len(actual) not in (4, 5) or actual[0] not in ("on", "off")
                or not isinstance(actual[1], str) or not 0 < len(actual[1]) <= 32
                or type(actual[2]) is not int or type(actual[3]) not in (int, float)
                or not math.isfinite(actual[3]) or actual[3] < 0):
            raise ValueError("设备未返回有效完整状态")
        return actual, observed_mono, observed_wall

    request = target()
    original_release = request.release
    level = request.level
    result = {"configured": True, "confirmed": False, "level": None, "rpm": None,
              "power": None, "mode": None, "execution": "pending", "attempts": schedule.attempts,
              "fault": schedule.fault, "error": "", "observed_mono": None,
              "ambient_c": None, "ambient_source": None,
              "ambient_observed_mono": None, "ambient_verified_at": None,
              "protection_interrupted": bool(schedule.fault or schedule.persistence_error)}

    def observed(actual, observed_mono, observed_wall):
        result.update(power=actual[0], mode=actual[1], level=actual[2], rpm=actual[3],
                      verified_at=time.time(), observed_mono=observed_mono)
        if len(actual) == 5 and type(actual[4]) in (int, float) and -200 <= actual[4] <= 800:
            result.update(ambient_c=actual[4] / 10, ambient_source=f"purifier:{DEVICE_ID}:temp_dec",
                          ambient_observed_mono=observed_mono, ambient_verified_at=observed_wall)
        return actual[0] == "on" and actual[1] == "favorite" and actual[3] > 0

    def finish(error="", execution="pending"):
        if schedule.attempts >= 2:
            schedule.fault = True
            execution = "fault"
        if schedule.persistence_error:
            error, execution = schedule.persistence_error, "fault"
            result["verified_at"] = None
            result["observed_mono"] = None
        result.update(error=error, execution=execution, attempts=schedule.attempts, fault=schedule.fault)
        return result

    try:
        actual, observed_mono, observed_wall = readback()
    except Exception as error:
        if (isinstance(error, (TimeoutError, OSError))
                or isinstance(error, DeviceException) and isinstance(error.__cause__, OSError)):
            result["readback_failure"] = "initial_transport"
        return finish("设备读回未知，保持只读，不盲目开机或切换模式", "fault" if schedule.fault else "pending")
    request = target()
    level = request.level
    running = observed(actual, observed_mono, observed_wall)
    if running and actual[2] == level and (not schedule.persistence_error or level == request.limit):
        if (schedule.attempts or schedule.expected_level is not None or schedule.fault or schedule.persistence_error) and not schedule.persist_debt_then_set_budget(0, None):
            return finish(execution="fault")
        result["confirmed"] = True
        return finish(execution="matched")
    observation_floor = (request.read_only_floor if request.read_only_floor is not None else
                         read_only() if read_only is not None else None)
    if type(observation_floor) is int:
        if not running or actual[2] != observation_floor:
            return finish("恢复读回与上次确认状态不一致，停止本次写入并请求保护", "fault")
        return finish("真实读回已恢复，先发布当前状态；上调请求保留原执行截止")
    if request.upward_only and not running:
        return finish("通信恢复状态异常，停止本次写入并请求保护", "fault")
    if schedule.fault:
        return finish("两次修正未确认，故障锁定；仅观察实际匹配且转动的状态以恢复", "fault")
    if running and actual[2] == schedule.expected_level:
        if not schedule.persist_debt_then_set_budget(0, None):
            return finish(execution="fault")
    now = time.monotonic()
    if now - schedule.last_attempt < 8:
        return finish("等待八秒暂定观察间隔，最新请求待执行")
    if schedule.attempts >= 2:
        schedule.fault = True
        return finish("两次修正未确认，故障锁定；继续只读观察", "fault")
    if not 0 <= actual[2] <= MAX_LEVEL:
        return finish("实际档位超出本地控制范围，保留原始读回并停止写入")
    if actual[:3] == ["on", "favorite", level]:
        return finish("档位已匹配，等待正转速读回")
    latest = target()
    level = destination(latest)
    if level < actual[2] and now - schedule.last_decrease < 30:
        return finish("距离上次执行降档不足三十秒，最新请求待执行")
    if actual[:3] == ["on", "favorite", level]:
        return finish("最新请求已合并，等待转速或下一步降档")
    schedule.last_attempt = time.monotonic()
    if not schedule.persist_debt_then_set_budget(schedule.attempts + 1, level, schedule.attempts + 1 >= 2):
        return finish(execution="fault")
    result["protection_interrupted"] = result["protection_interrupted"] or schedule.fault
    result["verified_at"] = None
    result["observed_mono"] = None
    result.update(ambient_c=None, ambient_source=None, ambient_observed_mono=None, ambient_verified_at=None)
    def mutate(function, *args, decrease=False):
        schedule.last_attempt = time.monotonic()
        try:
            function(*args)
        finally:
            schedule.last_attempt = time.monotonic()
            if decrease:
                schedule.last_decrease = schedule.last_attempt
    try:
        if actual[0] != "on":
            mutate(device.on)
        if actual[1] != "favorite":
            mutate(device.set_mode, OperationMode.Favorite)
        while True:
            latest = target()
            next_level = destination(latest)
            level = min(latest.limit, max(level, next_level))
            if level < actual[2] and time.monotonic() - schedule.last_decrease < 30:
                level = actual[2]
            if schedule.expected_level == level:
                break
            if not schedule.persist_debt_then_set_budget(schedule.attempts, level, schedule.fault):
                return finish(execution="fault")
        if actual[2] != level:
            mutate(device.set_favorite_level, level, decrease=level < actual[2])
        result["level"], result["rpm"], result["power"], result["mode"] = None, None, None, None
        actual, observed_mono, observed_wall = readback()
        running = observed(actual, observed_mono, observed_wall)
    except Exception:
        return finish("修正未获完整确认，已计入尝试预算，等待观察间隔")
    if running and actual[2] == target().level:
        if not schedule.persist_debt_then_set_budget(0, None):
            return finish(execution="fault")
        result["confirmed"] = True
        return finish(execution="matched")
    if running and actual[2] == schedule.expected_level:
        if not schedule.persist_debt_then_set_budget(0, None):
            return finish(execution="fault")
    return finish("指令尚未匹配最新请求或电机尚未转动")


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.sendto(message.encode(), address)
    except OSError:
        pass


def learning_archive_usage(root):
    directory = Path(root) / "learning"
    try:
        before = directory.lstat()
    except FileNotFoundError:
        return dict(checkpoint_bytes=0, preserved_bytes=0, reserved_bytes=0)
    if (not stat.S_ISDIR(before.st_mode)
            or os.name == "posix" and before.st_uid != os.getuid()):
        raise ValueError("learning archive directory")
    checkpoint_bytes, preserved_bytes = 0, 0
    for count, path in enumerate(directory.iterdir(), 1):
        if count > 128:
            raise ValueError("learning archive count")
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or os.name == "posix" and info.st_uid != os.getuid()):
            raise ValueError("learning archive file ownership")
        if path.name != "checkpoint.json" and not re.fullmatch(r"\.pending-[a-z0-9_]{8}", path.name):
            raise ValueError("learning archive file name")
        if info.st_size > 4 * 1024**2:
            raise ValueError("learning archive file size")
        preserved_bytes += info.st_size
        if path.name == "checkpoint.json":
            checkpoint_bytes = info.st_size
    after = directory.lstat()
    if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
        raise ValueError("learning archive changed")
    return dict(checkpoint_bytes=checkpoint_bytes, preserved_bytes=preserved_bytes,
                reserved_bytes=preserved_bytes)


async def create_app(root, origins=None, *, learning_enabled=False, high_wind_gate=True):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    archive_usage = learning_archive_usage(root) if not learning_enabled else None
    defaults = {"retention_days": 7, "max_log_mb": 4096, "manual_level": MAX_LEVEL}
    settings_path = root / "settings.json"
    settings = settings_from_disk(json.loads(settings_path.read_text()) if settings_path.exists() else defaults)
    origins = set(origins or ("https://192.168.3.180:8910", "https://127.0.0.1:8910", "https://localhost:8910"))
    hosts = {urlsplit(origin).netloc for origin in origins}
    motor_schedule = await asyncio.to_thread(MotorSchedule.load, root / "motor-budget.json", root / "device-token")
    state = {"settings": settings, "nodes": [], "decision": {"level": MAX_LEVEL, "reasons": ["启动全风保护"]},
             "fan": {"configured": False, "confirmed": False, "level": None, "rpm": None,
                     "fault": motor_schedule.fault, "attempts": motor_schedule.attempts,
                     "error": motor_schedule.persistence_error or "尚未绑定本地执行凭据，无法执行风力控制"}, "storage_error": "",
             "settings_error": "", "settings_task": None,
             "dropped_records": 0, "log_bytes": 0, "last_poll": time.monotonic(),
             "last_fan": time.monotonic(), "updated_at": None, "stop": False,
             "controller": Controller(HOSTS, rules_only=not learning_enabled, high_wind_gate=high_wind_gate), "device": None, "fan_lock": asyncio.Lock(),
             "pair_lock": asyncio.Lock(), "pairing": False,
             "journal": Journal(root / "logs"), "queue": asyncio.Queue(maxsize=2),
             "disk": ThreadPoolExecutor(max_workers=1), "history_lock": asyncio.Lock(),
             "history_disk": ThreadPoolExecutor(max_workers=1),
             "root": root, "force_max": False, "last_identity": 0,
             "motor_schedule": motor_schedule, "settings_revision": 0,
             "fan_request": FanRequest(MAX_LEVEL, protected=True),
             "learning": {}, "learning_wall": None, "learning_mono": None,
             "history_recording": {"source": "control", "state": "starting", "writes": 0,
                 "failed_writes": 0, "last_timestamp": None, "error": None}}
    learning = Learning(HOSTS) if learning_enabled else None
    learning_root = root / "learning"
    learning_path = learning_root / "checkpoint.json"
    learning_loaded = False
    learning_checkpoint_bucket = None
    readback_episode = ReadbackEpisode()
    durable_published = {}
    def disabled_learning(usage):
        return dict(enabled=False, reason="disabled", published={}, active={}, candidate={},
                    eligibility=dict(phase="disabled", reason="disabled", since=None, context=None, gear=None),
                    next_at=None, bucket_count=0, fixed_power_w=80, error="",
                    **{key: usage.get(key, 0) for key in ("checkpoint_bytes", "preserved_bytes", "reserved_bytes")})

    state["learning"] = learning.status(settings) if learning_enabled else disabled_learning(archive_usage)

    @web.middleware
    async def protection(request, handler):
        try:
            if request.host not in hosts:
                raise web.HTTPForbidden(text="入口地址不匹配")
            if request.method not in ("GET", "HEAD") or request.path.startswith("/api/xiaomi/"):
                allowed, loopback = trusted_peer(request)
                if not allowed:
                    raise web.HTTPForbidden(text="仅允许可信局域网或本机传输连接")
                if not request.secure and not loopback:
                    raise web.HTTPForbidden(text="管理操作需要 HTTPS")
            if request.method not in ("GET", "HEAD"):
                if request.headers.get("Origin") not in origins:
                    raise web.HTTPForbidden(text="提交来源不匹配")
                if request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType(text="需要 JSON 数据")
            response = await handler(request)
        except web.HTTPException as error:
            response = error
        except QRError as error:
            response = web.json_response({"error": error.code}, status=error.status)
        except (ValueError, json.JSONDecodeError):
            response = web.json_response({"error": "输入无效，或设备验证失败。检查字段范围、token 和设备连接。"}, status=400)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        if isinstance(response, web.HTTPException):
            raise response
        return response

    app = web.Application(middlewares=[protection], client_max_size=8192)
    app[STATE] = state

    def protect(reason="服务或执行条件异常，重新建立全风保护基线"):
        decision = state["controller"].protect(reason, time.monotonic())
        state["decision"].update(decision)
        state["fan_request"] = FanRequest(state["controller"].effective_limit(time.monotonic()), protected=True,
                                          limit=state["controller"].effective_limit(time.monotonic()))
        return decision

    def fan_healthy():
        observed = state["fan"]
        gear, rpm, stamp = observed.get("level"), observed.get("rpm"), observed.get("verified_at")
        mono = observed.get("observed_mono")
        return (observed.get("configured") and not state["motor_schedule"].fault
                and not observed.get("fault") and observed.get("execution") != "fault"
                and observed.get("power") == "on" and observed.get("mode") == "favorite"
                and type(gear) is int and 0 <= gear <= MAX_LEVEL
                and type(rpm) in (int, float) and math.isfinite(rpm) and rpm > 0
                and type(stamp) in (int, float) and 0 <= time.time() - stamp <= 9
                and type(mono) in (int, float) and math.isfinite(mono) and 0 <= time.monotonic() - mono <= 9
                and 0 <= time.monotonic() - state["last_fan"] <= 9)

    def request_snapshot():
        base = state["fan_request"]
        limit = state["controller"].effective_limit(time.monotonic())
        authority = state["controller"].high_wind if high_wind_gate else None
        hold = readback_hold(time.monotonic())
        protected = bool(state["force_max"] or state["storage_error"]
                or state["settings_error"] or state["pairing"] or state["settings_task"] is not None
                or time.monotonic() - state["last_poll"] > 6 or not fan_healthy() and hold is None)
        if protected:
            return FanRequest(limit, protected=True, limit=limit, high_wind=authority)
        level = min(limit, max(base.level, state["settings"]["manual_level"] or 0,
                    hold.hold_level if hold else 0))
        if hold is not None:
            return FanRequest(level, upward_only=True, limit=limit, high_wind=authority)
        release = (base.release if base.level == level and not base.protected
                   and state["settings"]["manual_level"] is None else None)
        return FanRequest(level, release=release, limit=limit, high_wind=authority)

    def publish_request(decision, now):
        trial = decision.get("execution_trial") or {}
        authority = None
        if (not learning_enabled and state["settings"]["manual_level"] is None
                and trial.get("status") == "pending" and trial.get("profile") == "rules"
                and trial.get("purpose") == "release" and trial.get("planned_step") in (2, 3)
                and trial.get("original_to") == trial.get("to") == decision["level"]
                and trial.get("requested_at") == trial.get("destination_requested_at")
                and now < trial.get("original_deadline", -math.inf)):
            authority = ReleaseAuthority(trial["original_from"], trial["to"], trial["planned_step"],
                                         trial["requested_at"], trial["original_deadline"])
        state["fan_request"] = FanRequest(decision["level"], release=authority)

    def readback_hold(now):
        schedule = state["motor_schedule"]
        hold = readback_episode.assessment(now)
        controller = state["controller"]
        operation = controller._policy["operation"]
        lease = readback_episode.active_recovery
        restoring = (hold is not None and isinstance(lease, ActiveRecoveryLease)
            and lease.assessment is readback_episode.published and lease.fan is state["fan"]
            and lease.episode is (operation["episode"] if controller._pending() else None)
            and now < lease.deadline
            and min(controller.effective_limit(now), max(state["fan_request"].level, state["settings"]["manual_level"] or 0, hold.hold_level)) == lease.target
            and schedule.attempts == 1 and schedule.expected_level == lease.target)
        thermal_stamp = controller._policy["fan_transport_safe"]
        if (thermal_stamp is None or not 0 <= now - thermal_stamp <= 6
                or schedule.fault or schedule.persistence_error or (schedule.attempts
                or schedule.expected_level is not None) and not restoring or state["fan"].get("fault")
                or state["fan"].get("execution") == "fault"
                or state["fan"].get("protection_interrupted")
                or controller._pending() and now >= operation["episode"].original_deadline
                or controller._policy["startup_required"]):
            return None
        return hold

    def wanted():
        return request_snapshot().level

    def current_fan(requested_level):
        observed = dict(state["fan"])
        if observed.get("execution") != "matched":
            return observed
        matched = (observed.get("confirmed") is True and observed.get("fault") is False
                   and fan_healthy() and observed.get("level") == requested_level)
        trial = state["decision"].get("execution_trial")
        if trial is not None:
            destination = trial.get("to") if isinstance(trial, dict) else None
            stamp = trial.get("destination_requested_at") if isinstance(trial, dict) else None
            matched = (matched and type(destination) is int and destination == requested_level
                       and type(stamp) in (int, float) and math.isfinite(stamp)
                       and 0 <= stamp < observed["observed_mono"] <= time.monotonic())
        if not matched:
            observed.update(execution="pending", confirmed=False)
        return observed

    async def disk_call(function, *args):
        return await drain_owned(asyncio.get_running_loop().run_in_executor(state["disk"], function, *args))

    def learning_healthy():
        return (learning_enabled and not state["stop"] and not state["force_max"] and not state["pairing"]
                and state["settings_task"] is None and not state["settings_error"]
                and not state["storage_error"] and fan_healthy())

    def active_power(nodes, now, wall, advancing=False):
        if not learning_enabled:
            return {}
        cached = state["learning"]
        observed = current_fan(wanted())
        if (not learning_healthy() or cached.get("error")
                or observed.get("confirmed") is not True or observed.get("execution") != "matched"
                or cached.get("settings_revision") != state["settings_revision"]
                or not 0 <= now - state["last_poll"] <= 6
                or state["learning_wall"] is None or not 0 <= wall - state["learning_wall"] <= 6):
            return {}
        if (len(nodes) != len(HOSTS) or {node.get("host") for node in nodes} != set(HOSTS)
                or any(not node.get("online") or node.get("thermal_throttle") or node.get("ambiguous_sensors")
                       or type(node.get("source_ts")) not in (int, float) or not math.isfinite(node["source_ts"])
                       or (node["source_ts"] <= state["controller"].sources.get(node["host"], -math.inf) if advancing
                           else node["source_ts"] < state["controller"].sources.get(node["host"], -math.inf))
                       or not 0 <= now - node.get("requested_mono", -math.inf) <= 6 for node in nodes)):
            return {}
        return {host: policy for host, policy in cached.get("published", {}).items()
                if 0 <= wall - policy["generated_at"] <= WINDOW
                and policy["context"] == context_of(nodes)
                and policy["evidence_from"] >= wall - state["settings"]["retention_days"] * 86400}

    def learning_checkpoint(config, now):
        nonlocal learning_checkpoint_bucket, durable_published
        load_learning(config)
        learning.loss["crash_bytes"] += learning_workspace()
        raw_since = state["journal"].retained_since if state["journal"].retained_since is not None else now
        text = learning.checkpoint(now, config, raw_since)
        learning_checkpoint_bucket = int(learning.last_ts // BUCKET) if learning.last_ts is not None else int(now // BUCKET)
        atomic_write(learning_path, text)
        fsync_directory(learning_root)
        durable_published = deepcopy(learning.published)
        return learning.status(config)

    def load_learning(config):
        nonlocal learning_loaded, learning_checkpoint_bucket, durable_published
        if learning_loaded:
            return
        learning_loaded = True
        try:
            recovered = learning_workspace()
            if learning_path.is_symlink():
                raise ValueError("checkpoint link")
            if learning_path.exists():
                with learning_path.open("r", encoding="utf-8") as stream:
                    learning.restore(stream.read(checkpoint_limit(config) + 1), time.time(), config)
                learning_checkpoint_bucket = int(learning.last_ts // BUCKET) if learning.last_ts is not None else None
                durable_published = deepcopy(learning.published)
            learning.loss["crash_bytes"] += recovered
        except Exception:
            learning.fail()

    def learning_workspace():
        if learning_root.is_symlink():
            raise ValueError("learning directory link")
        created = not learning_root.exists()
        learning_root.mkdir(mode=0o700, exist_ok=True)
        directory_info = learning_root.lstat()
        if not stat.S_ISDIR(directory_info.st_mode) or os.name == "posix" and directory_info.st_uid != os.getuid():
            raise ValueError("learning directory ownership")
        os.chmod(learning_root, 0o700)
        if created:
            fsync_directory(root)
        entries = list(learning_root.iterdir())
        if len(entries) > 128:
            raise ValueError("learning directory budget")
        recovered = 0
        for path in entries:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or os.name == "posix" and info.st_uid != os.getuid()):
                raise ValueError("learning file ownership")
            if re.fullmatch(r"\.pending-[a-z0-9_]{8}", path.name):
                path.unlink()
                recovered += info.st_size
            elif path.name != "checkpoint.json":
                raise ValueError("learning file name")
            elif info.st_size > 4 * 1024**2:
                raise ValueError("learning file budget")
        return recovered

    def learning_reserve(config):
        if not learning_enabled:
            return learning_archive_usage(root)["reserved_bytes"]
        limit = checkpoint_limit(config)
        return limit + max(limit, learning_path.stat().st_size if learning_path.exists() else 0)

    async def page(request):
        return web.FileResponse(Path(__file__).with_name("ui.html"))

    async def asset(request):
        return web.FileResponse(Path(__file__).with_name("ui." + request.match_info["ext"]))

    async def status(request):
        requested_level = wanted()
        return web.json_response({key: state[key] for key in ("nodes", "decision", "fan", "settings",
            "storage_error", "settings_error", "dropped_records", "log_bytes", "updated_at")} | {
                "high_wind_gate": state["controller"].high_wind_gate,
                "high_wind_limit": state["controller"].effective_limit(time.monotonic()),
                "high_wind_witness": state["controller"].high_wind.witness
                    if state["controller"].effective_limit(time.monotonic()) > 10 else None,
                "high_wind_expires_mono": state["controller"].high_wind.observed_mono + 6
                    if state["controller"].high_wind.allowed else None,
                "requested_level": requested_level, "settings_pending": state["settings_task"] is not None,
                "read_only": False, "control_mode": "auto" if state["settings"]["manual_level"] is None else "manual",
                "strategy_mode": "learning" if learning_enabled else "rules",
                "control_active": not state["stop"] and state["updated_at"] is not None and time.monotonic() - state["last_poll"] < 8,
                "history_recording": dict(state["history_recording"]),
                "fan": current_fan(requested_level),
                "learning": (dict(state["learning"], enabled=True, active=active_power(state["nodes"], time.monotonic(), time.time()))
                             if learning_enabled else disabled_learning(state["learning"])),
                "server_time": time.time(), "period_seconds": 3,
                "controller_age_seconds": round(time.monotonic() - state["last_poll"], 2)})

    async def history(request):
        hours = float(request.query.get("hours", "1"))
        if not 0 < hours <= 720 or not math.isfinite(hours):
            raise ValueError
        now = time.time()
        until = float(request.query.get("until", now))
        if not math.isfinite(until) or not 0 <= until <= now:
            raise ValueError
        if state["history_lock"].locked():
            raise web.HTTPTooManyRequests(text="历史正在读取")
        async with state["history_lock"]:
            since = max(until - hours * 3600, now - state["settings"]["retention_days"] * 86400)
            try:
                result = await drain_owned(asyncio.get_running_loop().run_in_executor(
                    state["history_disk"], state["journal"].history, since, until))
            except HistoryLimit as error:
                raise web.HTTPServiceUnavailable(text=str(error)) from None
            return web.json_response({**result, "from": since, "to": until,
                                      "aggregation": "时间桶内最高温，缺失记录保留"})

    async def configure(request):
        if state["stop"] or state["settings_task"] is not None:
            raise web.HTTPConflict(text="设置正在保存或服务正在停止，请刷新状态后重试")
        state["settings_revision"] += 1
        async def transaction():
            try:
                proposed = settings_checked(await request.json())
                protect()
                def save():
                    try:
                        atomic_write(settings_path, json.dumps(proposed))
                        fsync_directory(root)
                    except Exception:
                        try:
                            actual = settings_from_disk(json.loads(settings_path.read_text()))
                        except Exception:
                            actual = None
                        return actual, "设置保存结果未可靠确认，保持全风；请刷新状态后重新保存", "", None, None
                    try:
                        if learning_enabled:
                            load_learning(state["settings"])
                        state["journal"].prune(time.time(), proposed["retention_days"],
                                               proposed["max_log_mb"] * 1024**2 - Journal.SEGMENT_BYTES
                                               - learning_reserve(proposed))
                        cached = (learning_checkpoint(proposed, time.time()) if learning_enabled
                                  else disabled_learning(learning_archive_usage(root)))
                        return proposed, "", "", state["journal"].size() + (
                            learning.bytes if learning_enabled else cached["preserved_bytes"]), cached
                    except Exception:
                        if learning_enabled:
                            learning.fail()
                        return proposed, "", "设置已保存，但历史或学习状态维护失败，可能已部分清理；检查磁盘后重试", None, (
                            learning.status(proposed) if learning_enabled else disabled_learning(state["learning"]))
                actual, error, maintenance_error, size, cached = await disk_call(save)
                if actual is not None:
                    state["settings"] = actual
                state["settings_error"] = error
                if maintenance_error:
                    state["storage_error"] = maintenance_error
                if size is not None:
                    state["log_bytes"] = size
                if cached is not None:
                    state["learning"] = dict(cached, settings_revision=state["settings_revision"])
                protect()
                return web.json_response({"ok": not error, "settings": state["settings"],
                    "settings_known": actual is not None, "error": error,
                    "maintenance_error": maintenance_error}, status=503 if error else 200)
            finally:
                state["settings_task"] = None
        state["settings_task"] = asyncio.create_task(transaction())
        return await drain_owned(state["settings_task"])

    async def pair_token(*, admit):
        async def transaction():
            async with state["pair_lock"]:
                credential = admit()
                state["pairing"] = True
                protect()
                try:
                    device = await asyncio.to_thread(device_from_token, credential)
                    await disk_call(atomic_write, root / "device-token", credential.lower())
                    async with state["fan_lock"]:
                        state["device"] = device
                        protect()
                        state["last_identity"] = time.monotonic()
                        state["fan"] = {"configured": True, "confirmed": False, "level": None, "rpm": None,
                                        "fault": state["motor_schedule"].fault, "attempts": state["motor_schedule"].attempts,
                                        "error": state["motor_schedule"].persistence_error or "凭据已保存，等待受限执行与实际设备读回"}
                except web.HTTPException:
                    raise
                except Exception:
                    raise ValueError("设备绑定验证或保存失败") from None
                finally:
                    state["pairing"] = False
        await drain_owned(transaction())

    state["qr"] = QRConnector(pair_token)

    async def xiaomi_qr(request):
        action = request.match_info["action"]
        if request.query_string:
            raise QRError("invalid_input")
        try:
            async with asyncio.timeout(5):
                body = strict_json(await request.read())
        except TimeoutError:
            raise QRError("request_timeout", 408) from None
        fields = {"owner", "consent", "did"} if action == "bind" else {"owner", "consent"} if action == "start" else {"owner"}
        if (not isinstance(body, dict) or set(body) != fields or not isinstance(body.get("owner"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", body["owner"])
                or (action == "start" and body["consent"] != START_CONSENT)
                or (action == "bind" and body["consent"] != BIND_CONSENT)):
            raise QRError("invalid_input")
        owner, origin = body["owner"], request.headers["Origin"]
        connector = state["qr"]
        if action == "start":
            result = connector.start(owner, origin)
        elif action == "cancel":
            result = connector.cancel(owner, origin)
        elif action == "bind":
            result = await connector.bind(owner, origin, body["did"])
        else:
            session = connector.owned(owner, origin)
            if action == "image":
                if not session.image:
                    raise QRError("image_not_ready", 409)
                return web.Response(body=session.image, content_type="image/png")
            result = connector.public(session)
        return web.json_response(result)

    async def poll(session, host, wall):
        started = time.monotonic()
        try:
            async with session.get(f"http://{host}:9100/status", allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError
                raw = bytearray()
                async for chunk in response.content.iter_chunked(32768):
                    raw.extend(chunk)
                    if len(raw) > 262144:
                        raise ValueError
                data = json.loads(raw)
            return normalize(host, data, started, wall)
        except Exception as error:
            transport = isinstance(error, (asyncio.TimeoutError, OSError, aiohttp.ClientConnectionError))
            return {"host": host, "hostname": host, "online": False, "sampled_at": wall,
                    "failure_kind": "transport" if transport else "evidence",
                    "requested_mono": started, "error": "节点接口超时、离线或数据不完整",
                    "cpu_temp": None, "gpu_temp": None, "board_temp": None, "sensors": []}

    async def monitor():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5), trust_env=False) as session:
            deadline = time.monotonic()
            while not state["stop"]:
                wall = time.time()
                nodes = await asyncio.gather(*(poll(session, host, wall) for host in HOSTS))
                now = time.monotonic()
                minimum = state["settings"]["manual_level"]
                power_thresholds = active_power(nodes, now, wall, advancing=True) if learning_enabled else {}
                feedback = current_fan(wanted())
                hold = readback_hold(now)
                if hold is not None:
                    feedback["readback_hold"] = dict(level=hold.hold_level, failures=hold.failures,
                        recovering=hold.recovering_at is not None, stage=hold.stage,
                        until=hold.last_good + 30 if hold.stage != "lost" else now + 3)
                if state["motor_schedule"].fault:
                    feedback["fault"] = True
                decision = state["controller"].decide(nodes, now, minimum_level=0 if minimum is None else minimum,
                                                       power_thresholds=power_thresholds,
                                                       feedback=feedback,
                                                       manual_level=minimum)
                if not learning_enabled:
                    decision["learning_eligible"] = False
                if (state["storage_error"] or state["settings_error"] or not fan_healthy() and hold is None
                        or state["pairing"] or state["settings_task"] is not None):
                    decision.update(protect())
                state.update(nodes=nodes, decision=decision, last_poll=now, updated_at=wall)
                publish_request(decision, now)
                requested_level = wanted()
                row_power_thresholds = active_power(nodes, now, wall) if learning_enabled else {}
                row = {"ts": wall, "snapshot_at": time.time(), "nodes": nodes,
                       "control": {"manual_level": minimum}, "decision": deepcopy(decision),
                       "fan": current_fan(requested_level), "requested_level": requested_level,
                       "active_power_policy": {host: {key: policy[key] for key in
                           ("version", "threshold_w", "source", "generated_at")} for host, policy in row_power_thresholds.items()},
                       "learning": {"enabled": learning_enabled, "healthy": learning_healthy(), "mono": now,
                                    "revision": state["settings_revision"],
                                    "dropped_records": state["dropped_records"],
                                    "elapsed": now - state["learning_mono"] if state["learning_mono"] is not None else 0}}
                state["learning_wall"], state["learning_mono"] = wall, now
                try:
                    state["queue"].put_nowait(row)
                except asyncio.QueueFull:
                    state["dropped_records"] += 1
                    state["storage_error"] = "磁盘写入滞后，采样记录丢失，全风保护"
                    protect()
                deadline += 3
                if deadline < now:
                    deadline = now + 3
                await asyncio.sleep(max(0, deadline - time.monotonic()))

    async def storage():
        def write(row, config):
            if learning_enabled:
                load_learning(config)
            budget = config["max_log_mb"] * 1024**2
            encoded = len(json.dumps(row, ensure_ascii=False).encode()) + 1
            if encoded > 65536:
                raise ValueError("记录过大")
            state["journal"].prune(time.time(), config["retention_days"],
                                   budget - encoded - Journal.SEGMENT_BYTES - learning_reserve(config))
            try:
                state["journal"].append(row)
                state["history_recording"].update(state="recording",
                    writes=state["history_recording"]["writes"] + 1, last_timestamp=row["ts"], error=None)
            except Exception:
                if learning_enabled:
                    learning.loss["raw_append"] += 1
                    learning.eligibility.update(phase="blocked", reason="gap", since=None)
                    for bucket in learning.buckets.values():
                        bucket["last"] = None
                raise
            if not learning_enabled:
                usage = learning_archive_usage(root)
                return state["journal"].size() + usage["preserved_bytes"], disabled_learning(usage)
            try:
                raw_since = state["journal"].retained_since
                learning.trim(time.time(), config, raw_since if raw_since is not None else time.time())
                learning.observe(row)
                if learning_checkpoint_bucket != int(row["ts"] // BUCKET) or learning.published != durable_published:
                    cached = learning_checkpoint(config, time.time())
                else:
                    cached = learning.status(config)
                    cached["published"] = {host: policy for host, policy in cached["published"].items()
                                           if durable_published.get(host) == policy}
            except Exception:
                learning.fail()
                cached = learning.status(config)
            actual_bytes = learning_path.lstat().st_size if learning_path.exists() and not learning_path.is_symlink() else 0
            cached["checkpoint_bytes"] = actual_bytes
            cached["reserved_bytes"] = learning_reserve(config)
            return state["journal"].size() + actual_bytes, cached
        while not state["stop"] or not state["queue"].empty():
            try:
                row = await asyncio.wait_for(state["queue"].get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            writes_before = state["history_recording"]["writes"]
            try:
                config, revision = dict(state["settings"]), state["settings_revision"]
                size, cached = await disk_call(write, row, config)
                state["log_bytes"] = size
                state["storage_error"] = ""
                if not learning_enabled:
                    state["learning"] = cached
                elif (revision == state["settings_revision"] and row.get("learning", {}).get("revision") == revision
                        and row.get("ts") == state["updated_at"] and learning_healthy()
                        and context_of(state["nodes"]) == cached["eligibility"].get("context")):
                    state["learning"] = dict(cached, settings_revision=revision)
                else:
                    state["learning"] = dict(cached, settings_revision=-1)
            except Exception:
                state["dropped_records"] += 1
                state["storage_error"] = "日志写入失败，全风保护。检查磁盘空间和权限。"
                if state["history_recording"]["writes"] == writes_before:
                    state["history_recording"].update(state="failed",
                        failed_writes=state["history_recording"]["failed_writes"] + 1,
                        error=state["storage_error"])
                protect()
            finally:
                state["queue"].task_done()

    async def fan():
        token_path = root / "device-token"
        while not state["stop"]:
            async with state["fan_lock"]:
                schedule = state["motor_schedule"]
                thermal_stamp = state["controller"]._policy["fan_transport_safe"]
                thermal_safe = (thermal_stamp is not None and 0 <= time.monotonic() - thermal_stamp <= 6)
                eligible = (thermal_safe and not schedule.fault and not schedule.persistence_error and schedule.attempts == 0
                            and schedule.expected_level is None
                            and not state["controller"]._policy["startup_required"])
                if fan_healthy() and readback_episode.published.failures == 0:
                    readback_episode.accept(state["fan"], time.monotonic(), state["decision"]["level"], eligible=eligible)
                if not fan_healthy() and readback_hold(time.monotonic()) is None:
                    protect("风机反馈中断，重新建立全风保护基线")
                try:
                    if state["device"] is None and token_path.exists():
                        state["device"] = await asyncio.to_thread(device_from_token, token_path.read_text().strip())
                        state["last_identity"] = time.monotonic()
                    device = state["device"]
                    if device is not None:
                        if time.monotonic() - state["last_identity"] > 60:
                            state["device"] = device = await asyncio.to_thread(
                                device_from_token, token_path.read_text().strip())
                            state["last_identity"] = time.monotonic()
                        state["fan"] = await apply_fan(device)
                        readback_episode.accept(state["fan"], time.monotonic(), state["decision"]["level"],
                            eligible=eligible and not schedule.fault and schedule.attempts == 0
                            and schedule.expected_level is None and not schedule.persistence_error,
                            limit=state["controller"].effective_limit(time.monotonic()),
                            safe_level=max(state["decision"]["level"],
                                state["controller"]._policy["operation"]["episode"].original_from
                                if state["controller"]._pending() else 0))
                        if state["fan"].get("protection_interrupted"):
                            protect("执行期间触发保护，读回恢复后仍需重新建立安全基线")
                except Exception:
                    readback_episode.published = ReadbackAssessment()
                    state["device"] = None
                    state["fan"] = {"configured": token_path.exists(), "confirmed": False,
                                    "level": None, "rpm": None, "fault": state["motor_schedule"].fault,
                                    "attempts": state["motor_schedule"].attempts,
                                    "error": state["motor_schedule"].persistence_error or "净化器不可达、身份验证失败或读回不一致，全风尚未确认"}
                state["last_fan"] = time.monotonic()
                if not fan_healthy() and readback_hold(time.monotonic()) is None:
                    protect()
            await asyncio.sleep(3)

    async def watchdog():
        notify("READY=1")
        while not state["stop"]:
            now = time.monotonic()
            if now - state["last_poll"] < 8 and now - state["last_fan"] < 30:
                notify("WATCHDOG=1")
            await asyncio.sleep(2)

    async def startup(application):
        state["tasks"] = [asyncio.create_task(task()) for task in (monitor, storage, fan, watchdog)]

    async def apply_fan(device):
        def execute():
            interrupted = False
            def target():
                nonlocal interrupted
                request = request_snapshot()
                interrupted = interrupted or request.protected
                return request
            result = fan_apply(device, target, state["motor_schedule"], high_wind_gate=high_wind_gate)
            result["protection_interrupted"] = result.get("protection_interrupted", False) or interrupted
            return result
        schedule = state["motor_schedule"]
        hold = readback_hold(time.monotonic())
        operation = state["controller"]._policy["operation"]
        if (hold is not None and schedule.attempts == 0 and schedule.expected_level is None
                and not schedule.fault and not schedule.persistence_error):
            episode = operation["episode"] if state["controller"]._pending() else None
            readback_episode.active_recovery = ActiveRecoveryLease(readback_episode.published,
                state["fan"], episode, request_snapshot().level,
                episode.original_deadline if episode is not None else hold.last_good + 30)
        try:
            return await drain_owned(asyncio.to_thread(execute))
        finally:
            readback_episode.active_recovery = None

    async def shutdown():
        state["force_max"], state["stop"] = True, True
        await state["qr"].close()
        if state["settings_task"] is not None:
            try:
                await drain_owned(state["settings_task"])
            except Exception:
                pass
        try:
            await asyncio.wait_for(asyncio.gather(*state.get("tasks", [])), timeout=20)
            async with state["fan_lock"]:
                if state["device"] is not None:
                    await asyncio.wait_for(apply_fan(state["device"]), timeout=10)
        except Exception:
            pass
        if learning_enabled:
            try:
                state["learning"] = await disk_call(learning_checkpoint, dict(state["settings"]), time.time())
            except Exception:
                state["learning"] = dict(state["learning"], error="学习检查点保存失败，固定保护继续生效")
        state["disk"].shutdown(wait=False, cancel_futures=True)
        state["history_disk"].shutdown(wait=False, cancel_futures=True)

    async def cleanup(application):
        await drain_owned(shutdown())

    app.router.add_get("/", page)
    app.router.add_get("/ui.{ext:css|js}", asset)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/history", history)
    app.router.add_post("/api/settings", configure)
    app.router.add_post("/api/xiaomi/qr/{action:start|status|image|cancel|bind}", xiaomi_qr)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path(__file__).parent / "state")
    parser.add_argument("--plain-loopback", action="store_true")
    args = parser.parse_args()
    import fcntl

    state_fd = os.open(args.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fcntl.flock(state_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Keep the raw fd until OS exit, including executor threads after run_app returns.
    os.umask(0o077)
    context = None
    origins = None
    if args.plain_loopback:
        origins = {"http://127.0.0.1:8910"}
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.state / "tls.crt", args.state / "tls.key")
    web.run_app(create_app(args.state, origins), host="127.0.0.1" if args.plain_loopback else "0.0.0.0",
                port=8910, ssl_context=context, access_log=None, print=None)


if __name__ == "__main__":
    main()
