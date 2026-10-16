import unittest
from unittest.mock import patch
import service
from core import Controller
from test_core import HOSTS
from test_scenarios import fleet, feedback
from test_service import FakeFan


class StableControlTests(unittest.TestCase):
    def step(self, controller, now, actual, *, stamp=None, **values):
        return controller.decide(fleet(now, cpu=values.pop('cpu', 49), gpu=47,
            board=values.pop('board', 54), power=values.pop('power', 60), **values),
            now, minimum_level=0, feedback=feedback(now if stamp is None else stamp, actual))

    def test_flat_workload_epoch_never_staircases(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 0
        commands = []
        for now in range(0, 303, 3):
            decision = self.step(controller, now, actual, load=35, board=59)
            actual = decision['level']
            commands.append(actual)
        self.assertEqual(max(commands), 2)
        self.assertEqual(commands[-1], 2)
        self.assertEqual(decision['demand'], 2)

    def test_repeated_short_pulses_share_epoch(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 0
        commands = []
        for now in range(0, 303, 3):
            decision = self.step(controller, now, actual, load=35 if now % 30 < 12 else 5)
            actual = decision['level']
            commands.append(actual)
        self.assertEqual(max(commands), 2)

    def test_fresh_duplicate_preserves_credit_without_elapsed_credit(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 55, 3):
            decision = self.step(controller, now, 10)
        self.assertEqual(decision['operating']['observe_credit'], 24)
        duplicate = self.step(controller, 57, 10, stamp=54)
        self.assertEqual(duplicate['operating']['observe_credit'], 24)
        advancing = self.step(controller, 60, 10)
        self.assertEqual(advancing['operating']['observe_credit'], 27)

    def test_confirmed_descent_observes_180_seconds(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        edges = []
        for now in range(0, 450, 3):
            decision = self.step(controller, now, actual)
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertEqual(edges[0], (210, 10, 9))
        self.assertGreaterEqual(edges[1][0] - edges[0][0], 183)

    def test_failed_floor_blocks_same_and_lower_destinations(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        failed = confirmed_at = None
        for now in range(0, 600, 3):
            pulse = confirmed_at is not None and now == confirmed_at + 3
            decision = self.step(controller, now, actual, cpu=58 if pulse else 49)
            if decision['scenario'] == 'recovery_released' and confirmed_at is None:
                confirmed_at = now
            if (decision.get('trial') or {}).get('outcome') == 'failed':
                failed = failed or now
            actual = decision['level']
            if failed is not None:
                self.assertGreater(actual, 9)
        self.assertIsNotNone(confirmed_at)
        self.assertEqual(failed, confirmed_at + 3)
        self.assertTrue(decision['retry_blocked'])

    def test_critical_witness_survives_unrelated_missing_node(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        nodes = fleet(0, cpu=75)
        nodes[1].update(online=False, failure_kind="transport", sensors=[])
        decision = controller.decide(nodes, 0, minimum_level=0, feedback=feedback(0, 2))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(controller.motion.actual_c, 75)
        self.assertIsNotNone(controller.motion.source)

    def test_coupled_transport_retains_anchor_and_unknown_actual(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 55, 3):
            self.step(controller, now, 7)
        anchor = controller._policy['fan_transport_safe']
        for now in (57, 60):
            nodes = fleet(now, cpu=49, gpu=47, board=54, power=60, load=5)
            nodes[1].update(online=False, failure_kind='transport', sensors=[])
            decision = controller.decide(nodes, now, minimum_level=0,
                feedback=feedback(now, None, confirmed=False, execution='pending', rpm=None,
                    readback_hold=dict(level=7, until=69, failures=1)))
            self.assertEqual(decision['level'], 7)
            self.assertEqual(controller._policy['fan_transport_safe'], anchor)
            self.assertEqual(decision['scenario'], 'communication_hold')
        self.assertTrue(controller.communication_safe(84))
        self.assertFalse(controller.communication_safe(84.001))

    def test_middle_band_releases_only_one_after_180_seconds(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        edges = []
        for now in range(0, 450, 3):
            decision = self.step(controller, now, actual, board=59, load=5)
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertEqual(edges[:2], [(210, 10, 9), (393, 9, 8)])

    def test_failure_is_not_cleared_by_protection_or_clock_alone(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        confirmed_at = failed_at = None
        for now in range(0, 900, 3):
            pulse = confirmed_at is not None and now == confirmed_at + 3
            decision = self.step(controller, now, actual, cpu=58 if pulse else 49,
                                 board=59 if failed_at is not None else 54)
            if decision['scenario'] == 'recovery_released' and confirmed_at is None:
                confirmed_at = now
            if decision['retry_blocked'] and failed_at is None:
                failed_at = now
            actual = decision['level']
            if failed_at is not None and now == failed_at + 3:
                controller.protect('storage evidence interruption', now)
            if failed_at is not None:
                self.assertTrue(decision['retry_blocked'])
                self.assertGreater(actual, 9)
        self.assertIsNotNone(failed_at)
        self.assertEqual(decision['operating']['failed_floor']['failed_at'], confirmed_at + 3)

    def test_nonidle_release_needs_180_seconds(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        edges = []
        for now in range(0, 240, 3):
            decision = self.step(controller, now, actual, load=20)
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertEqual(edges, [(210, 10, 9)])

    def test_uncommanded_actual_change_retires_failed_edge_authority(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        for now in range(0, 214, 3):
            decision = self.step(controller, now, actual)
            actual = decision['level']
        self.assertEqual(decision['scenario'], 'recovery_released')
        confirmed_at = now
        self.step(controller, confirmed_at + 3, 10)
        decision = self.step(controller, confirmed_at + 6, 9, cpu=58)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertIsNone(controller.motion.restore_level)
        self.assertFalse(decision['retry_blocked'])

    def test_brief_node_transport_recovers_without_new_ten_floor(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 55, 3):
            self.step(controller, now, 7)
        levels = []
        for now in (57, 60, 63, 66):
            nodes = fleet(now, cpu=49, gpu=47, board=54, power=60, load=5)
            if now == 57:
                nodes[1].update(online=False, failure_kind='transport', sensors=[])
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 7))
            levels.append(decision['level'])
        self.assertEqual(levels, [7, 7, 7, 7])

    def test_transient_load_change_permanently_invalidates_old_edge(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        for now in range(0, 214, 3):
            decision = self.step(controller, now, actual)
            actual = decision['level']
        self.assertEqual(decision['scenario'], 'recovery_released')
        confirmed_at = now
        self.step(controller, confirmed_at + 3, actual, load=20)
        decision = self.step(controller, confirmed_at + 6, actual, cpu=58, load=5)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertIsNone(controller.motion.restore_level)

    def test_stale_feedback_revokes_credit(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 55, 3):
            self.step(controller, now, 10)
        decision = self.step(controller, 57, 10, stamp=45)
        self.assertEqual(decision['operating']['observe_credit'], 0)



class HeadroomReleaseTests(unittest.TestCase):
    def sample(self, controller, now, actual, cpu, board=50):
        return controller.decide(fleet(now, cpu=cpu, gpu=44, board=board, power=60, load=5),
            now, minimum_level=0, feedback=feedback(now, actual))

    def test_tiny_drift_with_large_headroom_releases(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        edges = []
        for now in range(0, 450, 3):
            decision = self.sample(controller, now, actual, 45 + now * .001)
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertEqual(edges[:2], [(210, 10, 9), (393, 9, 8)])

    def test_exact_eight_allows_drift_but_below_eight_vetoes(self):
        for board, expected in ((57, [(210, 10, 9)]), (57.001, [])):
            with self.subTest(board=board):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                actual = 10
                edges = []
                for now in range(0, 241, 3):
                    decision = self.sample(controller, now, actual, 45 + now * .001, board)
                    if decision['level'] < actual:
                        edges.append((now, actual, decision['level']))
                    actual = decision['level']
                self.assertEqual(edges, expected)

    def test_pending_descent_keeps_mild_drift_but_revokes_near_target(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 211, 3):
            decision = self.sample(controller, now, 10, 45)
        self.assertEqual(decision['level'], 9)
        requested_at = now
        deadline = decision['execution_trial']['original_deadline']
        for now in (requested_at + 3, requested_at + 6, requested_at + 9):
            decision = self.sample(controller, now, 10, 45 + (now - requested_at) * .001)
            self.assertEqual(decision['level'], 9)
            self.assertEqual(decision['execution_trial']['status'], 'pending')
            self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
        decision = self.sample(controller, requested_at + 12, 10, 58)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertEqual(decision['level'], 10)

    def test_warming_still_blocks_failed_floor_cool_requalification(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        confirmed_at = failed_at = None
        for now in range(0, 900, 3):
            pulse = confirmed_at is not None and now == confirmed_at + 3
            cpu = 58 if pulse else 45 if failed_at is None else 45 + (now - failed_at) * .001
            decision = self.sample(controller, now, actual, cpu)
            if decision['scenario'] == 'recovery_released' and confirmed_at is None:
                confirmed_at = now
            if decision['retry_blocked'] and failed_at is None:
                failed_at = now
            actual = decision['level']
            if failed_at is not None:
                self.assertTrue(decision['retry_blocked'])
                self.assertGreater(actual, 9)
        self.assertIsNotNone(failed_at)
        self.assertEqual(decision['operating']['failed_floor']['failed_at'], confirmed_at + 3)


class StableWorkerTests(unittest.TestCase):
    def test_ordinary_ascent_two_levels_ten_seconds(self):
        device = FakeFan(['on', 'favorite', 2, 900])
        schedule = service.MotorSchedule()
        for now, expected in ((100, [4]), (108, [4]), (110, [4, 6])):
            with patch.object(service.time, 'monotonic', return_value=now):
                service.fan_apply(device, lambda: service.FanRequest(10), schedule, high_wind_gate=True)
            self.assertEqual(device.writes, expected)
        self.assertEqual(schedule.attempts, 0)

    def test_final_protection_merge_cannot_bypass_pacing(self):
        device = FakeFan(['on', 'favorite', 2, 900])
        requests = iter([service.FanRequest(3)] * 3 + [service.FanRequest(16, protected=True)] * 20)
        with patch.object(service.time, 'monotonic', return_value=100):
            service.fan_apply(device, lambda: next(requests), service.MotorSchedule(), high_wind_gate=True)
        self.assertEqual(device.writes, [4])

    def test_only_current_critical_witness_bypasses_step(self):
        from core import MotionAuthority, HighWindAuthority
        for motion, high, expected in (
            (MotionAuthority(100, ('host', 'cpu'), 75, 75), None, 10),
            (MotionAuthority(94, ('host', 'cpu'), 75, 75), None, 4),
            (MotionAuthority(100, ('host', 'cpu'), 74, 75), None, 4),
            (MotionAuthority(100, ('host', 'cpu'), 75, 75), HighWindAuthority(True, ('host', 'DGX', 'cpu'), 100, 80), 16)):
            device = FakeFan(['on', 'favorite', 2, 900])
            with patch.object(service.time, 'monotonic', return_value=100):
                service.fan_apply(device, lambda: service.FanRequest(16, protected=True,
                    high_wind=high, motion=motion), service.MotorSchedule(), high_wind_gate=True)
            self.assertEqual(device.writes, [expected])

    def test_authority_expiring_during_reservation_recomputes_exact_target(self):
        from core import MotionAuthority
        device = FakeFan(['on', 'favorite', 2, 900])
        schedule = service.MotorSchedule()
        clock = [100]
        persist = schedule.persist_debt_then_set_budget
        def reserve(*args):
            clock[0] = 106
            return persist(*args)
        with patch.object(service.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(schedule, 'persist_debt_then_set_budget', side_effect=reserve):
            service.fan_apply(device, lambda: service.FanRequest(10,
                motion=MotionAuthority(100, ('host', 'cpu'), 75, 75)), schedule, high_wind_gate=True)
        self.assertEqual(device.writes, [4])
        self.assertEqual(schedule.attempts, 0)

    def test_unknown_readback_never_writes_with_critical_authority(self):
        from core import MotionAuthority
        device = FakeFan(['on', 'favorite', 2, 900])
        device.fail = True
        with patch.object(service.time, 'monotonic', return_value=100):
            result = service.fan_apply(device, lambda: service.FanRequest(10,
                motion=MotionAuthority(100, ('host', 'cpu'), 75, 75)), service.MotorSchedule(), high_wind_gate=True)
        self.assertEqual(device.writes, [])
        self.assertIsNone(result['level'])


class StableServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_canceled_old_execution_does_not_block_current_feedback(self):
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from test_fan_snapshot import matched_fan
        clock = [0]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(service, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0])), \
                patch.object(service.MotorSchedule, 'load', return_value=service.MotorSchedule()):
            app = await service.create_app(Path(directory), high_wind_gate=True)
            state = app[service.STATE]
            state['settings']['manual_level'] = None
            try:
                controller = state['controller']
                handler = next(r.handler for r in app.router.routes() if r.resource.canonical == '/api/status')
                actual = 10
                for now in range(0, 214, 3):
                    clock[0] = now
                    decision = controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=5),
                        now, minimum_level=0, feedback=feedback(now, actual))
                    actual = decision['level']
                self.assertEqual(actual, 9)
                self.assertEqual(decision['scenario'], 'recovery_released')
                confirmed_at = now
                decision = controller.protect('transient storage interruption', confirmed_at + 1)
                self.assertEqual(decision['level'], 10)
                state.update(decision=decision, fan_request=service.FanRequest(10))
                recovered = []
                for now in range(confirmed_at + 3, confirmed_at + 243, 3):
                    clock[0] = now
                    state.update(fan=matched_fan(10, now, 1000 + now), last_poll=now, last_fan=now)
                    public = json.loads((await handler(SimpleNamespace())).body)
                    self.assertTrue(public['fan']['confirmed'])
                    self.assertEqual(public['fan']['level'], 10)
                    if now == confirmed_at + 3:
                        controller._policy['operation'].update(status='failed', outcome='failed')
                        self.assertTrue(json.loads((await handler(SimpleNamespace())).body)['fan']['confirmed'])
                        controller._policy['operation'].update(status='invalidated', outcome='invalidated')
                    decision = controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=5),
                        now, minimum_level=0, feedback=public['fan'])
                    state.update(decision=decision, fan_request=service.FanRequest(decision['level']))
                    if decision['level'] < 10:
                        recovered.append((now, decision['level']))
                        break
                self.assertEqual(len(recovered), 1)
                self.assertGreaterEqual(recovered[0][0] - (confirmed_at + 3), 180)
                self.assertEqual(recovered[0][1], 9)
                self.assertEqual(decision['execution_trial']['status'], 'pending')
                requested_at = recovered[0][0]
                state.update(fan=matched_fan(9, requested_at, 1000 + requested_at), last_poll=requested_at, last_fan=requested_at)
                public = json.loads((await handler(SimpleNamespace())).body)
                self.assertFalse(public['fan']['confirmed'])
                clock[0] = requested_at + 6
                state.update(fan=matched_fan(9, clock[0], 1000 + clock[0]), last_poll=clock[0], last_fan=clock[0])
                self.assertTrue(json.loads((await handler(SimpleNamespace())).body)['fan']['confirmed'])
            finally:
                await state['qr'].close()
                state['disk'].shutdown(wait=True)
                state['history_disk'].shutdown(wait=True)

    async def test_coupled_transport_public_request_and_recovery(self):
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from test_fan_snapshot import matched_fan
        clock = [54]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(service, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0])), \
                patch.object(service.MotorSchedule, 'load', return_value=service.MotorSchedule()):
            app = await service.create_app(Path(directory), high_wind_gate=True)
            state = app[service.STATE]
            state['settings']['manual_level'] = None
            try:
                controller = state['controller']
                for now in range(0, 55, 3):
                    decision = controller.decide(fleet(now, cpu=49, gpu=47, board=54, power=60, load=5),
                        now, minimum_level=0, feedback=feedback(now, 7))
                startup = app.on_startup[-1]
                fan_loop = dict(zip(startup.__code__.co_freevars,
                    (cell.cell_contents for cell in startup.__closure__)))['fan']
                state.update(decision=decision, fan_request=service.FanRequest(7),
                    fan=matched_fan(7, 54, 1054), last_poll=54, last_fan=54, last_identity=54)
                class Missing(FakeFan):
                    missing = True
                    def send(self, *args):
                        if self.missing:
                            raise TimeoutError('transport')
                        return super().send(*args)
                device = Missing(['on', 'favorite', 7, 900])
                state['device'] = device
                async def once():
                    state['stop'] = False
                    async def stop(delay):
                        state['stop'] = True
                    with patch.object(service.asyncio, 'sleep', side_effect=stop):
                        await fan_loop()
                await once()
                clock[0] = 57
                nodes = fleet(57, cpu=49, gpu=47, board=54, power=60, load=5)
                nodes[1].update(online=False, failure_kind='transport', sensors=[])
                observed = dict(state['fan'], readback_hold=dict(level=7, until=84, failures=1))
                decision = controller.decide(nodes, 57, minimum_level=0, feedback=observed)
                state.update(decision=decision, fan_request=service.FanRequest(decision['level']), last_poll=57)
                handler = next(r.handler for r in app.router.routes() if r.resource.canonical == '/api/status')
                public = json.loads((await handler(SimpleNamespace())).body)
                self.assertEqual(public['requested_level'], 7)
                self.assertIsNone(public['fan']['level'])
                self.assertEqual(device.writes, [])
                state['storage_error'] = 'disk'
                self.assertEqual(json.loads((await handler(SimpleNamespace())).body)['requested_level'], 10)
                state['storage_error'] = ''
                device.missing = False
                for now in (60, 63):
                    clock[0] = now
                    state['last_poll'] = now
                    await once()
                self.assertEqual(device.writes, [])
                public = json.loads((await handler(SimpleNamespace())).body)
                self.assertEqual(public['requested_level'], 7)
                self.assertEqual(public['fan']['level'], 7)
            finally:
                await state['qr'].close()
                state['disk'].shutdown(wait=True)
                state['history_disk'].shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
