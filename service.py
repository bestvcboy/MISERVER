import argparse
import asyncio
import errno
import hashlib
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
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from miio import AirPurifier
from miio.exceptions import DeviceException
from miio.integrations.airpurifier.zhimi.airpurifier import OperationMode

from core import (Controller, HighWindAuthority, MotionAuthority, NoAscent, Measured80,
    SourceFaultProtection, FailedDescentRestoration, HistoryLimit, Journal, MAX_LEVEL, HIGH_WIND_TTL, ThermalView, normalize)
from learning import BUCKET, Learning, WINDOW, checkpoint_limit, context_of
from policy_memory import ThermalMemoryGate, ThermalMemoryStore, memory_digest
from thermal_policy import source_timing
from qr_connector import BIND_CONSENT, START_CONSENT, QRError, QRConnector, strict_json

HOSTS = tuple(f"192.168.3.{n}" for n in (180, 181, 183, 184))
DEVICE_IP, DEVICE_MODEL, DEVICE_ID = "192.168.3.24", "zhimi.airpurifier.m2", 70632677
DEVICE_MAC = "78:11:dc:47:87:bf"
STATE = web.AppKey("thermal_state", dict)
MOTOR_IDENTITY = {"ip": DEVICE_IP, "model": DEVICE_MODEL, "did": DEVICE_ID, "mac": DEVICE_MAC}
logging.getLogger("miio").setLevel(100)


def atomic_write(path, text, *, before_replace=None):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".pending-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
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
class StorageHealth:
    phase: str = "healthy"
    first_failure: float | None = None
    consecutive_failures: int = 0
    first_success: float | None = None
    recovery_successes: int = 0
    last_error: dict | None = None
    recovery_revision: int | None = None
    failure_epoch: int = 0

    def refresh(self, now, *, hold_eligible):
        if self.phase == "holding" and (not hold_eligible or now - self.first_failure >= 9
                                        or self.consecutive_failures >= 3):
            self.phase = "protect"
        return self.phase

    def failure(self, now, wall, error, write_phase, committed, *, hold_eligible):
        self.failure_epoch += 1
        transient = isinstance(error, TimeoutError) or isinstance(error, OSError) and error.errno in (
            errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ETIMEDOUT)
        if self.phase == "healthy":
            self.first_failure = now
            self.consecutive_failures = 0
            self.phase = "holding"
        self.consecutive_failures += 1
        self.first_success = None
        self.recovery_successes = 0
        self.recovery_revision = None
        category = type(error).__name__ if type(error).__module__ == "builtins" else "OtherError"
        self.last_error = dict(category=category, errno=error.errno if isinstance(error, OSError) else None,
                               write_phase=write_phase, committed=committed, mono=now, wall=wall)
        if not transient or committed is not False or write_phase not in ("prepare", "prune"):
            self.phase = "protect"
        return self.refresh(now, hold_eligible=hold_eligible)

    def success(self, now, *, hold_eligible, verified_revision):
        self.refresh(now, hold_eligible=hold_eligible)
        if self.phase == "healthy":
            return
        self.consecutive_failures = 0
        if verified_revision is None:
            self.first_success = None
            self.recovery_successes = 0
            self.recovery_revision = None
            return
        if self.recovery_revision != verified_revision:
            self.first_success = None
            self.recovery_successes = 0
            self.recovery_revision = verified_revision
        if self.first_success is None:
            self.first_success = now
            self.recovery_successes = 1
        elif now - self.first_success >= 3:
            self.recovery_successes = 2
            self.phase = "healthy"
            self.first_failure = None
            self.first_success = None
            self.recovery_revision = None

    def status(self):
        return dict(phase=self.phase, first_failure_mono=self.first_failure,
                    consecutive_failures=self.consecutive_failures,
                    recovery_successes=self.recovery_successes, last_error=self.last_error)


@dataclass
class LegacyBudget:
    raw: bytes
    metadata: dict
    sha256: str
    exhausted: bool


