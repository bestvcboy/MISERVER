import asyncio
import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import service
from core import Controller, normalize
from learning import Learning, context_of
from test_service import FakeFan


def matched_fan(level=16, mono=99, wall=999):
    return dict(configured=True, confirmed=True, level=level, rpm=2044,
                power="on", mode="favorite", verified_at=wall, observed_mono=mono,
                execution="matched", fault=False, attempts=1, error="retained evidence",
                ambient_c=29.6, ambient_source="purifier:70632677:temp_dec",
                ambient_observed_mono=mono, ambient_verified_at=wall,
                protection_interrupted=False)


def execution(level, stamp=100):
    return dict(to=level, destination_requested_at=stamp, requested_at=90,
                original_deadline=150, deadline=150, status="pending")


def telemetry(wall):
    return dict(timestamp=wall, cpu_percent=0, gpu_percent=0, system_power_w=35,
                system_power_source="spbm", temperatures=dict(gpu=40,
                    acpi=[dict(zone="0", temp=45)],
                    mlx=[dict(chip="mlx5", label="asic", temp=50)]))


class LocalTelemetry:
    def __init__(self, wall):
        self.wall = wall

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, *args, **kwargs):
        wall = self.wall

        class Response:
            status = 200

            async def __aenter__(self):
                self.content = self
                return self

            async def __aexit__(self, *args):
                pass

            async def iter_chunked(self, size):
                yield json.dumps(telemetry(wall)).encode()

        return Response()


class CurrentRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mono, self.wall = 100, 1000
        self.clock = patch("service.time", SimpleNamespace(
            monotonic=lambda: self.mono, time=lambda: self.wall))
        self.clock.start()
        with patch("service.MotorSchedule.load", return_value=service.MotorSchedule()):
            self.app = await service.create_app(self.root)
        self.state = self.app[service.STATE]
        self.state["settings"]["manual_level"] = None
        self.state["fan"] = matched_fan()
        self.state["last_fan"] = self.state["last_poll"] = self.mono
        self.nodes = [normalize(host, telemetry(self.wall), self.mono, self.wall)
                      for host in service.HOSTS]
        startup = self.app.on_startup[-1]
        self.monitor = dict(zip(startup.__code__.co_freevars,
            (cell.cell_contents for cell in startup.__closure__)))["monitor"]
        self.status = next(route.handler for route in self.app.router.routes()
                          if route.resource.canonical == "/api/status")

    async def asyncTearDown(self):
        await self.state["qr"].close()
        self.state["disk"].shutdown(wait=True)
        self.state["history_disk"].shutdown(wait=True)
        self.clock.stop()
        self.temp.cleanup()

    async def public(self):
        return json.loads((await self.status(SimpleNamespace())).body)

    async def sample(self):
        self.state["stop"] = False

        async def stop_after_row(delay):
            self.state["stop"] = True

        with patch("service.aiohttp.ClientSession", side_effect=lambda **kw: LocalTelemetry(self.wall)), \
                patch("service.asyncio.sleep", stop_after_row):
            await self.monitor()
        self.state["stop"] = False
        return self.state["queue"].get_nowait()

    def install_policy(self):
        policy = {service.HOSTS[0]: dict(version=14, threshold_w=75, source="spbm",
            generated_at=self.wall, evidence_from=self.wall - 100,
            context=context_of(self.nodes))}
        self.state.update(nodes=deepcopy(self.nodes), learning_wall=self.wall,
                          learning=dict(published=policy, settings_revision=0, error=""))
        return policy

    def assert_evidence(self, projected, physical):
        self.assertEqual(projected, dict(physical, confirmed=False, execution="pending"))
        self.assertEqual(self.state["fan"], physical)

    async def test_status_actual_r2_transitions_preserve_physical_evidence_and_deadline(self):
        rows = json.loads(Path(__file__).with_name(
            "test_fan_snapshot_transitions.json").read_text(encoding="utf-8"))["changes_and_final_rows"]
        for row in (rows[0], rows[-1]):
            with self.subTest(requested=row["requested"], actual=row["fan"]["level"]):
                self.mono, self.wall = row["learning"]["mono"], row["snapshot_at"]
                self.state.update(fan=deepcopy(row["fan"]), decision=deepcopy(row["decision"]),
                                  last_fan=self.mono, last_poll=self.mono)
                physical, decision = deepcopy(self.state["fan"]), deepcopy(self.state["decision"])
                debt = deepcopy(vars(self.state["motor_schedule"]))
                result = await self.public()
                self.assertEqual(result["requested_level"], row["requested"])
                self.assert_evidence(result["fan"], physical)
                self.assertEqual(self.state["decision"], decision)
                self.assertEqual(vars(self.state["motor_schedule"]), debt)
                self.assertEqual(result["decision"]["execution_trial"]["original_deadline"],
                                 decision["execution_trial"]["destination_requested_at"] + 60)

    async def test_status_same_level_return_and_aba_require_new_destination_stamp(self):
        controller = self.state["controller"]
        controller._request(15, 16, 90, None, purpose="release", profile="ordinary")
        for now, target in ((100, 16), (101, 15), (102, 16)):
            controller._request(target, 16, now, None, purpose="rollback", profile="ordinary")
            self.mono = now
            self.state.update(decision=controller._decision("trial_pending", "retarget"),
                              fan=matched_fan(target, now - 1), last_poll=now, last_fan=now)
            physical = deepcopy(self.state["fan"])
            result = await self.public()
            self.assertEqual(result["requested_level"], target)
            self.assert_evidence(result["fan"], physical)
            self.assertEqual(result["decision"]["execution_trial"]["destination_requested_at"], now)
            self.assertEqual(result["decision"]["execution_trial"]["original_deadline"], 150)
            self.state["fan"]["observed_mono"] = now + .1
            self.mono = now + .1
            result = await self.public()
            self.assertEqual(result["fan"]["execution"], "matched")
            self.assertIs(result["fan"]["confirmed"], True)

    async def test_status_strict_equal_nonfinite_future_and_malformed_current_provenance(self):
        bad_trials = [{}, "old", False, [], execution(14, 98), execution(True, 98)]
        bad_trials += [execution(15, stamp) for stamp in
                       (None, True, "98", math.nan, math.inf, -math.inf, 99, 100, 101)]
        for trial in bad_trials:
            with self.subTest(trial=trial):
                self.state.update(fan=matched_fan(15),
                    decision=dict(level=15, execution_trial=trial, trial=execution(15, 90)))
                physical = deepcopy(self.state["fan"])
                result = await self.public()
                self.assert_evidence(result["fan"], physical)
        for stamp in (None, True, "99", math.nan, math.inf, -math.inf, 101, 90):
            with self.subTest(observation=stamp):
                self.state.update(fan=matched_fan(16, stamp),
                                  decision=dict(level=16, execution_trial=execution(16, 98)))
                result = await self.public()
                self.assertEqual(result["requested_level"], 16)
                self.assertIs(result["fan"]["confirmed"], False)
                self.assertEqual(result["fan"]["execution"], "pending")
        self.state.update(fan=matched_fan(15), decision=dict(level=15, execution_trial=execution(15, 98)))
        self.assertEqual((await self.public())["fan"], self.state["fan"])

    async def test_status_never_upgrades_pending_fault_unknown_or_completed_old_ack(self):
        for outcome in ("pending", "fault", "unknown", None):
            with self.subTest(execution=outcome):
                physical = dict(matched_fan(15), execution=outcome, confirmed=False,
                                fault=outcome == "fault")
                self.state.update(fan=physical, decision=dict(level=15, execution_trial=execution(15, 98)))
                result = await self.public()
                self.assertEqual(result["fan"], physical)
                self.assertIs(result["fan"]["confirmed"], False)
        self.state.update(fan=matched_fan(15), decision=dict(level=15,
            execution_trial=dict(execution(15, 100), status="confirmed"), trial=execution(15, 90)))
        self.assert_evidence((await self.public())["fan"], deepcopy(self.state["fan"]))

    async def test_status_no_episode_baseline_and_existing_same_actual_request_are_valid(self):
        for gear in (16, 10, 0):
            for metadata in ({}, dict(execution_trial=None), dict(execution_trial=None, trial=execution(14, 100))):
                with self.subTest(gear=gear, metadata=metadata):
                    controller = Controller(service.HOSTS)
                    controller._request(gear, gear, 100, None, purpose="reserve", profile="ordinary")
                    self.assertIsNone(controller._decision("hold", "same actual")["execution_trial"])
                    self.state.update(fan=matched_fan(gear), decision=dict(level=gear, **metadata))
                    result = await self.public()
                    self.assertEqual(result["requested_level"], gear)
                    self.assertEqual(result["fan"], self.state["fan"])

    async def test_status_authoritative_physical_health_faults_block_policy_without_rewriting_evidence(self):
        self.install_policy()
        for changes in (dict(fault=True), dict(fault=None), dict(fault="unknown"),
                        dict(rpm=0), dict(power="off"), dict(mode="auto"),
                        dict(verified_at=990), dict(verified_at=1001)):
            with self.subTest(changes=changes):
                self.state.update(fan=dict(matched_fan(16), **changes), decision=dict(level=15))
                physical = deepcopy(self.state["fan"])
                result = await self.public()
                self.assert_evidence(result["fan"], physical)
                self.assertEqual(result["learning"]["active"], {})
                if changes != dict(fault=None):
                    self.assertEqual(result["requested_level"], 16)
        self.state.update(fan=matched_fan(16), decision=dict(level=15))
        self.state["motor_schedule"].fault = True
        result = await self.public()
        self.assertEqual(result["requested_level"], 16)
        self.assert_evidence(result["fan"], deepcopy(self.state["fan"]))
        self.assertEqual(result["learning"]["active"], {})

    async def test_monitor_projects_feedback_before_decide_and_does_not_sanitize_bad_faults(self):
        self.state.update(fan=matched_fan(15), decision=dict(level=15, execution_trial=execution(15)))
        physical = deepcopy(self.state["fan"])
        with patch.object(self.state["controller"], "decide", wraps=self.state["controller"].decide) as decide:
            row = await self.sample()
        self.assertEqual(decide.call_args.kwargs["feedback"], dict(physical, confirmed=False, execution="pending"))
        self.assertIsNot(decide.call_args.kwargs["feedback"], self.state["fan"])
        self.assertEqual(self.state["fan"], physical)
        for fault in (None, "unknown", True):
            self.state.update(fan=dict(matched_fan(16), fault=fault),
                              decision=dict(level=16), last_poll=self.mono)
            with patch.object(self.state["controller"], "decide", return_value=dict(level=16)) as decide:
                await self.sample()
            self.assertEqual(decide.call_args.kwargs["feedback"]["fault"], fault)

    async def test_monitor_new_intent_blocks_row_policy_and_learner_but_keeps_valid_decision_input(self):
        policy = self.install_policy()
        self.state["decision"] = dict(level=16, execution_trial=None)
        physical = deepcopy(self.state["fan"])
        new = dict(level=15, execution_trial=execution(15), scenario="steady", learning_eligible=True,
                   trend_complete=dict.fromkeys(service.HOSTS, True),
                   rise_c_per_min=dict.fromkeys(service.HOSTS, 0))
        with patch.object(self.state["controller"], "decide", return_value=deepcopy(new)) as decide:
            row = await self.sample()
        self.assertEqual(decide.call_args.kwargs["power_thresholds"], policy)
        self.assertEqual(decide.call_args.kwargs["feedback"], physical)
        self.assertEqual(row["requested_level"], 15)
        self.assertEqual(row["active_power_policy"], {})
        self.assert_evidence(row["fan"], physical)
        self.assertTrue(row["learning"]["healthy"])
        learner = Learning(service.HOSTS)
        learner.observe(row)
        self.assertEqual(learner.eligibility["reason"], "motor")
        self.assertTrue(all(bucket["n"] == 0 and bucket["seconds"] == 0 for bucket in learner.buckets.values()))
        self.state["journal"].append(row)
        stored = json.loads(self.state["journal"].files()[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(stored, row)
        self.assertEqual((await self.public())["learning"]["active"], {})
        self.assertEqual(self.state["learning"]["published"], policy)
        self.mono += .1
        self.state["fan"] = matched_fan(15, self.mono)
        self.assertEqual((await self.public())["learning"]["active"], policy)

    async def test_monitor_current_pending_policy_is_blocked_before_decide(self):
        self.install_policy()
        self.state.update(fan=matched_fan(15), decision=dict(level=15, execution_trial=execution(15)))
        with patch.object(self.state["controller"], "decide", return_value=deepcopy(self.state["decision"])) as decide:
            row = await self.sample()
        self.assertEqual(decide.call_args.kwargs["power_thresholds"], {})
        self.assertEqual(row["active_power_policy"], {})
        self.assertEqual(row["requested_level"], 15)
        self.assertIs(row["fan"]["confirmed"], False)
        self.assertEqual(row["fan"]["level"], row["requested_level"])
        self.assertTrue(row["learning"]["healthy"])
        learner = Learning(service.HOSTS)
        learner.observe(row)
        self.assertEqual(learner.eligibility["reason"], "motor")
        self.assertTrue(all(bucket["n"] == 0 and bucket["seconds"] == 0 for bucket in learner.buckets.values()))

    async def test_monitor_real_core_requests_preserve_episode_deadline_and_accept_only_new_readback(self):
        controller = self.state["controller"]
        for tick in range(-30, 31, 3):
            self.mono, self.wall = 100 + tick, 1000 + tick
            self.state.update(fan=matched_fan(16, self.mono - .1, self.wall - .1), last_fan=self.mono)
            row = await self.sample()
        self.assertEqual(row["requested_level"], 15)
        self.assertEqual(row["fan"]["execution"], "pending")
        self.assertIs(row["fan"]["confirmed"], False)
        self.assertEqual(row["decision"]["execution_trial"]["original_deadline"], 190)
        self.mono, self.wall = 133, 1033
        row = await self.sample()
        self.assertEqual(row["requested_level"], 15)
        self.assertEqual(row["decision"]["execution_trial"]["original_deadline"], 190)
        self.state.update(fan=matched_fan(15, 133, 1033), last_fan=133)
        self.mono, self.wall = 136, 1036
        row = await self.sample()
        self.assertEqual(row["fan"]["execution"], "matched")
        self.assertEqual(row["decision"]["execution_trial"]["status"], "confirmed")
        self.assertEqual(row["decision"]["execution_trial"]["original_deadline"], 190)
        self.assertEqual(controller.level, 15)


if __name__ == "__main__":
    unittest.main()
