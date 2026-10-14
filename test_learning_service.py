import asyncio
import importlib.util
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from core import Journal
from learning import BUCKET, SLOT, Learning
from test_core import HOSTS
from test_learning import SETTINGS, frame, populated_learning


def closure(function):
    return dict(zip(function.__code__.co_freevars, (cell.cell_contents for cell in function.__closure__)))


@unittest.skipUnless(importlib.util.find_spec("miio"), "miio missing; run isolated import mock or ARM venv")
class LearningStorageTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.end = int(time.time() // SLOT) * SLOT
        cls.complete = populated_learning(14, cls.end)

    async def asyncSetUp(self):
        import service
        self.service = service
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = self.end + 3
        self.sync = patch("service.fsync_directory")
        self.clock = patch("service.time.time", side_effect=lambda: self.now)
        self.sync.start()
        self.clock.start()
        async def local_thread(function, *args):
            return await asyncio.get_running_loop().run_in_executor(None, function, *args)
        self.thread = patch("service.asyncio.to_thread", local_thread, create=True)
        self.thread.start()
        self.settings = deepcopy(SETTINGS)
        (self.root / "settings.json").write_text(json.dumps(self.settings))
        directory = self.root / "learning"
        directory.mkdir()
        checkpoint = deepcopy(self.complete).checkpoint(self.end, self.settings, self.end - SLOT)
        (directory / "checkpoint.json").write_text(checkpoint, encoding="utf-8")
        Journal(self.root / "logs").append({"ts": self.end - SLOT, "nodes": []})
        with patch("service.MotorSchedule.load", return_value=service.MotorSchedule()):
            self.app = await service.create_app(self.root)
        self.state = self.app[service.STATE]
        startup = self.app.on_startup[-1]
        self.storage = closure(startup)["storage"]
        self.learner = closure(self.storage)["learning"]
        status = next(route.handler for route in self.app.router.routes() if route.resource.canonical == "/api/status")
        self.active_power = closure(status)["active_power"]
        self.worker = asyncio.create_task(self.storage())

    async def asyncTearDown(self):
        self.state["stop"] = True
        if not self.worker.done():
            self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        await self.state["qr"].close()
        self.state["disk"].shutdown(wait=True)
        self.state["history_disk"].shutdown(wait=True)
        self.clock.stop()
        self.sync.stop()
        self.thread.stop()
        self.temp.cleanup()

    async def commit(self, stamp, gear=0, power=35, load=0, learning_eligible=True):
        self.now = stamp
        row = frame(stamp, gear=gear, power=power)
        row["fan"].update(observed_mono=time.monotonic(), execution="matched", fault=False)
        row["decision"].update(scenario="steady", learning_eligible=learning_eligible)
        row["learning"]["revision"] = self.state["settings_revision"]
        row["learning"]["elapsed"] = stamp - self.learner.last_ts if self.learner.last_ts is not None else 3
        for node in row["nodes"]:
            node["cpu_load"] = load
        self.state.update(nodes=row["nodes"], updated_at=stamp, fan=row["fan"], last_poll=stamp,
                          last_fan=time.monotonic(), learning_wall=stamp,
                          decision=dict(row["decision"], level=gear, execution_trial=None))
        self.state["queue"].put_nowait(row)
        await asyncio.wait_for(self.state["queue"].join(), 2)
        return row

    async def test_durable_restart_policy_triggers_reserve_ten_and_survives_load_gear_pending(self):
        self.assertEqual(json.loads((self.root / 'learning' / 'checkpoint.json').read_text(encoding='utf-8'))['controller'], 14)
        row = await self.commit(self.now)
        policy = deepcopy(self.state["learning"]["published"])
        self.assertEqual(policy[HOSTS[0]]["threshold_w"], 75)
        self.assertEqual(self.state["learning"]["eligibility"]["phase"], "stabilizing")
        self.now += 3
        hot = frame(self.now, power=76)
        active = self.active_power(hot["nodes"], self.now, self.now, advancing=True)
        self.assertEqual(active, policy)
        actual = 16
        for stamp in range(self.now - 900, self.now, 3):
            qualified = frame(stamp, gear=actual)
            decision = self.state["controller"].decide(qualified['nodes'], stamp, minimum_level=0,
                feedback=qualified['fan'] | {'execution': 'matched', 'fault': False, 'observed_mono': stamp})
            actual = decision['level']
        self.assertEqual(actual, 0)
        decision = self.state["controller"].decide(hot["nodes"], self.now, minimum_level=0,
                                                 power_thresholds=active, feedback=hot['fan'] | {'execution': 'matched', 'fault': False, 'observed_mono': self.now})
        self.assertEqual(decision["level"], 10)
        self.assertEqual((decision['execution_trial']['from'], decision['execution_trial']['to']), (0, 10))
        self.assertIn("已发布整机功耗", " ".join(decision["reasons"]))
        row = await self.commit(self.now + 3, gear=16, power=76, load=40)
        self.assertEqual(self.state["learning"]["published"], policy)
        self.assertEqual(self.active_power(row["nodes"], self.now, self.now), policy)
        self.state["fan"].update(confirmed=False, execution="pending")
        self.assertEqual(self.active_power(row["nodes"], self.now, self.now), {})
        self.assertEqual(self.state["learning"]["published"], policy)
        self.state["fan"].update(confirmed=True, execution="matched")
        self.state["settings_task"] = object()
        self.assertEqual(self.active_power(row["nodes"], self.now, self.now), {})
        self.state["settings_task"] = None
        row["nodes"][0]["power_source"] = "estimate:gpu"
        self.assertEqual(self.active_power(row["nodes"], self.now, self.now), {})

    async def test_failed_raw_append_never_admitted_or_bridged(self):
        await self.commit(self.now)
        before = deepcopy(self.learner.buckets)
        with patch.object(self.state["journal"], "append", side_effect=OSError("private disk detail")):
            await self.commit(self.now + 3)
        self.assertTrue(self.state["storage_error"])
        self.assertEqual(self.learner.loss["raw_append"], 1)
        self.assertEqual({k: b["n"] for k, b in self.learner.buckets.items()}, {k: b["n"] for k, b in before.items()})
        self.assertTrue(all(bucket["last"] is None for bucket in self.learner.buckets.values()))
        await self.commit(self.now + 3)
        self.assertFalse(self.state["storage_error"])
        self.assertEqual(self.learner.eligibility["phase"], "stabilizing")

    async def assert_legacy_restore(self, version, corrupt=False):
        previous = populated_learning(version, self.end)
        self.assertEqual(len(previous.published), 2)
        context = previous.published[HOSTS[0]]["context"]
        with patch("learning.CONTROLLER_VERSION", version):
            text = previous.checkpoint(self.end, self.settings, self.end - SLOT)
        self.assertEqual(json.loads(text)["controller"], version)
        if corrupt:
            data = json.loads(text)
            data['published'][HOSTS[0]]['threshold_w'] = 40
            text = json.dumps(data)
        path = self.root / "learning" / "checkpoint.json"
        path.write_text(text, encoding="utf-8")
        binding = self.root / 'device-token'
        binding.write_bytes(b'00' * 16)
        motor = self.root / 'motor-budget.json'
        motor.write_bytes(b'motor-budget-preserved')
        raw = {file: file.read_bytes() for file in self.state["journal"].files()}
        settings_bytes = (self.root / "settings.json").read_bytes()
        closure(self.storage)["load_learning"](self.settings)
        expected = Learning(HOSTS)
        if corrupt:
            expected.fail()
        self.assertEqual(self.learner.__dict__, expected.__dict__)
        self.assertEqual(closure(self.storage)["durable_published"], {})
        self.assertEqual({file: file.read_bytes() for file in raw}, raw)
        self.assertEqual((self.root / "settings.json").read_bytes(), settings_bytes)
        self.assertEqual(binding.read_bytes(), b'00' * 16)
        self.assertEqual(motor.read_bytes(), b'motor-budget-preserved')
        if corrupt:
            return
        row = await self.commit(self.now)
        self.assertFalse(self.state["learning"]["error"])
        self.assertEqual(self.state["learning"]["published"], {})
        self.assertEqual(self.active_power(row["nodes"], self.now, self.now), {})
        durable = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(durable["controller"], 14)
        self.assertEqual(durable["published"], {})
        self.assertEqual((self.root / "settings.json").read_bytes(), settings_bytes)
        self.assertEqual(binding.read_bytes(), b'00' * 16)
        self.assertEqual(motor.read_bytes(), b'motor-budget-preserved')
        self.assertTrue(all(file.read_bytes().startswith(content) for file, content in raw.items()))
        restored = Learning(HOSTS)
        restored.restore(path.read_text(encoding="utf-8"), self.now + 3, self.settings)
        self.assertFalse(restored.error)
        self.assertEqual(restored.published, {})
        self.assertTrue(restored.buckets)
        self.assertTrue(all(bucket["context"] != context for bucket in restored.buckets.values()))

    async def test_version_two_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(2)

    async def test_version_three_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(3)

    async def test_version_four_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(4)

    async def test_version_five_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(5)

    async def test_version_six_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(6)

    async def test_version_seven_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(7)

    async def test_version_eight_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(8)

    async def test_version_nine_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(9)

    async def test_corrupt_version_nine_rejects_before_discard_and_preserves_raw_settings_binding(self):
        await self.assert_legacy_restore(9, corrupt=True)

    async def test_version_ten_restore_never_caches_old_publication_and_durably_writes_thirteen(self):
        await self.assert_legacy_restore(10)

    async def test_version_twelve_restore_discards_validated_evidence_and_writes_thirteen(self):
        await self.assert_legacy_restore(12)

    async def test_corrupt_version_twelve_rejects_before_discard_and_preserves_raw_settings_binding(self):
        await self.assert_legacy_restore(12, corrupt=True)

    async def test_corrupt_version_ten_rejects_before_discard_and_preserves_raw_settings_binding(self):
        await self.assert_legacy_restore(10, corrupt=True)

    async def test_corrupt_version_eight_rejects_before_discard_and_preserves_raw_settings_binding(self):
        await self.assert_legacy_restore(8, corrupt=True)

    async def test_scenario_refusal_records_raw_data_without_admitting_learning_samples(self):
        for stamp in range(self.end + 3, self.end + 124, 3):
            await self.commit(stamp)
        self.assertEqual(self.learner.eligibility["phase"], "eligible")
        before = {key: (bucket["n"], bucket["seconds"]) for key, bucket in self.learner.buckets.items()}
        published = deepcopy(self.state["learning"]["published"])
        row = await self.commit(self.now + 3, learning_eligible=False)
        self.assertEqual(self.state["learning"]["eligibility"]["reason"], "stabilizing")
        self.assertEqual(self.state["learning"]["published"], published)
        self.assertEqual({key: (bucket["n"], bucket["seconds"]) for key, bucket in self.learner.buckets.items()}, before)
        self.assertTrue(all(bucket["last"] is None for bucket in self.learner.buckets.values()))
        records = [json.loads(line) for path in self.state["journal"].files()
                   for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["ts"], row["ts"])
        self.assertIs(records[-1]["decision"]["learning_eligible"], False)
        await self.commit(self.now + 3)
        self.assertEqual(self.learner.eligibility["reason"], "stabilizing")
        self.assertEqual({key: (bucket["n"], bucket["seconds"]) for key, bucket in self.learner.buckets.items()}, before)

    async def test_clock_invalidation_is_durable_before_restart(self):
        await self.commit(self.now)
        self.assertTrue(self.state["learning"]["published"])
        self.now += 5.5
        row = frame(self.now)
        row["learning"].update(revision=self.state["settings_revision"], elapsed=3)
        self.state.update(nodes=row["nodes"], updated_at=self.now, fan=row["fan"],
                          last_poll=self.now, last_fan=time.monotonic(), learning_wall=self.now)
        with patch("service.atomic_write", wraps=self.service.atomic_write) as writes:
            self.state["queue"].put_nowait(row)
            await asyncio.wait_for(self.state["queue"].join(), 2)
            self.assertEqual(len(writes.call_args_list), 1)
        self.assertFalse(self.state["learning"]["published"])
        restored = Learning(HOSTS)
        restored.restore((self.root / "learning" / "checkpoint.json").read_text(encoding="utf-8"),
                         self.now + 30, self.settings)
        self.assertFalse(restored.error)
        self.assertFalse(restored.published)

    @unittest.skipUnless(os.name == "posix", "real directory sync requires Linux")
    async def test_real_linux_checkpoint_permissions_sync_and_proof_restore(self):
        await self.commit(self.now)
        self.assertTrue(self.state["learning"]["published"])
        self.sync.stop()
        await self.commit(self.end + BUCKET)
        self.assertFalse(self.state["storage_error"])
        self.assertFalse(self.state["learning"]["error"])
        directory = self.root / "learning"
        path = directory / "checkpoint.json"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        restored = Learning(HOSTS)
        restored.restore(path.read_text(encoding="utf-8"), self.now, self.settings)
        self.assertFalse(restored.error)
        self.assertEqual(restored.published, self.state["learning"]["published"])
        self.assertTrue(restored.published)

    async def test_checkpoint_cadence_failure_isolation_and_owned_crash_cleanup(self):
        leftover = self.root / "learning" / ".pending-abcdefgh"
        leftover.write_bytes(b"partial")
        auth_temp = self.root / ".pending-abcdefgh"
        auth_temp.write_bytes(b"must remain")
        with patch("service.atomic_write", wraps=self.service.atomic_write) as writes:
            for offset in (3, 6, 9):
                await self.commit(self.end + offset)
            self.assertFalse(writes.called)
            self.assertFalse(leftover.exists())
            self.assertEqual(auth_temp.read_bytes(), b"must remain")
            self.assertEqual(self.learner.loss["crash_bytes"], 7)
            await self.commit(self.end + BUCKET)
            self.assertEqual(len(writes.call_args_list), 1)
            self.assertEqual(writes.call_args.args[0], self.root / "learning" / "checkpoint.json")
        with patch("service.atomic_write", side_effect=OSError("private path and secret")):
            await self.commit(self.end + 2 * BUCKET)
        self.assertFalse(self.state["storage_error"])
        self.assertTrue(self.state["learning"]["error"])
        self.assertNotIn("secret", self.state["learning"]["error"])
        self.assertFalse(self.state["learning"]["published"])

    async def test_cancelled_worker_keeps_real_thread_ownership(self):
        await self.commit(self.now)
        entered, release = threading.Event(), threading.Event()
        real = self.service.atomic_write
        def blocked(path, text):
            entered.set()
            if not release.wait(3):
                raise AssertionError("checkpoint barrier not released")
            real(path, text)
        with patch("service.atomic_write", side_effect=blocked):
            committing = asyncio.create_task(self.commit(self.end + BUCKET))
            try:
                self.assertTrue(await asyncio.get_running_loop().run_in_executor(None, entered.wait, 1))
                self.worker.cancel()
                await asyncio.sleep(.01)
                self.assertFalse(self.worker.done())
                self.assertEqual(self.state["queue"]._unfinished_tasks, 1)
                self.worker.cancel()
                await asyncio.sleep(.01)
                self.assertFalse(self.worker.done())
            finally:
                release.set()
                await committing
                with self.assertRaises(asyncio.CancelledError):
                    await self.worker
        self.assertEqual(self.state["queue"]._unfinished_tasks, 0)


if __name__ == "__main__":
    unittest.main()