def budget_metadata(info):
    return {key: getattr(info, key) for key in ('st_dev', 'st_ino', 'st_mode', 'st_nlink',
        'st_uid', 'st_gid', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}


@dataclass
class MotorSchedule:
    last_attempt: float = -math.inf
    last_decrease: float = -math.inf
    last_increase: float = -math.inf
    attempts: int = 0
    expected_level: int = None
    fault: bool = False
    budget_path: Path = field(default=None, repr=False)
    persistence_error: str = ""
    schema_version: int = 2
    legacy: LegacyBudget | None = field(default=None, repr=False)

    @property
    def legacy_pending(self):
        return self.legacy is not None and self.legacy.exhausted

    def _validate_legacy_source(self):
        source = self.legacy
        if source is None:
            return
        before = self.budget_path.lstat()
        if (budget_metadata(before) != source.metadata or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1 or before.st_mode & 0o022
                or hasattr(os, 'getuid') and before.st_uid != os.getuid()):
            raise ValueError('legacy source changed')
        descriptor = os.open(self.budget_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
            | getattr(os, 'O_NONBLOCK', 0))
        try:
            if budget_metadata(os.fstat(descriptor)) != source.metadata:
                raise ValueError('legacy source changed')
            raw = os.read(descriptor, 4097)
            if raw != source.raw or budget_metadata(os.fstat(descriptor)) != source.metadata:
                raise ValueError('legacy source changed')
        finally:
            os.close(descriptor)
        if budget_metadata(self.budget_path.lstat()) != source.metadata:
            raise ValueError('legacy source changed')

    def _archive_legacy(self):
        source = self.legacy
        if source is None:
            return
        self._validate_legacy_source()
        archive = self.budget_path.parent / ('.legacy-budget-' + source.sha256)
        archive.mkdir(mode=0o700, exist_ok=True)
        info = archive.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or hasattr(os, 'getuid') and info.st_uid != os.getuid()):
            raise ValueError('archive is not private')
        directory_fd = (os.open(archive, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if os.name == 'posix' else None)
        try:
            self._archive_files(archive, source, info, directory_fd)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
        self._validate_legacy_source()

    def _archive_files(self, archive, source, info, directory_fd):
        def directory_unchanged():
            linked = archive.lstat()
            if ((linked.st_dev, linked.st_ino, linked.st_mode, linked.st_uid)
                    != (info.st_dev, info.st_ino, info.st_mode, info.st_uid)):
                raise ValueError('archive directory changed')
            if directory_fd is not None:
                opened = os.fstat(directory_fd)
                if ((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid)
                        != (info.st_dev, info.st_ino, info.st_mode, info.st_uid)):
                    raise ValueError('archive directory changed')
        directory_unchanged()
        relative = {'dir_fd': directory_fd} if directory_fd is not None else {}
        provenance = json.dumps(dict(metadata=source.metadata, sha256=source.sha256), sort_keys=True).encode()
        for name, raw in (('source.bin', source.raw), ('provenance.json', provenance)):
            path = archive / name
            open_path = name if directory_fd is not None else path
            def linked_metadata():
                return budget_metadata(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if directory_fd is not None else path.lstat())
            directory_unchanged()
            try:
                descriptor = os.open(open_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, 'O_NOFOLLOW', 0), 0o600, **relative)
            except FileExistsError:
                descriptor = os.open(open_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                    | getattr(os, 'O_NONBLOCK', 0), **relative)
                try:
                    existing = os.fstat(descriptor)
                    if (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
                            or existing.st_mode & 0o077 or existing.st_size != len(raw)
                            or hasattr(os, 'getuid') and existing.st_uid != os.getuid()
                            or os.read(descriptor, len(raw) + 1) != raw
                            or budget_metadata(os.fstat(descriptor)) != budget_metadata(existing)
                            or linked_metadata() != budget_metadata(existing)):
                        raise ValueError('archive conflict')
                    directory_unchanged()
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            else:
                with os.fdopen(descriptor, 'wb') as stream:
                    created = os.fstat(stream.fileno())
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                    final = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(final.st_mode) or final.st_nlink != 1
                            or final.st_mode & 0o077 or final.st_size != len(raw)
                            or hasattr(os, 'getuid') and final.st_uid != os.getuid()
                            or (created.st_dev, created.st_ino) != (final.st_dev, final.st_ino)
                            or linked_metadata() != budget_metadata(final)):
                        raise ValueError('archive changed')
            directory_unchanged()
        if directory_fd is not None:
            os.fsync(directory_fd)
        else:
            fsync_directory(archive)
        fsync_directory(archive.parent)
        directory_unchanged()
        self._validate_legacy_source()

    def _retire_legacy(self, qualified):
        def before_replace():
            self._validate_legacy_source()
            if not qualified():
                raise ValueError('legacy admission changed')
        try:
            self._archive_legacy()
            if not qualified():
                return False
            self._validate_legacy_source()
            atomic_write(self.budget_path, json.dumps(dict(version=2, identity=MOTOR_IDENTITY,
                attempts=0, expected_level=None, fault=False)), before_replace=before_replace)
            fsync_directory(self.budget_path.parent)
        except (OSError, ValueError):
            self.persistence_error = 'legacy_archive_or_migration_failed'
            return False
        self.legacy = None
        self.schema_version = 2
        self.attempts, self.expected_level, self.fault = 0, None, False
        return True

    def persist_debt_then_set_budget(self, attempts, expected_level, fault=False):
        if self.legacy_pending:
            return False
        try:
            if self.budget_path is not None:
                self._archive_legacy()
                self._validate_legacy_source()
                durable_debt = {"version": 2, "identity": MOTOR_IDENTITY, "attempts": attempts,
                          "expected_level": expected_level, "fault": fault}
                if attempts == 0 and (self.attempts or self.fault or self.persistence_error):
                    durable_debt.update(attempts=self.attempts, expected_level=self.expected_level,
                                  fault=self.fault or self.attempts >= 2)
                    if self.attempts and durable_debt["expected_level"] is None:
                        durable_debt["expected_level"] = MAX_LEVEL
                if self.legacy is not None:
                    atomic_write(self.budget_path, json.dumps(durable_debt),
                        before_replace=self._validate_legacy_source)
                else:
                    atomic_write(self.budget_path, json.dumps(durable_debt))
                fsync_directory(self.budget_path.parent)
        except (OSError, ValueError):
            self.attempts = max(self.attempts, attempts)
            self.fault = True
            self.persistence_error = "执行预算未能可靠保存，保持只读；仅当前授权安全上限的有效读回及可靠保存可恢复"
            if self.budget_path is not None and self.legacy is None:
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
        self.legacy = None
        self.schema_version = 2
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
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4096
                        or before.st_mode & 0o022 or hasattr(os, 'getuid') and before.st_uid != os.getuid()):
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
                        or type(record["version"]) is not int or record["version"] not in (1, 2) or record["identity"] != MOTOR_IDENTITY
                        or type(record["attempts"]) is not int or not 0 <= record["attempts"] <= 2
                        or type(record["fault"]) is not bool
                        or (record["expected_level"] is not None and (type(record["expected_level"]) is not int
                            or not 0 <= record["expected_level"] <= MAX_LEVEL))
                        or (record["attempts"] == 0) != (record["expected_level"] is None)):
                    raise ValueError
                schedule.attempts, schedule.expected_level = record["attempts"], record["expected_level"]
                schedule.fault = record["fault"] or schedule.attempts == 2
                schedule.schema_version = record['version']
                if record['version'] == 1:
                    schedule.legacy = LegacyBudget(raw, budget_metadata(before), hashlib.sha256(raw).hexdigest(),
                        schedule.attempts == 2 and record['fault'] is True)
                    if schedule.legacy_pending:
                        schedule.fault = False
                if schedule.fault and schedule.attempts < 2:
                    schedule.persistence_error = "执行预算处于持久只读故障，需当前授权安全上限的有效读回及可靠保存恢复"
        except (OSError, ValueError, UnicodeError, RecursionError):
            schedule.attempts, schedule.fault = 2, True
            schedule.persistence_error = "执行预算缺失、损坏或不安全，保持只读；仅当前授权安全上限的有效读回及可靠保存可恢复"
        finally:
            schedule.last_attempt = schedule.last_decrease = schedule.last_increase = time.monotonic()
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

    def accept(self, result, now, requested, *, eligible, safe_level=None, limit=MAX_LEVEL, anchor=None):
        old = self.published
        stamp = result.get("observed_mono")
        physical = (eligible and result.get("fault") is False and result.get("power") == "on"
                   and result.get("mode") == "favorite" and type(result.get("level")) is int
                   and 0 <= result["level"] <= MAX_LEVEL
                   and type(result.get("rpm")) in (int, float) and math.isfinite(result["rpm"])
                   and result["rpm"] > 0 and type(stamp) in (int, float)
                   and math.isfinite(stamp) and 0 <= now - stamp < 9)
        accepted_stamp = min(stamp, anchor) if physical and anchor is not None else stamp
        matched = physical and result.get("confirmed") is True and result.get("execution") == "matched" and result["level"] == requested
        if physical and old.failures:
            if (matched and old.recovering_at is not None and stamp - old.recovering_at >= 3
                    and result["level"] >= min(limit, self.assessment(now).hold_level)):
                self.published = ReadbackAssessment(requested, accepted_stamp)
            elif old.recovering_at is None and stamp > old.last_good:
                self.published = ReadbackAssessment(old.hold_level, old.last_good, old.failures, stamp)
        elif matched:
            self.published = ReadbackAssessment(requested, accepted_stamp)
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
    motion: MotionAuthority | None = None
    snapshot: object = None


