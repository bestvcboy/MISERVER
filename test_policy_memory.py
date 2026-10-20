import asyncio
import json
import os
import tempfile
import unittest
from threading import Event
from pathlib import Path
from unittest.mock import Mock

from policy_memory import MAX_MEMORY_BYTES, ThermalMemoryGate, ThermalMemoryStore, memory_digest
from service import atomic_write


class ThermalMemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sync = Mock()
        self.identity = {"did": 70632677, "hosts": ["h1", "h2"]}
        self.store = ThermalMemoryStore(self.root, self.identity, atomic_write, self.sync)
        self.payload = {"version": 1, "failures": [{"safe": 8, "failed": 7}], "pending": None}

    def test_initialization_roundtrip_and_unchanged_memory_does_not_write(self):
        self.assertIsNone(self.store.load())
        digest = self.store.save(self.payload)
        self.assertEqual(digest, memory_digest(self.payload))
        self.assertEqual(self.sync.call_count, 2)
        self.assertEqual(self.store.load(), self.payload)
        self.assertIsNone(self.store.confirmed_digest)
        self.store.save(self.payload)
        self.assertEqual(self.sync.call_count, 3)
        self.store.save(self.payload)
        self.assertEqual(self.sync.call_count, 3)
        self.assertEqual(self.store.confirmed_digest, digest)

    def test_previous_initialization_cannot_silently_forget_missing_memory(self):
        self.store.save(self.payload)
        self.store.path.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.store.load()
        self.assertIsNone(self.store.confirmed_digest)

    def test_corruption_duplicate_fields_or_identity_mismatch_are_preserved(self):
        self.store.save(self.payload)
        valid = json.loads(self.store.path.read_text())
        corrupted = dict(valid, sha256="0" * 64)
        foreign = dict(valid, identity={"did": 1})
        for raw in ("{", json.dumps(corrupted), json.dumps(foreign),
                    '{"schema":1,"schema":1}', json.dumps(dict(valid, schema=True))):
            with self.subTest(raw=raw):
                atomic_write(self.store.path, raw)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    self.store.load()
                self.assertEqual(self.store.path.read_text(), raw)
                self.assertIsNone(self.store.confirmed_digest)

    def test_failed_directory_sync_never_acknowledges_or_restores_old_memory(self):
        self.store.save(self.payload)
        newer = dict(self.payload, pending={"from": 8, "to": 7})
        self.sync.side_effect = OSError("directory sync failed")
        with self.assertRaises(OSError):
            self.store.save(newer)
        self.assertIsNone(self.store.confirmed_digest)
        self.assertEqual(json.loads(self.store.path.read_text())["payload"], newer)
        self.sync.side_effect = None
        self.assertEqual(self.store.save(newer), memory_digest(newer))

    def test_interrupted_first_initialization_requires_a_new_durable_commit(self):
        self.sync.side_effect = OSError("first sync interrupted")
        with self.assertRaises(OSError):
            self.store.save(self.payload)
        reopened = ThermalMemoryStore(self.root, self.identity, atomic_write, Mock())
        self.assertEqual(reopened.load(), self.payload)
        self.assertIsNone(reopened.confirmed_digest)
        reopened.save(self.payload)
        self.assertEqual(reopened.confirmed_digest, memory_digest(self.payload))
        self.assertTrue(reopened.marker.exists())

    def test_restart_cannot_acknowledge_replacement_with_failed_directory_sync(self):
        self.store.save(self.payload)
        newer = dict(self.payload, pending={"from": 8, "to": 7})
        self.sync.side_effect = OSError("replacement directory sync failed")
        with self.assertRaises(OSError):
            self.store.save(newer)
        reopened = ThermalMemoryStore(self.root, self.identity, atomic_write, Mock())
        self.assertEqual(reopened.load(), newer)
        self.assertIsNone(reopened.confirmed_digest)
        reopened.sync.side_effect = OSError("still unavailable")
        with self.assertRaises(OSError):
            reopened.save(newer)
        self.assertIsNone(reopened.confirmed_digest)
        reopened.sync.side_effect = None
        self.assertEqual(reopened.save(newer), memory_digest(newer))

    def test_marker_failure_and_nonfinite_or_oversized_payloads_do_not_acknowledge(self):
        self.store.save(self.payload)
        atomic_write(self.store.marker, "wrong marker")
        with self.assertRaises(ValueError):
            self.store.load()
        for payload in ({"x": float("nan")}, {"x": "x" * MAX_MEMORY_BYTES}, []):
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(ValueError):
                    self.store.save(payload)
        self.assertIsNone(self.store.confirmed_digest)

    def test_nonregular_file_is_not_followed(self):
        self.store.path.mkdir()
        with self.assertRaises(ValueError):
            self.store.load()

    @unittest.skipUnless(os.name == "posix", "Native POSIX thermal memory permissions and directory sync required")
    def test_native_store_permissions_sync_and_link_rejection(self):
        from service import fsync_directory
        store = ThermalMemoryStore(self.root, self.identity, atomic_write, fsync_directory)
        store.save(self.payload)
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.load(), self.payload)
        self.assertIsNone(store.confirmed_digest)
        store.save(self.payload)
        os.chmod(store.path, 0o666)
        with self.assertRaises(ValueError):
            store.load()
        os.chmod(store.path, 0o600)
        linked = self.root / "hardlink"
        os.link(store.path, linked)
        with self.assertRaises(ValueError):
            store.load()
        linked.unlink()
        target = self.root / "original-memory"
        store.path.rename(target)
        store.path.symlink_to(target)
        with self.assertRaises(ValueError):
            store.load()


