import ast
import atexit
import hashlib
import json
import math
import os
import re
import secrets
import stat
import struct
import sys
import threading
import time
from dataclasses import fields, is_dataclass
from enum import IntEnum
from pathlib import Path
from types import CodeType


class Stage(IntEnum):
    READINESS = 1
    LIVE = 2
    HISTORY = 3


class Fact(IntEnum):
    MISSING = 0
    INVALID = 1
    PRESENT = 2


class Event(IntEnum):
    OPEN = 0
    BEFORE = 1
    AFTER = 2
    TERMINAL = 3


class Verdict(IntEnum):
    PENDING = 0
    ACCEPTED = 1
    REJECTED = 2
    INCOMPLETE = 3


class Evaluation(IntEnum):
    NOT_REACHED = 0
    ROW = 1
    INITIALIZING = 2
    SNAPSHOT_REFUSED = 3


class Category(IntEnum):
    NONE = 0
    ASSERTION = 1
    INPUT = 2
    IO = 3
    OTHER = 4
    EVIDENCE = 5


class Attempt(IntEnum):
    NOT_REACHED = 0
    CAPTURED = 1
    UNAVAILABLE = 2


class EvidenceFailure(Exception):
    pass


HOSTS = ('192.168.3.180', '192.168.3.181', '192.168.3.183', '192.168.3.184')
MAX_EVENTS = 4096
MAX_EVENT_BYTES = 4096
MAX_BYTES = 8 * 1024 * 1024
FRAME_BYTES = 36
TERMINAL_RESERVE = 2 * MAX_EVENT_BYTES
MAX_TRACEBACK = 64
MAX_SOURCE_BYTES = 128 * 1024
INTEGER_BITS = 128
STARTUP_FIELDS = ('phase', 'started', 'deadline', 'initial_readback', 'waited', 'earned', 'last_credit')
PHASES = ('waiting', 'matched16qualification', 'closed')
EXECUTIONS = ('matched', 'pending')
SCENARIOS = ('protect', 'trial_pending', 'trial', 'hold', 'cold_release', 'release')
NODE_NUMBERS = ('source_ts', 'sampled_at', 'requested_mono', 'cpu_temp', 'gpu_temp', 'board_temp', 'power_w', 'cpu_load', 'gpu_load')
NODE_FLAGS = ('online', 'thermal_throttle', 'ambiguous_sensors')
ROW_NUMBERS = ('ts', 'snapshot_at', 'mono', 'readback_mono', 'execution_deadline', 'destination_requested_at', 'requested_at', 'ambient_c', 'ambient_verified_at', 'gear', 'requested', 'rpm', 'verified_at')
ROW_FLAGS = ('confirmed', 'configured', 'fault', 'ambient_fresh', 'service_handoff', 'telemetry_complete', 'thermal_complete', 'startup')
FAN_NUMBERS = ('level', 'rpm', 'verified_at', 'observed_mono', 'attempts', 'ambient_c', 'ambient_verified_at')
FAN_FLAGS = ('configured', 'fault', 'confirmed', 'protection_interrupted')
INPUT_NUMBERS = ('server_time', 'snapshot_at', 'ts', 'updated_at', 'controller_age_seconds', 'requested_level', 'dropped_records')
TRIAL_NUMBERS = ('from', 'to', 'observation_elapsed', 'deadline', 'elapsed', 'requested_at', 'original_deadline', 'destination_requested_at')
CLOCK_NUMBERS = ('wall', 'mono', 'thermal_ns', 'denominator', 'local_wall', 'local_mono', 'stamp')
ADD_NUMBERS = ('now', 'earned', 'stamp')
ADD_FLAGS = ('matched', 'credit', 'ready')
TIMELINE_NUMBERS = ('pending_since', 'pending_deadline', 'request_after', 'last_decrease')
AST_PINS = {
    (3, 8): ('772bc3b215fa5870d853c79a9880d983f1419e853231180832c483c9e254a486',
             'b818587e198e8d1b5e96326ddec4b4c96d8380ccef31397db449f03006bd54e0'),
    (3, 12): ('340c5dde686ee5a421f856818054a36eaaa99c3eda9887b002a111e89f97b5d5',
              '6b145aa3889dd54d1eb080a14461320b0d600c08be26a4085099ee5ea322c9f8'),
}
if sys.version_info[:2] not in AST_PINS:
    raise EvidenceFailure('unsupported verifier runtime')