@dataclass(frozen=True)
class ActuationSnapshot:
    generation: int
    settings_revision: int
    operation: object
    operation_status: str | None
    original_deadline: float | None
    proof: HighWindAuthority | None
    normal_ceiling: int
    effective_target: int
    next_command: int
    transition_target: int | None
    endpoint: tuple
    memory_digest: str | None
    acknowledged_digest: str | None
    memory_revision: int
    storage_state: str
    budget_identity: tuple
    blocker: str | None
    thermal_view: ThermalView | None = None

    @property
    def needs_durable_intent(self):
        actual = self.endpoint[2]
        return (type(actual) is not int or max(actual, self.next_command) > 10
            or self.next_command < actual)


@dataclass(frozen=True)
class BudgetReservation:
    attempts: int
    level: int
    last_attempt: float
    snapshot: ActuationSnapshot | None = None

    def owns_second_attempt(self, schedule):
        return (self.attempts == 2 and schedule.fault and not schedule.persistence_error
            and (schedule.attempts, schedule.expected_level, schedule.last_attempt)
                == (self.attempts, self.level, self.last_attempt))


@dataclass(frozen=True)
class Submitted:
    dispatch_id: int


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class SubmissionUncertain:
    dispatch_id: int
    reason: str


class AdmissionBoundary:
    def __init__(self):
        self.lock = threading.Lock()
        self.dispatches = deque(maxlen=64)
        self.serial = 0

    def retire_legacy(self, schedule, current, direct_read):
        with self.lock:
            if (not schedule.legacy_pending or schedule.persistence_error
                    or time.monotonic() - schedule.last_attempt < 8):
                return False
            snapshot = current().snapshot
            try:
                actual, stamp, _ = direct_read()
            except Exception:
                return False
            def qualified():
                fresh = current().snapshot
                now = time.monotonic()
                view = snapshot.thermal_view if snapshot is not None else None
                return (snapshot is not None and fresh == snapshot and not snapshot.blocker
                    and snapshot.budget_identity == tuple(sorted(MOTOR_IDENTITY.items()))
                    and snapshot.storage_state == 'healthy'
                    and isinstance(snapshot.memory_digest, str)
                    and snapshot.memory_digest == snapshot.acknowledged_digest
                    and (snapshot.original_deadline is None or now < snapshot.original_deadline)
                    and actual[:2] == ['on', 'favorite'] and type(actual[2]) is int
                    and 0 <= actual[2] <= MAX_LEVEL and type(actual[3]) in (int, float)
                    and math.isfinite(actual[3]) and actual[3] > 0 and 0 <= now - stamp < 6
                    and isinstance(view, ThermalView) and view.generation == snapshot.generation
                    and view.complete and not view.faults
                    and {h for h, _ in view.inventory} == set(HOSTS)
                    and {key[0] for key, _, _ in view.observations} == set(HOSTS)
                    and type(view.maximum_c) in (int, float) and math.isfinite(view.maximum_c)
                    and view.maximum_c < 80 and type(view.expires_mono) in (int, float)
                    and math.isfinite(view.expires_mono) and now < view.expires_mono
                    and all(type(temp) in (int, float) and math.isfinite(temp) and temp < 80
                        and type(observed) in (int, float) and math.isfinite(observed)
                        and 0 <= now - observed < 6 for _, temp, observed in view.observations))
            return qualified() and schedule._retire_legacy(qualified)

    @asynccontextmanager
    async def publication(self):
        # A canceled async waiter must not strand a lock acquired by its worker.
        # Handoff (or release on cancellation) occurs on the event loop itself.
        loop = asyncio.get_running_loop()
        acquisition = loop.create_future()
        def handoff():
            if acquisition.cancelled():
                self.lock.release()
            else:
                acquisition.set_result(True)
        def acquire():
            self.lock.acquire()
            try:
                loop.call_soon_threadsafe(handoff)
            except RuntimeError:
                self.lock.release()
        worker = loop.run_in_executor(None, acquire)
        try:
            await acquisition
        except asyncio.CancelledError:
            if acquisition.done() and not acquisition.cancelled():
                self.lock.release()
            raise
        try:
            yield
        finally:
            self.lock.release()

    def submit_guarded(self, command, expected_snapshot, reservation, schedule, current,
                       validate, sdk_mutation, args, *, high_wind_gate=True):
        with self.lock:
            request = current()
            snapshot = request.snapshot
            now = time.monotonic()
            if expected_snapshot is not None and snapshot != expected_snapshot:
                return Refused('actuation_snapshot_changed')
            if (schedule.persistence_error or reservation is None
                    or (schedule.attempts, schedule.expected_level, schedule.last_attempt)
                        != (reservation.attempts, reservation.level, reservation.last_attempt)):
                return Refused('durable_motor_reservation_changed')
            if snapshot is not None:
                if snapshot.blocker:
                    return Refused(snapshot.blocker)
                if (snapshot.original_deadline is not None and now >= snapshot.original_deadline):
                    return Refused('original_deadline_expired')
                if snapshot.needs_durable_intent and snapshot.memory_digest != snapshot.acknowledged_digest:
                    return Refused('current_memory_not_acknowledged')
            reason = validate(request, command, now)
            if reason:
                return Refused(reason)
            entry_now = time.monotonic()
            if snapshot is not None and (snapshot.original_deadline is not None
                    and entry_now >= snapshot.original_deadline):
                return Refused('original_deadline_expired')
            if high_wind_gate and snapshot is not None and max(command[1], snapshot.endpoint[2] if type(snapshot.endpoint[2]) is int else 0) > 10:
                if request.high_wind is None or not request.high_wind.valid_at(entry_now):
                    return Refused('proof_expired_at_sdk_entry')
                if (isinstance(request.high_wind.proof, NoAscent)
                        or snapshot.endpoint[:2] == ('on', 'favorite')):
                    stamp = snapshot.endpoint[4]
                    if type(stamp) not in (int, float) or not 0 <= entry_now - stamp < 6:
                        return Refused('endpoint_expired_at_sdk_entry')
                    rpm = snapshot.endpoint[3]
                    if type(rpm) not in (int, float) or not math.isfinite(rpm) or rpm <= 0:
                        return Refused('running_endpoint_invalid_at_sdk_entry')
            self.serial += 1
            dispatch = dict(dispatch_id=self.serial, command=command, snapshot=snapshot,
                submitted_mono=entry_now, state='submitted')
            self.dispatches.append(dispatch)
            try:
                sdk_mutation(*args)
            except Exception:
                dispatch['state'] = 'submission_uncertain'
                return SubmissionUncertain(self.serial, 'sdk_send_uncertain')
            return Submitted(self.serial)


