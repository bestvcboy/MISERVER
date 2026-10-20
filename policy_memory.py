import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path

from thermal_policy import MAX_BYTES


MAX_MEMORY_BYTES = MAX_BYTES + 4096
MARKER = "thermal-memory-v1"


def canonical_memory(payload):
    if not isinstance(payload, dict):
        raise ValueError("Thermal memory must be an object")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False)
    if len(encoded.encode("ascii")) > MAX_MEMORY_BYTES - 4096:
        raise ValueError("Thermal memory exceeds its reserved limit")
    return encoded


def memory_digest(payload):
    return hashlib.sha256(canonical_memory(payload).encode("ascii")).hexdigest()


def unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate thermal memory field")
        value[key] = item
    return value


class ThermalMemoryStore:
    def __init__(self, root, identity, write, sync):
        self.root = Path(root)
        self.path = self.root / "thermal-memory.json"
        self.marker = self.root / "thermal-memory.initialized"
        self.identity = identity
        self.write, self.sync = write, sync
        self.confirmed_digest = None

    @staticmethod
    def _read(path, limit):
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > limit
                or os.name == "posix" and before.st_mode & 0o022):
            raise ValueError("Unsafe thermal memory file")
        if hasattr(os, "getuid") and before.st_uid != os.getuid():
            raise ValueError("Thermal memory owner mismatch")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(fd)
            raw = os.read(fd, limit + 1)
            after = os.fstat(fd)
            linked = path.lstat()
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                                   s.st_ctime_ns if os.name == "posix" else None)
            if (signature(before) != signature(opened) or signature(opened) != signature(after)
                    or signature(after) != signature(linked) or len(raw) != before.st_size):
                raise ValueError("Thermal memory changed while reading")
            return raw
        finally:
            os.close(fd)

    def load(self):
        self.confirmed_digest = None
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("Unsafe thermal memory directory")
        marker = self._read(self.marker, 64)
        raw = self._read(self.path, MAX_MEMORY_BYTES)
        if marker is not None and marker != MARKER.encode("ascii"):
            raise ValueError("Invalid thermal memory marker")
        if raw is None:
            if marker is not None:
                raise ValueError("Initialized thermal memory is missing")
            return None
        envelope = json.loads(raw.decode("ascii"), object_pairs_hook=unique_fields)
        if (not isinstance(envelope, dict)
                or set(envelope) != {"schema", "identity", "payload", "sha256"}
                or type(envelope["schema"]) is not int or envelope["schema"] != 1
                or envelope["identity"] != self.identity
                or envelope["sha256"] != memory_digest(envelope["payload"])):
            raise ValueError("Invalid thermal memory envelope")
        return envelope["payload"]

    def save(self, payload):
        encoded = canonical_memory(payload)
        digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        if digest == self.confirmed_digest:
            return digest
        envelope = json.dumps(dict(schema=1, identity=self.identity,
            payload=json.loads(encoded), sha256=digest), separators=(",", ":"),
            ensure_ascii=True, allow_nan=False)
        if len(envelope) > MAX_MEMORY_BYTES:
            raise ValueError("Thermal memory envelope exceeds its limit")
        self.confirmed_digest = None
        self.write(self.path, envelope)
        self.sync(self.root)
        marker = self._read(self.marker, 64)
        if marker is None:
            self.write(self.marker, MARKER)
            self.sync(self.root)
        elif marker != MARKER.encode("ascii"):
            raise ValueError("Invalid thermal memory marker")
        self.confirmed_digest = digest
        return digest


class ThermalMemoryGate:
    def __init__(self, store):
        self.store = store
        self.desired_digest = None
        self.acknowledged_digest = None
        self.task = None
        self.error = False
        self.load_failed = False

    @property
    def ready(self):
        return (not self.load_failed and not self.error and self.task is None and self.desired_digest is not None
                and self.desired_digest == self.acknowledged_digest)

    def invalidate(self):
        self.desired_digest = None

    async def initialize(self, controller, now):
        try:
            payload = await asyncio.to_thread(self.store.load)
            if payload is not None:
                controller.restore_thermal_memory(payload, now)
            self.desired_digest = memory_digest(controller.export_thermal_memory())
            self.acknowledged_digest = self.store.confirmed_digest
        except Exception:
            self.load_failed = True
            self.error = True

    def publish(self, controller):
        if self.load_failed:
            return
        try:
            encoded = canonical_memory(controller.export_thermal_memory())
            self.desired_digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        except Exception:
            self.invalidate()
            self.error = True
            return
        if self.ready or self.task is not None:
            return
        self.acknowledged_digest = None

        async def commit():
            try:
                self.acknowledged_digest = await asyncio.to_thread(self.store.save, json.loads(encoded))
                self.error = False
            except Exception:
                self.acknowledged_digest = None
                self.error = True
            finally:
                self.task = None

        self.task = asyncio.create_task(commit())

    async def flush(self, controller):
        if self.task is not None:
            await self.task
        self.publish(controller)
        if self.task is not None:
            await self.task

    def status(self):
        return dict(state="invalid" if self.load_failed else "failed" if self.error else
                    "ready" if self.ready else "pending", descent_allowed=self.ready,
                    next_condition="热记忆完整且当前快照已可靠保存" if not self.ready else None,
                    reserved_bytes=MAX_MEMORY_BYTES + 64)