SNAPSHOT_AST_SHA256, ADD_AST_SHA256 = AST_PINS[sys.version_info[:2]]
_MISSING = object()


def _get(mapping, key):
    return mapping.get(key, _MISSING) if type(mapping) is dict else _MISSING


def _number(value):
    if value is _MISSING:
        return [Fact.MISSING]
    if type(value) not in (int, float):
        return [Fact.INVALID]
    if type(value) is int:
        valid = -(1 << (INTEGER_BITS - 1)) <= value < (1 << (INTEGER_BITS - 1))
    else:
        valid = math.isfinite(value) and abs(value) < 1e18
    return [Fact.PRESENT, value] if valid else [Fact.INVALID]


def _flag(value):
    if value is _MISSING:
        return [Fact.MISSING]
    return [Fact.PRESENT, value] if type(value) is bool else [Fact.INVALID]


def _enum(value, catalog):
    if value is _MISSING:
        return [Fact.MISSING]
    if type(value) is str and len(value) <= 64 and value in catalog:
        return [Fact.PRESENT, catalog.index(value)]
    return [Fact.INVALID]


def _presence(value):
    return Fact.MISSING if value is _MISSING else Fact.PRESENT if type(value) is dict else Fact.INVALID


def _error_flag(value):
    if value is _MISSING:
        return [Fact.MISSING]
    if value is None or value is False:
        return [Fact.PRESENT, False]
    if type(value) is str:
        return [Fact.PRESENT, len(value) != 0]
    if value is True:
        return [Fact.PRESENT, True]
    return [Fact.INVALID]


def _hosts(value, trend=_MISSING):
    slots = [_MISSING] * len(HOSTS)
    invalid = type(value) is not list or len(value) > len(HOSTS)
    if not invalid:
        for node in value:
            host = _get(node, 'host')
            if type(host) is not str or len(host) > 64 or host not in HOSTS:
                invalid = True
                continue
            index = HOSTS.index(host)
            if slots[index] is _MISSING:
                slots[index] = node
            else:
                invalid = True
                slots[index] = None
    result = []
    for index, node in enumerate(slots):
        power = _get(node, 'power_source')
        power_fact = ([Fact.MISSING] if power is _MISSING else
                      [Fact.PRESENT, bool(power) and power != '不可用'] if type(power) is str and len(power) <= 64 else
                      [Fact.INVALID])
        result.append([_presence(node),
                       [_number(_get(node, key)) for key in NODE_NUMBERS],
                       [_flag(_get(node, key)) for key in NODE_FLAGS],
                       _flag(_get(trend, HOSTS[index])), power_fact])
    return [invalid, result]


def _detail(value):
    if type(value) is not str or len(value) > 192:
        return [[Fact.MISSING] if value is _MISSING else [Fact.INVALID], [Fact.MISSING]]
    waiting = value in ('启动或故障恢复，等待实际16档确认', '启动或故障恢复，等待实际 16 档确认')
    baseline = re.fullmatch(r'启动新基线 earned=([0-9]{1,18}(?:\.[0-9]{1,18})?)/30s，完整匹配确认后才允许下降', value)
    return [[Fact.PRESENT, waiting], _number(float(baseline.group(1))) if baseline else [Fact.MISSING]]


def _row(row):
    return [_presence(row), [_number(_get(row, key)) for key in ROW_NUMBERS],
            [_flag(_get(row, key)) for key in ROW_FLAGS],
            _enum(_get(row, 'execution'), EXECUTIONS),
            _enum(_get(row, 'scenario'), SCENARIOS),
            _enum(_get(row, 'startup_phase'), PHASES), _hosts(_get(row, 'nodes')),
            _detail(_get(row, 'scenario_detail'))]