def fan_apply(device, wanted, schedule, *, read_only=None, high_wind_gate=True, admission=None):
    admission = admission or AdmissionBoundary()
    ceiling = MAX_LEVEL
    scopes = []
    reservation = None
    def target():
        nonlocal ceiling
        value = (wanted(reservation=reservation) if reservation is not None
            and reservation.snapshot is not None else wanted())
        request = value if isinstance(value, FanRequest) else FanRequest(value)
        if type(request.level) is not int or not 0 <= request.level <= MAX_LEVEL:
            raise ValueError("请求档位必须为 0 到 16 的整数")
        if type(request.limit) is not int or not 10 <= request.limit <= MAX_LEVEL:
            raise ValueError("风力授权上限无效")
        authority = request.high_wind if isinstance(request.high_wind, HighWindAuthority) else HighWindAuthority()
        if high_wind_gate:
            scopes.append(authority)
        ceiling = min(ceiling, request.limit, authority.ceiling_at(time.monotonic()) if high_wind_gate else MAX_LEVEL)
        return FanRequest(min(request.level, ceiling), request.protected, request.release,
                          request.read_only_floor, request.upward_only, ceiling, request.high_wind, request.motion,
                          request.snapshot)

    def destination(request):
        candidate = movement(request)
        if high_wind_gate and max(actual[2], candidate) > 10 and candidate != actual[2]:
            if not all(authority.permits(time.monotonic(), actual[2], candidate) for authority in scopes):
                return actual[2]
        return candidate

    def movement(request):
        if high_wind_gate and request.level > actual[2]:
            motion = request.motion
            emergency = (isinstance(motion, MotionAuthority) and motion.source is not None
                and type(motion.observed_mono) in (int, float) and math.isfinite(motion.observed_mono)
                and 0 <= time.monotonic() - motion.observed_mono < 6
                and type(motion.actual_c) in (int, float) and type(motion.critical_c) in (int, float)
                and math.isfinite(motion.actual_c) and math.isfinite(motion.critical_c)
                and motion.critical_c >= 75 and motion.actual_c >= motion.critical_c)
            if isinstance(request.high_wind.proof if request.high_wind else None, Measured80):
                emergency = False
            restoration = (isinstance(motion, MotionAuthority) and type(motion.restore_level) is int
                and motion.restore_from == actual[2] and request.level == min(request.limit, motion.restore_level)
                and type(motion.deadline) in (int, float) and math.isfinite(motion.deadline)
                and 0 < motion.deadline - time.monotonic() <= 60)
            if not emergency and not restoration:
                if time.monotonic() - schedule.last_increase < 10:
                    return actual[2]
                if isinstance(request.high_wind.proof if request.high_wind else None, Measured80):
                    return min(request.level, 10 if actual[2] < 10 else actual[2] + 1)
                return min(request.level, actual[2] + 2)
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

    def activation_allowed(request):
        candidate = destination(request)
        return (not high_wind_gate or all(authority.permits_activation(
            time.monotonic(), actual[2], candidate) for authority in scopes))

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
        if schedule.attempts >= 2 and not schedule.legacy_pending:
            schedule.fault = True
            execution = "fault"
        if schedule.persistence_error:
            error, execution = schedule.persistence_error, "fault"
            result["verified_at"] = None
            result["observed_mono"] = None
        result.update(error=error, execution=execution, attempts=schedule.attempts, fault=schedule.fault)
        result['legacy_migration_pending'] = schedule.legacy_pending
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
    if schedule.legacy_pending:
        def migration_readback():
            nonlocal actual, observed_mono, observed_wall, running
            actual, observed_mono, observed_wall = readback()
            running = observed(actual, observed_mono, observed_wall)
            return actual, observed_mono, observed_wall
        if not admission.retire_legacy(schedule, target, migration_readback):
            return finish('legacy_migration_pending_fresh_requalification')
        result['protection_interrupted'] = False
        request = target()
        level = request.level
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
    if not schedule.persistence_error and running and actual[2] == schedule.expected_level:
        if not schedule.persist_debt_then_set_budget(0, None):
            return finish(execution="fault")
    if schedule.fault:
        return finish("两次修正未确认，故障锁定；仅观察实际匹配且转动的状态以恢复", "fault")
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
    if not running and not activation_allowed(latest):
        return finish("高档保持仅限只读，无当前危急授权不得开机或切换模式")
    if level < actual[2] and now - schedule.last_decrease < 30:
        return finish("距离上次执行降档不足三十秒，最新请求待执行")
    if actual[:3] == ["on", "favorite", level]:
        return finish("最新请求已合并，等待转速或下一步降档")
    if latest.snapshot is not None and latest.snapshot.blocker:
        return finish(latest.snapshot.blocker)
    schedule.last_attempt = time.monotonic()
    reserved_snapshot = latest.snapshot
    reservation = BudgetReservation(schedule.attempts + 1, level, schedule.last_attempt, reserved_snapshot)
    if not schedule.persist_debt_then_set_budget(schedule.attempts + 1, level, schedule.attempts + 1 >= 2):
        return finish(execution="fault")
    if (schedule.attempts, schedule.expected_level, schedule.last_attempt) != (
            reservation.attempts, reservation.level, reservation.last_attempt):
        return finish('durable_motor_reservation_changed')
    result["protection_interrupted"] = result["protection_interrupted"] or schedule.fault
    result["verified_at"] = None
    result["observed_mono"] = None
    result.update(ambient_c=None, ambient_source=None, ambient_observed_mono=None, ambient_verified_at=None)
    def validate_entry(request, command, now):
        authority = request.high_wind or HighWindAuthority()
        if (high_wind_gate and max(actual[2], command[1]) > 10 and actual[:2] == ['on', 'favorite']
                and (not running or not 0 <= now - observed_mono < 6)):
            return 'running_endpoint_invalid'
        if request.snapshot is not None and isinstance(authority.proof,
                (Measured80, SourceFaultProtection, FailedDescentRestoration, NoAscent)):
            if authority.proof.generation != request.snapshot.generation:
                return 'source_generation_changed'
        if command[0] in ('on', 'mode'):
            if not activation_allowed(request):
                return 'activation_revoked'
        elif ((destination(request) != command[1] if high_wind_gate else command[1] > request.limit)
                or schedule.expected_level != command[1]
                or command[1] < actual[2] and now - schedule.last_decrease < 30):
            return 'exact_command_revoked'
        if high_wind_gate and max(actual[2], command[1]) > 10:
            if not all(scope.valid_at(now) and scope.permits(now, actual[2], command[1]) for scope in scopes):
                return 'high_wind_proof_revoked'
        if request.snapshot is not None and isinstance(authority.proof, NoAscent):
            if (command[0] != 'level' or request.snapshot.operation is not authority.proof.operation
                    or authority.proof.generation != request.snapshot.generation):
                return 'exact_ceiling_exit_required'
        return None

    def mutate(sdk_mutation, *args, command, decrease=False, increase=False):
        nonlocal reservation
        expected = latest.snapshot
        try:
            submission = admission.submit_guarded(command, expected, reservation, schedule,
                target, validate_entry, sdk_mutation, args, high_wind_gate=high_wind_gate)
            result['submission'] = dict(state=type(submission).__name__,
                dispatch_id=getattr(submission, 'dispatch_id', None), reason=getattr(submission, 'reason', None))
            return submission
        finally:
            if 'submission' in locals() and not isinstance(submission, Refused):
                schedule.last_attempt = time.monotonic()
            if decrease and 'submission' in locals() and not isinstance(submission, Refused):
                schedule.last_decrease = schedule.last_attempt
            if increase and 'submission' in locals() and not isinstance(submission, Refused):
                schedule.last_increase = schedule.last_attempt
            if 'submission' in locals() and not isinstance(submission, Refused):
                reservation = replace(reservation, last_attempt=schedule.last_attempt)
    try:
        if actual[0] != "on":
            if not activation_allowed(target()):
                return finish("开机前授权已收窄，保持只读")
            submission = mutate(device.on, command=('on', level))
            if not isinstance(submission, Submitted):
                return finish(getattr(submission, 'reason', 'submission_uncertain'))
        if actual[1] != "favorite":
            if not activation_allowed(target()):
                return finish("切换模式前授权已收窄，保持只读")
            submission = mutate(device.set_mode, OperationMode.Favorite, command=('mode', level))
            if not isinstance(submission, Submitted):
                return finish(getattr(submission, 'reason', 'submission_uncertain'))
        while True:
            latest = target()
            if reserved_snapshot is not None and (latest.snapshot != reserved_snapshot or latest.snapshot.blocker):
                return finish('actuation_snapshot_changed_after_budget')
            next_level = destination(latest)
            level = next_level if high_wind_gate else min(latest.limit, max(level, next_level))
            if level < actual[2] and time.monotonic() - schedule.last_decrease < 30:
                level = actual[2]
            if schedule.expected_level == level:
                break
            if not schedule.persist_debt_then_set_budget(schedule.attempts, level, schedule.fault):
                return finish(execution="fault")
            reservation = replace(reservation, level=level)
            if (schedule.attempts, schedule.expected_level, schedule.last_attempt) != (
                    reservation.attempts, reservation.level, reservation.last_attempt):
                return finish('durable_motor_reservation_changed')
        if actual[2] != level:
            submission = mutate(device.set_favorite_level, level, command=('level', level),
                decrease=level < actual[2], increase=level > actual[2])
            if not isinstance(submission, Submitted):
                return finish(getattr(submission, 'reason', 'submission_uncertain'))
        elif reserved_snapshot is not None:
            return finish('reserved_command_revoked')
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
    fixed_storage = not learning_enabled and high_wind_gate
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
    state = {"settings": settings, "nodes": [], "decision": {"level": MAX_LEVEL, "reasons": ["启动保护风力请求"]},
             "fan": {"configured": False, "confirmed": False, "level": None, "rpm": None,
                     "fault": motor_schedule.fault, "attempts": motor_schedule.attempts,
                     "legacy_migration_pending": motor_schedule.legacy_pending,
                     "error": motor_schedule.persistence_error or ('legacy_migration_pending_fresh_requalification'
                         if motor_schedule.legacy_pending else "尚未绑定本地执行凭据，无法执行风力控制")}, "storage_error": "",
             "settings_error": "", "settings_task": None, "storage_health": StorageHealth(),
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
    memory = (ThermalMemoryGate(ThermalMemoryStore(root,
        dict(device=MOTOR_IDENTITY, hosts=list(HOSTS)), atomic_write, fsync_directory))
        if fixed_storage else None)
    state["thermal_memory"] = memory
    admission = AdmissionBoundary()
    state['admission'] = admission
    if memory is not None:
        await memory.initialize(state["controller"], time.monotonic())

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

    def protect(reason="服务或执行条件异常，重新建立保护风力基线"):
        now = time.monotonic()
        controller = state["controller"]
        if memory is not None:
            memory.invalidate()
        decision = controller.protect(reason, now)
        if memory is not None:
            memory.publish(controller)
        state["decision"].update(decision)
        limit = controller.effective_limit(now)
        state["fan_request"] = FanRequest(limit, protected=True, limit=limit,
                                          high_wind=controller.high_wind, motion=controller.motion)
        return decision

    def fan_healthy(reservation=None):
        observed = state["fan"]
        gear, rpm, stamp = observed.get("level"), observed.get("rpm"), observed.get("verified_at")
        mono = observed.get("observed_mono")
        reserved_second_attempt = (reservation is not None and reservation.owns_second_attempt(state['motor_schedule']))
        return (observed.get("configured") and (not state["motor_schedule"].fault or reserved_second_attempt)
                and not observed.get("fault") and observed.get("execution") != "fault"
                and observed.get("power") == "on" and observed.get("mode") == "favorite"
                and type(gear) is int and 0 <= gear <= MAX_LEVEL
                and type(rpm) in (int, float) and math.isfinite(rpm) and rpm > 0
                and type(stamp) in (int, float) and 0 <= time.time() - stamp <= 9
                and type(mono) in (int, float) and math.isfinite(mono) and 0 <= time.monotonic() - mono <= 9
                and 0 <= time.monotonic() - state["last_fan"] <= 9)

    def storage_phase():
        if not fixed_storage:
            return "healthy", False
        now = time.monotonic()
        controller = state["controller"]
        hold_eligible = (fan_healthy() and state["fan"].get("confirmed") is True
                    and state["fan"].get("execution") == "matched"
                    and state["fan"].get("level") == controller.operating.confirmed_level
                    and controller.storage_safe(now) and 0 <= now - state["last_poll"] <= 6
                    and state["settings_task"] is None
                    and not state["motor_schedule"].attempts and not state["motor_schedule"].persistence_error
                    and not state["settings_error"] and not state["pairing"])
        phase = state["storage_health"].refresh(now, hold_eligible=hold_eligible)
        return phase, hold_eligible

    def storage_hard():
        phase, _ = storage_phase()
        return phase == "protect" or bool(state["storage_error"]) and phase == "healthy"

    def merged_request(reservation=None):
        base = state["fan_request"]
        current = state["controller"].high_wind if high_wind_gate else None
        authority = base.high_wind if current == base.high_wind else None
        limit = authority.ceiling_at(time.monotonic()) if high_wind_gate and authority is not None else 10 if high_wind_gate else MAX_LEVEL
        hold = readback_hold(time.monotonic(), reservation)
        schedule, actual = state['motor_schedule'], state['fan'].get('level')
        unconfirmed_protective_ascent = (schedule.attempts and type(actual) is int
            and type(schedule.expected_level) is int and schedule.expected_level > actual
            and schedule.expected_level != base.level)
        protected = bool(state["force_max"] or storage_hard()
                or unconfirmed_protective_ascent
                or state["settings_error"] or state["pairing"] or state["settings_task"] is not None
                or time.monotonic() - state["last_poll"] > 6 or not fan_healthy(reservation) and hold is None)
        if protected:
            return FanRequest(limit, protected=True, limit=limit, high_wind=authority, motion=base.motion)
        ceiling_exit = (authority is not None and isinstance(authority.proof, NoAscent)
            and authority.proof.generation is not None)
        if ceiling_exit:
            ready = (storage_phase()[0] == 'healthy' and (memory is None or memory.ready))
            return FanRequest(base.level if ready else state['fan'].get('level', base.level),
                upward_only=not ready, limit=limit, high_wind=authority, motion=base.motion)
        level = min(limit, max(base.level, state["settings"]["manual_level"] or 0,
                    hold.hold_level if hold else 0))
        if storage_phase()[0] == "holding" or memory is not None and not memory.ready:
            observed_level = state["fan"].get("level")
            return FanRequest(max(level, observed_level if type(observed_level) is int else level), upward_only=True,
                              limit=limit, high_wind=authority, motion=base.motion)
        if hold is not None:
            return FanRequest(level, upward_only=True, limit=limit, high_wind=authority)
        release = (base.release if base.level == level and not base.protected
                   and state["settings"]["manual_level"] is None else None)
        return FanRequest(level, release=release, limit=limit, high_wind=authority, motion=base.motion)

    def request_snapshot(reservation=None):
        request = merged_request(reservation)
        controller, observed = state['controller'], state['fan']
        operation = controller._policy['operation']
        episode = operation.get('episode') if operation else None
        phase = storage_phase()[0]
        digest = memory_digest(controller.export_thermal_memory()) if memory is not None else None
        acknowledged = memory.acknowledged_digest if memory is not None else None
        actual = observed.get('level')
        needs_intent = type(actual) is not int or max(actual, request.level) > 10 or request.level < actual
        blocker = ('settings_revision_pending' if state['settings_task'] is not None or state['settings_error'] else
            'pairing' if state['pairing'] else 'storage_' + phase if phase != 'healthy' or storage_hard() else
            'memory_conflict' if controller._thermal_memory.saturated else
            'memory_failed' if memory is not None and (memory.error or memory.load_failed) else
            'current_memory_not_acknowledged' if needs_intent and memory is not None and (not memory.ready
                or memory.desired_digest != digest or digest != acknowledged) else None)
        snapshot = ActuationSnapshot(controller.thermal_view.generation, state['settings_revision'],
            episode, operation.get('status') if operation else None,
            episode.original_deadline if episode is not None else None, request.high_wind,
            controller.effective_limit(time.monotonic()), state['decision'].get('demand', request.level),
            request.level, state['decision'].get('transition_target'),
            tuple(observed.get(k) for k in ('power', 'mode', 'level', 'rpm', 'observed_mono')),
            digest, acknowledged, controller._thermal_memory.revision, phase,
            tuple(sorted(MOTOR_IDENTITY.items())), blocker, controller.thermal_view)
        state['actuation_snapshot'] = snapshot
        return replace(request, snapshot=snapshot)

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
        state["fan_request"] = FanRequest(decision["level"], release=authority,
            high_wind=state["controller"].high_wind, motion=state["controller"].motion)

    def readback_hold(now, reservation=None):
        schedule = state["motor_schedule"]
        hold = readback_episode.assessment(now)
        controller = state["controller"]
        operation = controller._policy["operation"]
        lease = readback_episode.active_recovery
        reserved_second_attempt = reservation is not None and reservation.owns_second_attempt(schedule)
        restoring = (hold is not None and isinstance(lease, ActiveRecoveryLease)
            and lease.assessment is readback_episode.published and lease.fan is state["fan"]
            and lease.episode is (operation["episode"] if controller._pending() else None)
            and now < lease.deadline
            and min(controller.effective_limit(now), max(state["fan_request"].level, state["settings"]["manual_level"] or 0, hold.hold_level)) == lease.target
            and (schedule.attempts == 1 or reserved_second_attempt) and schedule.expected_level == lease.target)
        if (not controller.communication_safe(now)
                or schedule.fault and not reserved_second_attempt or schedule.persistence_error or (schedule.attempts
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
                   and fan_healthy() and observed.get("level") == requested_level
                   and state["controller"].feedback_matches_request(
                       requested_level, observed.get("observed_mono"), time.monotonic()))
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
                       or source_timing(node, now) is None
                       or (node["source_ts"] <= state["controller"].sources.get(node["host"], -math.inf) if advancing
                           else node["source_ts"] < state["controller"].sources.get(node["host"], -math.inf))
                       for node in nodes)):
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
                "high_wind_expires_mono": state["controller"].high_wind.expires_mono,
                "requested_level": requested_level, "settings_pending": state["settings_task"] is not None,
                "read_only": False, "control_mode": "auto" if state["settings"]["manual_level"] is None else "manual",
                "strategy_mode": "learning" if learning_enabled else "rules",
                "control_active": not state["stop"] and state["updated_at"] is not None and time.monotonic() - state["last_poll"] < 8,
                "history_recording": dict(state["history_recording"]),
                "thermal_memory": memory.status() if memory is not None else dict(state="not_applicable"),
                "storage_health": state["storage_health"].status() if fixed_storage else dict(
                    phase="protect" if state["storage_error"] else "healthy", bounded_protocol=False),
                "fan": current_fan(requested_level),
                "learning": (dict(state["learning"], enabled=True, active=active_power(state["nodes"], time.monotonic(), time.time()))
                             if learning_enabled else disabled_learning(state["learning"])),
                "server_time": time.time(), "period_seconds": 3,
                "control_period_seconds": 3, "record_period_seconds": 3,
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
        async def transaction():
            try:
                proposed = settings_checked(await request.json())
                async with admission.publication():
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
                        return actual, "设置保存结果未可靠确认，请求保护风力并遵守当前上限；请刷新状态后重新保存", "", None, None
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
                async with admission.publication():
                    if actual is not None:
                        state["settings"] = actual
                    state["settings_error"] = error
                    if maintenance_error:
                        if fixed_storage:
                            state["storage_health"].failure(time.monotonic(), time.time(), RuntimeError(),
                                "settings_maintenance", None, hold_eligible=False)
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
                async with admission.publication():
                    if state["settings_task"] is asyncio.current_task():
                        state["settings_task"] = None
        async with admission.publication():
            if state["stop"] or state["settings_task"] is not None:
                raise web.HTTPConflict(text="设置正在保存或服务正在停止，请刷新状态后重试")
            state["settings_revision"] += 1
            task = asyncio.create_task(transaction())
            state["settings_task"] = task
        return await drain_owned(task)

    async def pair_token(*, admit):
        async def transaction():
            async with state["pair_lock"]:
                credential = admit()
                async with admission.publication():
                    state["pairing"] = True
                    protect()
                try:
                    device = await asyncio.to_thread(device_from_token, credential)
                    await disk_call(atomic_write, root / "device-token", credential.lower())
                    async with state["fan_lock"]:
                        async with admission.publication():
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
                    async with admission.publication():
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
            return normalize(host, data, started, time.time())
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
                async with admission.publication():
                    minimum = state["settings"]["manual_level"]
                    power_thresholds = active_power(nodes, now, wall, advancing=True) if learning_enabled else {}
                    feedback = current_fan(wanted())
                    hold = readback_hold(now)
                    if hold is not None:
                        feedback["readback_hold"] = dict(level=hold.hold_level, failures=hold.failures,
                            recovering=hold.recovering_at is not None, stage=hold.stage,
                            until=hold.last_good + 30 if hold.stage != "lost" else now + 3)
                    feedback.update(motor_attempts=state["motor_schedule"].attempts,
                        motor_fault=state["motor_schedule"].fault or bool(state["motor_schedule"].persistence_error),
                        next_motor_at=max(state["motor_schedule"].last_attempt + 8,
                            state["motor_schedule"].last_decrease + 30))
                    if state["motor_schedule"].fault:
                        feedback["fault"] = True
                    if memory is not None:
                        memory.invalidate()
                    decision = state["controller"].decide(nodes, now, minimum_level=0 if minimum is None else minimum,
                                                           power_thresholds=power_thresholds,
                                                           feedback=feedback,
                                                           manual_level=minimum)
                    if not learning_enabled:
                        decision["learning_eligible"] = False
                    if storage_phase()[0] == "holding":
                        decision.update(state["controller"].pause_release("日志暂态中断，暂停下降观察", now))
                    if (storage_hard() or state["settings_error"] or not fan_healthy() and hold is None
                            or state["pairing"] or state["settings_task"] is not None):
                        decision.update(protect())
                    state.update(nodes=nodes, decision=decision, last_poll=now, updated_at=wall)
                    publish_request(decision, now)
                    if memory is not None:
                        memory.publish(state["controller"])
                        decision["thermal_memory"] = memory.status()
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
                    except asyncio.QueueFull as error:
                        if fixed_storage:
                            state["storage_health"].failure(time.monotonic(), time.time(), error, "queue", False, hold_eligible=False)
                        state["dropped_records"] += 1
                        state["storage_error"] = "磁盘写入滞后，采样记录丢失，保护风力请求"
                        protect()
                deadline += 3
                if deadline < now:
                    deadline = now + 3
                await asyncio.sleep(max(0, deadline - time.monotonic()))

    async def storage():
        def write(row, config, outcome):
            outcome.update(phase="prepare", committed=False)
            if learning_enabled:
                load_learning(config)
            budget = config["max_log_mb"] * 1024**2
            encoded = len(json.dumps(row, ensure_ascii=False).encode()) + 1
            if encoded > 65536:
                raise ValueError("记录过大")
            outcome["phase"] = "prune"
            state["journal"].prune(time.time(), config["retention_days"],
                                   budget - encoded - Journal.SEGMENT_BYTES - learning_reserve(config))
            try:
                outcome.update(phase="append", committed=None)
                state["journal"].append(row)
                outcome.update(phase="accounting", committed=True)
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
            outcome = dict(phase="dispatch", committed=False)
            try:
                config, revision = dict(state["settings"]), state["settings_revision"]
                dispatch_epoch = state["storage_health"].failure_epoch
                size, cached = await disk_call(write, row, config, outcome)
                async with admission.publication():
                    state["log_bytes"] = size
                    if fixed_storage:
                        _, hold_eligible = storage_phase()
                        if dispatch_epoch == state["storage_health"].failure_epoch:
                            verified_revision = (revision if outcome["phase"] == "accounting"
                                and outcome["committed"] is True and revision == state["settings_revision"]
                                and config == state["settings"] and state["settings_task"] is None
                                and not state["settings_error"] else None)
                            state["storage_health"].success(time.monotonic(), hold_eligible=hold_eligible,
                                verified_revision=verified_revision)
                            state["storage_error"] = ("" if state["storage_health"].phase == "healthy"
                                                      else "日志恢复观察中，暂停下降")
                    else:
                        state["storage_error"] = ""
                    if not learning_enabled:
                        state["learning"] = cached
                    elif (revision == state["settings_revision"] and row.get("learning", {}).get("revision") == revision
                            and row.get("ts") == state["updated_at"] and learning_healthy()
                            and context_of(state["nodes"]) == cached["eligibility"].get("context")):
                        state["learning"] = dict(cached, settings_revision=revision)
                    else:
                        state["learning"] = dict(cached, settings_revision=-1)
            except Exception as error:
                async with admission.publication():
                    _, hold_eligible = storage_phase()
                    phase = (state["storage_health"].failure(time.monotonic(), time.time(), error,
                        outcome["phase"], outcome["committed"], hold_eligible=hold_eligible) if fixed_storage else "protect")
                    state["storage_error"] = ("日志暂态中断，限时暂停下降" if phase == "holding" else
                                              "日志异常，保护风力请求。检查存储状态。")
                    if outcome["committed"] is False:
                        state["dropped_records"] += 1
                    if state["history_recording"]["writes"] == writes_before:
                        state["history_recording"].update(state="failed" if outcome["committed"] is False else "uncertain",
                            failed_writes=state["history_recording"]["failed_writes"] + 1,
                            error=state["storage_error"])
                    if phase == "holding":
                        if memory is not None:
                            memory.invalidate()
                        decision = state["controller"].pause_release(state["storage_error"], time.monotonic())
                        state["decision"].update(decision)
                        publish_request(decision, time.monotonic())
                        if memory is not None:
                            memory.publish(state["controller"])
                    else:
                        protect(state["storage_error"])
            finally:
                state["queue"].task_done()

    async def fan():
        token_path = root / "device-token"
        while not state["stop"]:
            async with state["fan_lock"]:
                schedule = state["motor_schedule"]
                thermal_safe = state["controller"].communication_safe(time.monotonic())
                eligible = (thermal_safe and not schedule.fault and not schedule.persistence_error and schedule.attempts == 0
                            and schedule.expected_level is None
                            and not state["controller"]._policy["startup_required"])
                if fan_healthy() and readback_episode.published.failures == 0:
                    readback_episode.accept(state["fan"], time.monotonic(), state["decision"]["level"], eligible=eligible,
                        anchor=state["controller"].communication_anchor if high_wind_gate else None)
                if not fan_healthy() and readback_hold(time.monotonic()) is None:
                    async with admission.publication():
                        protect("风机反馈中断，重新建立保护风力基线")
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
                        fan_result = await apply_fan(device)
                        async with admission.publication():
                            state['fan'] = fan_result
                        readback_episode.accept(state["fan"], time.monotonic(), state["decision"]["level"],
                            eligible=eligible and not schedule.fault and schedule.attempts == 0
                            and schedule.expected_level is None and not schedule.persistence_error,
                            limit=state["controller"].effective_limit(time.monotonic()),
                            anchor=state["controller"].communication_anchor if high_wind_gate else None,
                            safe_level=max(state["decision"]["level"],
                                state["controller"]._policy["operation"]["episode"].original_from
                                if state["controller"]._pending() else 0))
                        if state["fan"].get("protection_interrupted"):
                            async with admission.publication():
                                protect("执行期间触发保护，读回恢复后仍需重新建立安全基线")
                except Exception:
                    async with admission.publication():
                        readback_episode.published = ReadbackAssessment()
                        state["device"] = None
                        state["fan"] = {"configured": token_path.exists(), "confirmed": False,
                                        "level": None, "rpm": None, "fault": state["motor_schedule"].fault,
                                        "attempts": state["motor_schedule"].attempts,
                                        "error": state["motor_schedule"].persistence_error or "净化器连接、身份验证或读回检查未完成，保护风力请求尚未匹配确认"}
                state["last_fan"] = time.monotonic()
                if not fan_healthy() and readback_hold(time.monotonic()) is None:
                    async with admission.publication():
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
        if memory is not None:
            memory.publish(state["controller"])
        state["tasks"] = [asyncio.create_task(task()) for task in (monitor, storage, fan, watchdog)]

    async def apply_fan(device):
        def execute():
            interrupted = False
            def target(reservation=None):
                nonlocal interrupted
                request = request_snapshot(reservation)
                interrupted = interrupted or request.protected
                return request
            result = fan_apply(device, target, state["motor_schedule"], high_wind_gate=high_wind_gate,
                admission=admission)
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
        async with admission.publication():
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
        remaining = [task for task in state.get("tasks", []) if not task.done()]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        if learning_enabled:
            try:
                state["learning"] = await disk_call(learning_checkpoint, dict(state["settings"]), time.time())
            except Exception:
                state["learning"] = dict(state["learning"], error="学习检查点保存失败，固定保护继续生效")
        if memory is not None:
            await drain_owned(memory.flush(state["controller"]))
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
    web.run_app(create_app(args.state, origins, high_wind_gate=True), host="127.0.0.1" if args.plain_loopback else "0.0.0.0",
                port=8910, ssl_context=context, access_log=None, print=None)


if __name__ == "__main__":
    main()
