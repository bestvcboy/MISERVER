import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import service
from test_fan_snapshot import matched_fan
from test_scenarios import feedback, fleet
from test_service import FakeFan


def closure(function):
    return dict(zip(function.__code__.co_freevars,
                    (cell.cell_contents for cell in function.__closure__)))


class ThermalMemoryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 210
        for target, value in (("time", SimpleNamespace(monotonic=lambda: self.now,
                time=lambda: 1000 + self.now)), ("fsync_directory", lambda path: None)):
            patcher = patch.object(service, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(service.MotorSchedule, "load", return_value=service.MotorSchedule())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = await service.create_app(Path(self.directory.name))
        self.state = self.app[service.STATE]
        self.memory = self.state["thermal_memory"]
        self.controller = self.state["controller"]
        self.state["settings"]["manual_level"] = 5
        for now in range(0, 211, 3):
            decision = self.controller.decide(fleet(now, cpu=40, gpu=40, board=45, load=5, power=45),
                now, feedback=feedback(now, 5), minimum_level=5, manual_level=5)
        self.state.update(decision=decision, fan=matched_fan(5, self.now, 1000 + self.now),
                          last_poll=self.now, last_fan=self.now)
        loops = closure(self.app.on_startup[-1])
        self.apply = closure(loops["fan"])["apply_fan"]
        self.snapshot = closure(self.apply)["request_snapshot"]
        self.status = next(route.handler for route in self.app.router.routes()
                           if route.resource.canonical == "/api/status")
        self.device = FakeFan(["on", "favorite", 5, 900])

    async def asyncTearDown(self):
        if self.memory.task is not None:
            await self.memory.task
        await self.state["qr"].close()
        self.state["disk"].shutdown(wait=True)
        self.state["history_disk"].shutdown(wait=True)

    def request(self, level):
        self.memory.invalidate()
        self.state["settings"]["manual_level"] = level
        for _ in range(80):
            self.now += 3
            decision = self.controller.decide(fleet(self.now, cpu=40, gpu=40, board=45, load=5, power=45),
                self.now, feedback=feedback(self.now, 5), minimum_level=level, manual_level=level)
            if decision["level"] == level:
                break
        self.assertEqual(decision["level"], level)
        self.state.update(decision=decision, fan=matched_fan(5, self.now, 1000 + self.now),
                          last_poll=self.now, last_fan=self.now,
                          fan_request=service.FanRequest(decision["level"],
                              high_wind=self.controller.high_wind, motion=self.controller.motion))

    async def test_manual_descent_waits_for_exact_durable_memory_before_motor_write(self):
        self.request(4)
        self.assertTrue(self.snapshot().upward_only)
        await self.apply(self.device)
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.state["motor_schedule"].attempts, 0)
        self.memory.publish(self.controller)
        await self.memory.task
        self.assertTrue(self.memory.ready)
        self.assertFalse(self.snapshot().upward_only)
        await self.apply(self.device)
        self.assertEqual(self.device.writes, [4])

    async def test_failed_save_blocks_descent_without_inventing_thermal_or_motor_fault(self):
        self.request(4)
        with patch.object(self.memory.store, "save", side_effect=OSError("private/path")):
            self.memory.publish(self.controller)
            await self.memory.task
        public = json.loads((await self.status(SimpleNamespace())).body)
        self.assertEqual(public["thermal_memory"]["state"], "failed")
        self.assertNotIn("private/path", json.dumps(public))
        self.assertEqual(public["requested_level"], 5)
        self.assertFalse(self.snapshot().protected)
        await self.apply(self.device)
        self.assertEqual(self.device.writes, [])
        self.assertFalse(self.state["motor_schedule"].fault)

    async def test_pending_memory_does_not_block_valid_upward_motion(self):
        self.request(7)
        self.assertTrue(self.snapshot().upward_only)
        await self.apply(self.device)
        self.assertEqual(self.device.writes, [7])

    async def test_gate_is_rechecked_after_motor_budget_reservation(self):
        self.request(4)
        await self.memory.flush(self.controller)
        self.assertTrue(self.memory.ready)
        schedule = self.state["motor_schedule"]
        reserve = schedule.persist_debt_then_set_budget
        reservations = []
        def revoke_after_reservation(*args, **kwargs):
            reservations.append(args)
            result = reserve(*args, **kwargs)
            self.memory.invalidate()
            return result
        with patch.object(schedule, "persist_debt_then_set_budget", side_effect=revoke_after_reservation):
            await self.apply(self.device)
        self.assertEqual(self.device.writes, [])
        self.assertEqual(reservations[0][:2], (1, 4))
        self.assertFalse(self.memory.ready)

    async def test_status_queries_cannot_save_memory_or_restore_descent_credit(self):
        self.request(4)
        before = self.controller.export_thermal_memory()
        for _ in range(4):
            public = json.loads((await self.status(SimpleNamespace())).body)
            self.assertFalse(public["thermal_memory"]["descent_allowed"])
            self.assertEqual((public["control_period_seconds"], public["record_period_seconds"]), (3, 3))
        self.assertEqual(self.controller.export_thermal_memory(), before)
        self.assertIsNone(self.memory.task)
        self.assertFalse(self.memory.store.path.exists())

    async def test_service_cleanup_flushes_new_failure_behind_the_old_writer(self):
        from test_closed_loop_policy import ClosedLoopPolicyTests
        from policy_memory import ThermalMemoryStore
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        write = self.memory.store.write
        def delayed(path, encoded):
            if path.name == "thermal-memory.json" and not release.is_set():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError()
            write(path, encoded)
        self.memory.store.write = delayed
        self.memory.publish(self.controller)
        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
        failed = ClosedLoopPolicyTests().failed_one_host()
        final_snapshot = failed.export_thermal_memory()
        self.assertTrue(final_snapshot["failures"])
        self.state["controller"] = failed
        self.memory.invalidate()
        self.memory.publish(failed)
        shutdown = closure(self.app.on_cleanup[-1])["shutdown"]
        closing = asyncio.create_task(shutdown())
        release.set()
        await closing
        reopened = ThermalMemoryStore(self.memory.store.root, self.memory.store.identity,
                                      service.atomic_write, lambda path: None)
        self.assertEqual(reopened.load(), final_snapshot)


if __name__ == "__main__":
    unittest.main()