def _input(value):
    fan = _get(value, 'fan')
    decision = _get(value, 'decision')
    learning = _get(value, 'learning')
    settings = _get(value, 'settings')
    settings_fact = ([Fact.MISSING] if settings is _MISSING else
                     [Fact.PRESENT, len(settings) == 3 and type(settings.get('retention_days')) is int
                      and settings.get('retention_days') == 7 and type(settings.get('max_log_mb')) is int
                      and settings.get('max_log_mb') == 4096 and settings.get('manual_level', _MISSING) is None]
                     if type(settings) is dict else [Fact.INVALID])
    trials = []
    for key in ('trial', 'execution_trial'):
        trial = _get(decision, key)
        trials.append([_presence(trial), [_number(_get(trial, field)) for field in TRIAL_NUMBERS],
                       _enum(_get(trial, 'status'), ('pending', 'observing', 'complete', 'failed'))])
    return [_presence(value), [_number(_get(value, key)) for key in INPUT_NUMBERS],
            [_error_flag(_get(value, key)) for key in ('storage_error', 'settings_error')],
            [_presence(fan), [_number(_get(fan, key)) for key in FAN_NUMBERS],
             [_flag(_get(fan, key)) for key in FAN_FLAGS],
             _enum(_get(fan, 'execution'), EXECUTIONS),
             _enum(_get(fan, 'power'), ('on', 'off')),
             _enum(_get(fan, 'mode'), ('favorite',)), _error_flag(_get(fan, 'error'))],
            [_number(_get(learning, 'mono')), _flag(_get(learning, 'healthy')),
             _number(_get(learning, 'dropped_records')), _error_flag(_get(learning, 'error'))],
            [_number(_get(decision, 'level')), _flag(_get(decision, 'retry_blocked')),
             _enum(_get(decision, 'scenario'), SCENARIOS), trials, _detail(_get(decision, 'scenario_detail'))],
            _hosts(_get(value, 'nodes'), _get(decision, 'trend_complete')), settings_fact]


def _same_code(left, right):
    if (left.co_code, left.co_names, left.co_varnames, left.co_freevars, left.co_cellvars,
        left.co_argcount, left.co_kwonlyargcount, left.co_flags) != (
            right.co_code, right.co_names, right.co_varnames, right.co_freevars, right.co_cellvars,
            right.co_argcount, right.co_kwonlyargcount, right.co_flags):
        return False
    if len(left.co_consts) != len(right.co_consts):
        return False
    return all(_same_code(a, b) if type(a) is CodeType and type(b) is CodeType
               else type(a) is type(b) and a == b for a, b in zip(left.co_consts, right.co_consts))


def _pin(snapshot_code, timeline_code):
    source_path = Path(__file__).with_name('verify-graded-cooling-live.py')
    with source_path.open('rb') as stream:
        source = stream.read(MAX_SOURCE_BYTES + 1)
    if len(source) > MAX_SOURCE_BYTES:
        raise EvidenceFailure('source bound')
    tree = ast.parse(source)
    snapshot = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'snapshot')
    timeline = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Timeline')
    add = next(node for node in timeline.body if isinstance(node, ast.FunctionDef) and node.name == 'add')
    for node, expected, actual in ((snapshot, SNAPSHOT_AST_SHA256, snapshot_code), (add, ADD_AST_SHA256, timeline_code)):
        if hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest() != expected:
            raise EvidenceFailure('source pin')
        module = compile(ast.Module(body=[timeline if node is add else node], type_ignores=[]), '<pinned>', 'exec')
        code = next(constant for constant in module.co_consts if type(constant) is CodeType)
        if node is add:
            code = next(constant for constant in code.co_consts if type(constant) is CodeType and constant.co_name == 'add')
        if type(actual) is not CodeType or not _same_code(code, actual):
            raise EvidenceFailure('code pin')


def _startup(value, startup_type):
    if value is None:
        return [Fact.MISSING]
    if (type(value) is not startup_type or not is_dataclass(startup_type)
            or tuple(field.name for field in fields(startup_type)) != STARTUP_FIELDS):
        return [Fact.INVALID]
    phase = value.phase
    return [Fact.PRESENT, _enum(phase.value if type(phase) is startup_type.__annotations__['phase'] else _MISSING, PHASES),
            [_number(getattr(value, name)) for name in ('started', 'deadline', 'initial_readback', 'earned', 'last_credit')],
            _flag(value.waited)]