class ThermalMemoryGateTests(unittest.IsolatedAsyncioTestCase):
    def ceiling_payload(self):
        from core import Controller
        from test_core import HOSTS
        from test_scenarios import feedback, fleet
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now, actual, cpu in ((0, 16, 40), (3, 15, 80), (6, 16, 40)):
            controller.decide(fleet(now, cpu=cpu, gpu=38, board=46, load=60, power=100),
                now, minimum_level=0, feedback=feedback(now, actual))
        payload = controller.export_thermal_memory()
        self.assertTrue(payload.get('ceiling_dangers'))
        return payload

    def controller(self, payload):
        controller = Mock()
        controller.export_thermal_memory.return_value = payload
        return controller

    async def test_changed_danger_provenance_cannot_use_older_save_acknowledgement(self):
        from copy import deepcopy
        from thermal_policy import ThermalMemory
        old = self.ceiling_payload()
        new = deepcopy(old)
        new['ceiling_dangers'][0]['baseline'] = [55, 55, 95]
        new = ThermalMemory.restore(new).export()
        self.assertEqual(new['failures'], old['failures'])
        self.assertNotEqual(memory_digest(old), memory_digest(new))
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        def delayed(payload):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return memory_digest(payload)
        store = Mock(confirmed_digest=None)
        store.load.return_value = None
        store.save.side_effect = delayed
        gate = ThermalMemoryGate(store)
        controller = self.controller(old)
        await gate.initialize(controller, 9)
        gate.publish(controller)
        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
        controller.export_thermal_memory.return_value = new
        gate.invalidate()
        gate.publish(controller)
        self.assertFalse(gate.ready)
        release.set()
        await gate.task
        self.assertFalse(gate.ready)
        await gate.flush(controller)
        self.assertTrue(gate.ready)
        self.assertEqual(store.save.call_args.args[0], new)

    async def test_failed_danger_save_stays_closed_and_restart_keeps_recovery_restriction(self):
        from core import Controller
        from test_core import HOSTS
        from test_scenarios import feedback, fleet
        payload = self.ceiling_payload()
        with tempfile.TemporaryDirectory() as directory:
            sync = Mock(side_effect=OSError('directory sync unavailable'))
            store = ThermalMemoryStore(Path(directory), {'device': 1}, atomic_write, sync)
            gate = ThermalMemoryGate(store)
            controller = self.controller(payload)
            await gate.initialize(controller, 9)
            gate.publish(controller)
            await gate.task
            self.assertFalse(gate.ready)
            self.assertEqual(gate.status()['state'], 'failed')
            sync.side_effect = None
            await gate.flush(controller)
            self.assertTrue(gate.ready)
            reopened = ThermalMemoryStore(Path(directory), {'device': 1}, atomic_write, Mock())
            restarted = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            next_gate = ThermalMemoryGate(reopened)
            await next_gate.initialize(restarted, 9)
            self.assertFalse(next_gate.ready)
            await next_gate.flush(restarted)
            self.assertTrue(next_gate.ready)
            self.assertEqual(restarted.export_thermal_memory()['ceiling_dangers'], payload['ceiling_dangers'])
            for now in range(9, 190, 3):
                decision = restarted.decide(fleet(now, cpu=40, gpu=38, board=46, load=60, power=100),
                    now, minimum_level=0, feedback=feedback(now, 16))
                self.assertEqual(decision['next_command'], 16)
                self.assertEqual(decision['operating']['failure_credit'], 0)

    async def test_initial_memory_needs_durable_commit_and_queries_do_not_write(self):
        store = Mock(confirmed_digest=None)
        store.load.return_value = None
        store.save.side_effect = memory_digest
        gate = ThermalMemoryGate(store)
        controller = self.controller({"version": 1})
        await gate.initialize(controller, 3)
        self.assertFalse(gate.ready)
        self.assertEqual(gate.status()["state"], "pending")
        store.save.assert_not_called()
        gate.publish(controller)
        await gate.task
        self.assertTrue(gate.ready)
        gate.invalidate()
        self.assertFalse(gate.ready)
        gate.publish(controller)
        self.assertTrue(gate.ready)
        self.assertIsNone(gate.task)
        store.save.assert_called_once()

    async def test_older_save_cannot_unlock_changed_descent_or_failure_clear(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        old = {"failure": {"safe": 10}}
        new = {"failure": None, "pending": {"from": 10, "to": 9}}
        def delayed(payload):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return memory_digest(payload)
        store = Mock(confirmed_digest=None)
        store.load.return_value = None
        store.save.side_effect = delayed
        gate = ThermalMemoryGate(store)
        controller = self.controller(old)
        await gate.initialize(controller, 3)
        gate.publish(controller)
        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
        controller.export_thermal_memory.return_value = new
        gate.invalidate()
        gate.publish(controller)
        self.assertFalse(gate.ready)
        release.set()
        await gate.task
        self.assertFalse(gate.ready)
        self.assertEqual(store.save.call_count, 1)
        gate.publish(controller)
        await gate.task
        self.assertTrue(gate.ready)
        self.assertEqual(store.save.call_args.args[0], new)

    async def test_corrupt_startup_is_latched_and_never_overwritten(self):
        for failure in ("load", "restore"):
            with self.subTest(failure=failure):
                store = Mock(confirmed_digest="old")
                store.load.return_value = {"version": 1}
                controller = self.controller({})
                getattr(store if failure == "load" else controller,
                        "load" if failure == "load" else "restore_thermal_memory").side_effect = ValueError()
                gate = ThermalMemoryGate(store)
                await gate.initialize(controller, 3)
                for _ in range(3):
                    gate.publish(controller)
                    self.assertFalse(gate.ready)
                self.assertEqual(gate.status()["state"], "invalid")
                store.save.assert_not_called()

    async def test_a_b_a_cannot_reopen_gate_during_a_replacement_write(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        a, b = {"revision": 1}, {"revision": 2}
        def delayed(payload):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return memory_digest(payload)
        store = Mock(confirmed_digest=memory_digest(a))
        store.load.return_value = a
        store.save.side_effect = delayed
        gate = ThermalMemoryGate(store)
        controller = self.controller(a)
        await gate.initialize(controller, 3)
        self.assertTrue(gate.ready)
        controller.export_thermal_memory.return_value = b
        gate.invalidate()
        gate.publish(controller)
        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
        controller.export_thermal_memory.return_value = a
        gate.invalidate()
        gate.publish(controller)
        self.assertFalse(gate.ready)
        release.set()
        await gate.task
        self.assertFalse(gate.ready)
        await gate.flush(controller)
        self.assertTrue(gate.ready)
        self.assertEqual(store.save.call_args.args[0], a)

    async def test_final_flush_commits_latest_failure_after_prior_writer_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            entered, release = Event(), Event()
            self.addCleanup(release.set)
            a, b = {"failures": []}, {"failures": [{"safe": 8, "failed": 7}]}
            def delayed_write(path, encoded):
                if path.name == "thermal-memory.json" and not release.is_set():
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError()
                atomic_write(path, encoded)
            store = ThermalMemoryStore(Path(directory), {"device": 1}, delayed_write, Mock())
            gate = ThermalMemoryGate(store)
            controller = self.controller(a)
            await gate.initialize(controller, 3)
            gate.publish(controller)
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            controller.export_thermal_memory.return_value = b
            gate.invalidate()
            gate.publish(controller)
            flush = asyncio.create_task(gate.flush(controller))
            release.set()
            await flush
            self.assertTrue(gate.ready)
            reopened = ThermalMemoryStore(Path(directory), {"device": 1}, atomic_write, Mock())
            self.assertEqual(reopened.load(), b)

    async def test_write_failure_denies_descent_then_forward_retry_recovers(self):
        store = Mock(confirmed_digest=None)
        store.load.return_value = None
        store.save.side_effect = OSError()
        gate = ThermalMemoryGate(store)
        controller = self.controller({"pending": {"from": 5, "to": 4}})
        await gate.initialize(controller, 3)
        gate.publish(controller)
        await gate.task
        self.assertFalse(gate.ready)
        self.assertEqual(gate.status()["state"], "failed")
        store.save.side_effect = memory_digest
        gate.publish(controller)
        await gate.task
        self.assertTrue(gate.ready)

    async def test_nonfinite_export_revokes_previously_durable_permission(self):
        store = Mock(confirmed_digest=memory_digest({}))
        store.load.return_value = {}
        gate = ThermalMemoryGate(store)
        controller = self.controller({})
        await gate.initialize(controller, 3)
        self.assertTrue(gate.ready)
        controller.export_thermal_memory.return_value = {"temperature": float("nan")}
        gate.publish(controller)
        self.assertFalse(gate.ready)
        store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
