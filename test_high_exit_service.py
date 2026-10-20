import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import service
from test_fan_snapshot import matched_fan
from test_scenarios import feedback, fleet
from test_service import FakeFan
from test_thermal_memory_service import closure


class HighExitServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 0
        for name, value in (('time', SimpleNamespace(monotonic=lambda: self.now,
                time=lambda: 1000 + self.now)), ('fsync_directory', lambda path: None)):
            patcher = patch.object(service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(service.MotorSchedule, 'load', return_value=service.MotorSchedule())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = await service.create_app(Path(self.directory.name))
        self.state = self.app[service.STATE]
        self.controller = self.state['controller']
        self.memory = self.state['thermal_memory']
        self.state['settings']['manual_level'] = None
        loops = closure(self.app.on_startup[-1])
        self.apply = closure(loops['fan'])['apply_fan']
        self.snapshot = closure(self.apply)['request_snapshot']
        self.device = FakeFan(['on', 'favorite', 16, 2096])

    async def asyncTearDown(self):
        if self.memory.task is not None:
            await self.memory.task
        await self.state['qr'].close()
        self.state['disk'].shutdown(wait=True)
        self.state['history_disk'].shutdown(wait=True)

    def qualify(self):
        for self.now in range(0, 601, 3):
            decision = self.controller.decide(fleet(self.now, cpu=65, gpu=52, board=64,
                load=60, power=100), self.now, minimum_level=0,
                feedback=feedback(self.now, 16, confirmed=self.now == 0,
                    execution='matched' if self.now == 0 else 'pending', rpm=2096))
            operation = decision.get('execution_trial') or {}
            if operation.get('status') == 'pending' and operation.get('purpose') == 'ceiling_exit':
                break
        self.assertEqual((operation.get('from'), operation.get('to')), (16, 15),
            'The service test needs the real controller to qualify an adjacent loaded high exit')
        self.state.update(decision=decision, fan=matched_fan(16, self.now, 1000 + self.now),
            last_poll=self.now, last_fan=self.now,
            fan_request=service.FanRequest(decision['level'], high_wind=self.controller.high_wind,
                motion=self.controller.motion))
        self.state['fan'].update(confirmed=False, execution='pending', attempts=0)

    async def test_high_exit_waits_for_exact_tagged_snapshot_then_writes_only_fifteen(self):
        self.qualify()
        self.assertEqual(self.snapshot().level, 16)
        self.assertTrue(self.snapshot().upward_only)
        await self.apply(self.device)
        self.assertEqual(self.device.writes, [])
        self.memory.publish(self.controller)
        await self.memory.task
        self.assertTrue(self.memory.ready)
        saved = self.memory.store.load()
        self.assertEqual(saved, self.controller.export_thermal_memory())
        self.assertIsNone(saved['open_edge'])
        self.assertEqual(saved['intent'], dict(from_level=16, to_level=15, purpose='ceiling_exit', state='pending'))
        self.assertEqual(self.snapshot().level, 15)
        self.assertFalse(self.snapshot().upward_only)
        await self.apply(self.device)
        self.assertEqual(self.device.writes, [15])
        self.assertNotIn(10, self.device.writes)

    async def test_failed_snapshot_save_prevents_high_exit_without_resetting_motor(self):
        self.qualify()
        with patch.object(self.memory.store, 'save', side_effect=OSError('private/path')):
            self.memory.publish(self.controller)
            await self.memory.task
        self.assertFalse(self.memory.ready)
        self.assertEqual(self.snapshot().level, 16)
        await self.apply(self.device)
        self.assertEqual(self.device.writes, [])
        self.assertEqual(self.state['motor_schedule'].attempts, 0)
        self.assertFalse(self.state['motor_schedule'].fault)

    async def test_current_memory_gate_is_rechecked_after_high_exit_motor_reservation(self):
        self.qualify()
        await self.memory.flush(self.controller)
        self.assertTrue(self.memory.ready)
        schedule = self.state['motor_schedule']
        schedule.budget_path = Path(self.directory.name) / 'motor-budget.json'
        reserve = schedule.persist_debt_then_set_budget
        reservations = []
        def revoke_after_reservation(*args, **kwargs):
            reservations.append(args)
            result = reserve(*args, **kwargs)
            self.memory.invalidate()
            return result
        with patch.object(schedule, 'persist_debt_then_set_budget', side_effect=revoke_after_reservation):
            await self.apply(self.device)
        self.assertEqual(self.device.writes, [])
        self.assertEqual(reservations[0][:2], (1, 15))
        self.assertEqual(schedule.attempts, 1)
        self.assertEqual(schedule.expected_level, 15)
        durable = json.loads(schedule.budget_path.read_text())
        self.assertEqual((durable['attempts'], durable['expected_level']), (1, 15))
        self.assertFalse(durable['fault'])
        self.assertFalse(self.memory.ready)


if __name__ == '__main__':
    unittest.main()