def _state(timeline, code):
    add = type(timeline).add
    if add.__code__ is not code:
        raise EvidenceFailure('timeline identity')
    startup_type = add.__globals__['Startup']
    previous = timeline.previous
    previous_facts = [[_number(_get(previous, key)) for key in ('mono', 'snapshot_at', 'readback_mono', 'gear', 'requested')],
                      [_flag(_get(previous, key)) for key in ('telemetry_complete', 'thermal_complete')],
                      _enum(_get(previous, 'execution'), EXECUTIONS)]
    sources = []
    for host in HOSTS:
        value = _get(timeline.sources, host)
        sources.append([_number(value[0]), _number(value[1])] if type(value) is tuple and len(value) == 2
                       else [[Fact.MISSING], [Fact.MISSING]])
    return [_startup(timeline.startup, startup_type),
            [_number(getattr(timeline, name)) for name in TIMELINE_NUMBERS], previous_facts, sources]


def _category(error):
    if isinstance(error, EvidenceFailure):
        return Category.EVIDENCE
    if isinstance(error, AssertionError):
        return Category.ASSERTION
    if isinstance(error, (KeyError, TypeError, ValueError)):
        return Category.INPUT
    return Category.IO if isinstance(error, OSError) else Category.OTHER


def _safe_directory(parent):
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise EvidenceFailure('private POSIX filesystem required')
    path = Path(parent)
    if not path.is_absolute() or '..' in path.parts:
        raise EvidenceFailure('private absolute parent required')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open('/', flags)
    try:
        for index, component in enumerate(path.parts[1:]):
            if index == len(path.parts) - 2:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise EvidenceFailure('unsafe private parent')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class EvidenceRun:
    @classmethod
    def create(cls, private_parent, original_deadline, snapshot_code, timeline_code):
        parent_fd = run_fd = journal_fd = None
        try:
            _pin(snapshot_code, timeline_code)
            if _number(original_deadline)[0] != Fact.PRESENT:
                raise EvidenceFailure('invalid deadline')
            parent_fd = _safe_directory(private_parent)
            name = secrets.token_hex(16)
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            run_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            info = os.fstat(run_fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise EvidenceFailure('unsafe run')
            journal_fd = os.open('journal', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=run_fd)
            info = os.fstat(journal_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise EvidenceFailure('unsafe journal')
            os.fsync(run_fd)
            os.fsync(parent_fd)
            run = cls(journal_fd, Path(private_parent) / name / 'journal', original_deadline, snapshot_code, timeline_code)
            journal_fd = None
            return run
        except EvidenceFailure:
            raise
        except (OSError, ValueError, SyntaxError, StopIteration) as error:
            raise EvidenceFailure('evidence allocation') from error
        finally:
            for descriptor in (journal_fd, run_fd, parent_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def __init__(self, descriptor, path, deadline, snapshot_code, timeline_code):
        self.path = path
        self._fd = descriptor
        self._deadline = deadline
        self._snapshot_code = snapshot_code
        self._timeline_code = timeline_code
        self._owner = (os.getpid(), threading.get_ident())
        self._count = self._bytes = self._observations = 0
        self._stage_counts = [0] * len(Stage)
        self._stage = 0
        self._active = False
        self._failed = False
        self._closed = False
        self._terminal = None
        self._hook = None
        self._emit(Event.OPEN, [deadline, os.getpid(), True,
                               list(bytes.fromhex(SNAPSHOT_AST_SHA256)), list(bytes.fromhex(ADD_AST_SHA256))])

    def _owned(self):
        if self._closed or self._owner != (os.getpid(), threading.get_ident()):
            raise EvidenceFailure('evidence owner')

    def _emit(self, kind, facts):
        self._owned()
        try:
            payload = json.dumps([int(kind), self._count, facts], separators=(',', ':'), allow_nan=False).encode('ascii')
            size = len(payload) + FRAME_BYTES
            if size > MAX_EVENT_BYTES or self._count >= MAX_EVENTS or self._bytes + size > MAX_BYTES:
                raise EvidenceFailure('evidence capacity')
            frame = struct.pack('>I', len(payload)) + hashlib.sha256(payload).digest() + payload
            view = memoryview(frame)
            while view:
                written = os.write(self._fd, view)
                if written <= 0:
                    raise EvidenceFailure('evidence write')
                view = view[written:]
            os.fsync(self._fd)
            self._count += 1
            self._bytes += size
        except (OSError, ValueError, OverflowError) as error:
            self._failed = True
            raise EvidenceFailure('evidence persistence') from error
        except EvidenceFailure:
            self._failed = True
            raise

    def observe(self, stage, value, timeline):
        self._owned()
        if type(stage) is not Stage or self._active or self._failed or self._terminal is not None:
            raise EvidenceFailure('observation state')
        if (self._count + 2 > MAX_EVENTS - 2
                or self._bytes + 2 * MAX_EVENT_BYTES > MAX_BYTES - TERMINAL_RESERVE):
            self._failed = True
            raise EvidenceFailure('evidence capacity')
        observation = _Observation(self, stage, value, timeline)
        self._active = observation
        return observation

    def accept(self):
        self._owned()
        if self._active or self._failed or self._terminal is not None or time.time() >= self._deadline:
            raise EvidenceFailure('acceptance unavailable')
        try:
            self._emit(Event.TERMINAL, self._terminal_facts(Verdict.ACCEPTED, Category.NONE))
            if time.time() >= self._deadline:
                raise EvidenceFailure('acceptance deadline')
            self._terminal = Verdict.ACCEPTED
        except EvidenceFailure:
            self._failed = True
            self._terminal = None
            try:
                self._emit(Event.TERMINAL, self._terminal_facts(Verdict.REJECTED, Category.EVIDENCE))
                self._terminal = Verdict.REJECTED
            except EvidenceFailure:
                pass
            raise

    def _terminal_facts(self, verdict, category):
        return [verdict, category, self._observations, time.time(), self._stage, list(self._stage_counts)]

    def finish(self, error=None):
        if self._closed:
            return
        try:
            if self._terminal is None or error is not None:
                verdict = Verdict.REJECTED if error is not None or self._failed else Verdict.INCOMPLETE
                category = _category(error) if error is not None else Category.EVIDENCE if self._failed else Category.NONE
                self._emit(Event.TERMINAL, self._terminal_facts(verdict, category))
                self._terminal = verdict
        except EvidenceFailure as failure:
            if error is not None:
                raise failure from error
            raise
        finally:
            try:
                os.close(self._fd)
            except OSError as failure:
                self._failed = True
                try:
                    self._emit(Event.TERMINAL, self._terminal_facts(Verdict.REJECTED, Category.EVIDENCE))
                    self._terminal = Verdict.REJECTED
                except EvidenceFailure:
                    pass
                if error is not None:
                    raise EvidenceFailure('evidence close') from error
                raise EvidenceFailure('evidence close') from failure
            finally:
                self._closed = True
                if self._hook is not None and sys.excepthook is self._hook:
                    sys.excepthook = self._previous_hook
        if self._terminal == Verdict.ACCEPTED and time.time() >= self._deadline:
            self._failed = True
            raise EvidenceFailure('completion deadline')

    def install_cleanup(self):
        self._owned()
        if self._hook is not None:
            raise EvidenceFailure('duplicate cleanup')
        self._previous_hook = sys.excepthook

        def hook(kind, error, traceback):
            try:
                self.finish(error)
            except EvidenceFailure as failure:
                self._previous_hook(type(failure), failure, failure.__traceback__)
            else:
                self._previous_hook(kind, error, traceback)

        self._hook = hook
        sys.excepthook = hook
        atexit.register(self.finish)

    def __enter__(self):
        self._owned()
        return self

    def __exit__(self, kind, error, traceback):
        self.finish(error)
        return False


class _Observation:
    def __init__(self, run, stage, value, timeline):
        self.run, self.stage, self.timeline = run, stage, timeline
        self.input = _input(value)
        self.before = _state(timeline, run._timeline_code)
        self.row = None
        self._row_identity = None
        self.evaluation = Evaluation.NOT_REACHED
        self._entered = False

    def __enter__(self):
        self.run._owned()
        if self.run._active is not self or self._entered:
            raise EvidenceFailure('observation owner')
        try:
            self.run._emit(Event.BEFORE, [self.stage, self.run._observations, self.input, self.before])
        except EvidenceFailure:
            self.run._active = False
            raise
        self._entered = True
        self.run._stage = self.stage
        return self

    def derived(self, row_or_none):
        if self.run._active is not self or not self._entered or self.evaluation != Evaluation.NOT_REACHED:
            raise EvidenceFailure('derived state')
        self.row = _row(row_or_none)
        self._row_identity = id(row_or_none) if type(row_or_none) is dict else None
        self.evaluation = Evaluation.INITIALIZING if row_or_none is None else Evaluation.ROW

    def __exit__(self, kind, error, traceback):
        attempted = [Attempt.NOT_REACHED]
        clocks = [[Fact.MISSING] for _ in CLOCK_NUMBERS]
        local_facts = [[_number(_MISSING) for _ in ADD_NUMBERS], [_flag(_MISSING) for _ in ADD_FLAGS]]
        current = traceback
        for _ in range(MAX_TRACEBACK):
            if current is None:
                break
            frame = current.tb_frame
            if frame.f_code is self.run._snapshot_code:
                clocks = [_number(frame.f_locals.get(name, _MISSING)) for name in CLOCK_NUMBERS]
                if self.evaluation == Evaluation.NOT_REACHED:
                    self.evaluation = Evaluation.SNAPSHOT_REFUSED
            if frame.f_code is self.run._timeline_code:
                value = frame.f_locals.get('startup', _MISSING)
                if value is not _MISSING:
                    facts = _startup(value, type(self.timeline).add.__globals__['Startup'])
                    attempted = [Attempt.CAPTURED, facts] if facts[0] != Fact.INVALID else [Attempt.UNAVAILABLE]
                local_facts = [[_number(frame.f_locals.get(name, _MISSING)) for name in ADD_NUMBERS],
                               [_flag(frame.f_locals.get(name, _MISSING)) for name in ADD_FLAGS]]
            current = current.tb_next
        else:
            attempted = [Attempt.UNAVAILABLE]
        committed = _state(self.timeline, self.run._timeline_code)
        if (attempted == [Attempt.NOT_REACHED] and self._row_identity is not None
                and id(self.timeline.previous) == self._row_identity):
            attempted = [Attempt.CAPTURED, committed[0]]
        if error is not None and not (self.stage == Stage.READINESS and isinstance(error, OSError)):
            self.run._failed = True
        try:
            self.run._emit(Event.AFTER, [self.stage, self.run._observations, self.evaluation,
                                       Verdict.REJECTED if error is not None else Verdict.ACCEPTED,
                                       _category(error) if error is not None else Category.NONE,
                                       self.row, attempted, committed, clocks, local_facts])
            self.run._observations += 1
            self.run._stage_counts[self.stage - 1] += 1
        except EvidenceFailure as failure:
            if error is not None:
                raise failure from error
            raise
        finally:
            self.run._active = False
        return False


def read_prefix(path):
    """Stream complete checksummed events. Stop at a torn/invalid prefix; never repair.

    A terminal acceptance must also be paired with successful verifier completion.
    It is not an independently authenticated durability or physical receipt.
    """
    if not hasattr(os, 'O_NOFOLLOW'):
        raise EvidenceFailure('safe reader unavailable')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise EvidenceFailure('reader bound')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            yield from _read_frames(stream)
    finally:
        os.close(descriptor)


def _read_frames(stream):
    consumed = 0
    for sequence in range(MAX_EVENTS):
        if consumed + FRAME_BYTES > MAX_BYTES:
            return
        header = stream.read(FRAME_BYTES)
        if len(header) != FRAME_BYTES:
            return
        consumed += FRAME_BYTES
        length = struct.unpack('>I', header[:4])[0]
        if length > MAX_EVENT_BYTES - FRAME_BYTES or consumed + length > MAX_BYTES:
            return
        payload = stream.read(length)
        consumed += length
        if len(payload) != length or hashlib.sha256(payload).digest() != header[4:]:
            return
        try:
            event = json.loads(payload)
            if (type(event) is not list or len(event) != 3 or type(event[0]) is not int
                    or event[0] not in tuple(Event) or type(event[1]) is not int or event[1] != sequence):
                return
            stack = [(event, 0)]
            visited = 0
            while stack:
                value, depth = stack.pop()
                visited += 1
                if visited > 2048 or depth > 16:
                    return
                if type(value) is list:
                    if len(value) > 32:
                        return
                    stack.extend((child, depth + 1) for child in value)
                elif value is not None and type(value) is not bool and _number(value)[0] != Fact.PRESENT:
                    return
        except (ValueError, RecursionError):
            return
        yield event
