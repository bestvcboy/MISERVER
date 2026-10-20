import unittest
from dataclasses import replace
from unittest.mock import patch

import service
from core import Controller, HighWindAuthority, MotionAuthority, SourceFaultProtection
from test_core import HOSTS
from test_scenarios import feedback, fleet
from test_service import FakeFan


WITNESS = (HOSTS[0], "DGX", "acpi:0:acpitz")


class MeasuredAuthorityTests(unittest.TestCase):
    def test_old_positional_evidence_without_temperature_fails_closed(self):
        authority = HighWindAuthority(True, WITNESS, 100)
        self.assertEqual((authority.allowed, authority.witness, authority.observed_mono),
                         (True, WITNESS, 100))
        self.assertFalse(authority.valid_at(100))

    def test_finite_nonboolean_evidence_and_exact_expiry(self):
        authority = HighWindAuthority(True, WITNESS, 100, 80)
        for now, expected in ((99.999, False), (100, True), (105.999, True),
                              (106, False), (106.001, False)):
            with self.subTest(now=now):
                self.assertEqual(authority.valid_at(now), expected)
        for invalid in (None, True, False, "100", float("nan"), float("inf"), -float("inf")):
            for field in ("actual_c", "observed_mono"):
                with self.subTest(field=field, invalid=invalid):
                    self.assertFalse(replace(authority, **{field: invalid}).valid_at(100))
            with self.subTest(now=invalid):
                self.assertFalse(authority.valid_at(invalid))
        self.assertFalse(replace(authority, actual_c=74.999).valid_at(100))
        self.assertFalse(replace(authority, allowed=False).valid_at(100))

    def test_witness_requires_host_hostname_and_sensor(self):
        for witness in (None, (), ("host", "sensor"), ("", "DGX", "cpu"),
                        (HOSTS[0], "", "cpu"), (HOSTS[0], "DGX", None),
                        (HOSTS[0], "DGX", " "), [HOSTS[0], "DGX", "cpu"]):
            with self.subTest(witness=witness):
                self.assertFalse(HighWindAuthority(True, witness, 100, 80).valid_at(100))

    def test_controller_and_worker_reject_identical_invalid_evidence(self):
        controller = Controller(HOSTS, high_wind_gate=True)
        valid = HighWindAuthority(True, WITNESS, 100, 80)
        for authority in (HighWindAuthority(True, WITNESS, 100),
                          replace(valid, actual_c=74.999), replace(valid, actual_c=float("inf")),
                          replace(valid, observed_mono=True), replace(valid, witness=None),
                          replace(valid, observed_mono=94), replace(valid, observed_mono=101)):
            with self.subTest(authority=authority):
                controller.high_wind = authority
                self.assertEqual(controller.effective_limit(100), 10)
                device = FakeFan(["on", "favorite", 10, 900])
                with patch("service.time.monotonic", return_value=100):
                    result = service.fan_apply(device, lambda: service.FanRequest(16, high_wind=authority),
                                               service.MotorSchedule())
                self.assertEqual((device.writes, result["level"]), ([], 10))


class SourceTrustTests(unittest.TestCase):
    def decide(self, controller, nodes, now):
        return controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 10))

    def test_rejected_current_source_revokes_previous_hot_evidence(self):
        for rejection in ("repeat", "regress", "stale", "future", "offline", "ambiguous", "uncalibrated"):
            with self.subTest(rejection=rejection):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                self.assertEqual(self.decide(controller, fleet(100, cpu=82), 100)["high_wind_limit"], 16)
                nodes = fleet(103, cpu=82)
                for node in nodes:
                    if rejection == "repeat":
                        node["source_ts"] = 100
                    elif rejection == "regress":
                        node["source_ts"] = 99
                    elif rejection == "stale":
                        node["source_ts"] = 96
                    elif rejection == "future":
                        node["source_ts"] = 106
                    elif rejection == "offline":
                        node["online"] = False
                    elif rejection == "ambiguous":
                        node["ambiguous_sensors"] = True
                    else:
                        for sensor in node["sensors"]:
                            sensor["calibrated"] = False
                decision = self.decide(controller, nodes, 103)
                self.assertEqual(decision["high_wind_limit"], 10)
                self.assertIsInstance(controller.high_wind.proof, SourceFaultProtection)
                self.assertIsNone(controller.high_wind.actual_c)
                self.assertEqual(controller.high_wind.normal_ceiling_at(103), 10)

    def test_untrusted_host_or_missing_witness_identity_never_authorizes(self):
        for rejection in ("unknown_host", "duplicate_host", "hostname", "sensor_id"):
            with self.subTest(rejection=rejection):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                nodes = fleet(100, cpu=43)
                hot = fleet(100, cpu=82)[0]
                nodes[0] = hot
                if rejection == "unknown_host":
                    hot["host"] = "unconfigured-host"
                elif rejection == "duplicate_host":
                    nodes.append(dict(hot))
                elif rejection == "hostname":
                    hot["hostname"] = ""
                else:
                    hot["sensors"][0]["id"] = ""
                self.assertEqual(self.decide(controller, nodes, 100)["high_wind_limit"], 10)

    def test_current_identity_is_carried_without_a_three_sample_chain(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        self.decide(controller, fleet(100, cpu=43), 100)
        nodes = fleet(103, cpu=43)
        hot = fleet(103, cpu=80)[0]
        hot["hostname"] = "replacement-DGX"
        hot["sensors"][0]["id"] = "replacement-cpu"
        nodes[0] = hot
        self.assertEqual(self.decide(controller, nodes, 103)["high_wind_limit"], 16)
        self.assertEqual(controller.high_wind.witness, (HOSTS[0], "replacement-DGX", "replacement-cpu"))
        self.assertEqual(controller.high_wind.actual_c, 80)

    def test_independent_hot_source_survives_unrelated_loss_in_either_order(self):
        for reverse in (False, True):
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            nodes = fleet(100, cpu=43)
            nodes[0] = fleet(100, cpu=80)[0]
            nodes[1]["online"] = False
            if reverse:
                nodes.reverse()
            self.assertEqual(self.decide(controller, nodes, 100)["high_wind_limit"], 16)
            self.assertEqual(controller.high_wind.witness[0], HOSTS[0])

    def test_source_age_is_deducted_from_permit_lifetime(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        nodes = fleet(100, cpu=80)
        for node in nodes:
            node["source_ts"] = 97
        self.assertEqual(self.decide(controller, nodes, 100)["high_wind_limit"], 16)
        self.assertEqual(controller.high_wind.observed_mono, 96.5)
        self.assertEqual(controller.effective_limit(102.499999), 16)
        self.assertEqual(controller.effective_limit(102.5), 10)


class WriteTimeAuthorityTests(unittest.TestCase):
    def test_latest_callback_revokes_before_write_and_cannot_reexpand_ceiling(self):
        for later_hot in (False, True):
            controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
            controller.decide(fleet(100, cpu=80), 100, feedback=feedback(100, 2))
            device = FakeFan(["on", "favorite", 2, 900])
            schedule = service.MotorSchedule()
            persist = schedule.persist_debt_then_set_budget
            reservations = []
            def reserve(*args):
                reservations.append(args[1])
                if len(reservations) == 1:
                    controller.decide(fleet(103, cpu=74), 103, feedback=feedback(103, 2))
                elif later_hot:
                    controller.decide(fleet(104, cpu=80), 104, feedback=feedback(104, 2))
                return persist(*args)
            with patch("service.time.monotonic", return_value=104), \
                    patch.object(schedule, "persist_debt_then_set_budget", side_effect=reserve):
                service.fan_apply(device, lambda: service.FanRequest(16, protected=True,
                    high_wind=controller.high_wind, motion=MotionAuthority(104, WITNESS, 80, 75)), schedule)
            self.assertEqual(reservations[0], 10)
            self.assertTrue(all(level is None or level <= 10 for level in reservations))
            self.assertEqual(device.writes, [10])

    def test_exact_expiry_during_reservation_clamps_before_write(self):
        for expired_at in (105.999, 106):
            device = FakeFan(["on", "favorite", 2, 900])
            schedule = service.MotorSchedule()
            clock = [100]
            persist = schedule.persist_debt_then_set_budget
            def reserve(*args):
                clock[0] = expired_at
                return persist(*args)
            with patch("service.time.monotonic", side_effect=lambda: clock[0]), \
                    patch.object(schedule, "persist_debt_then_set_budget", side_effect=reserve):
                service.fan_apply(device, lambda: service.FanRequest(16, protected=True,
                    high_wind=HighWindAuthority(True, WITNESS, 100, 80),
                    motion=MotionAuthority(clock[0], WITNESS, 80, 75)), schedule)
            self.assertEqual(device.writes, [16 if expired_at < 106 else 10])


if __name__ == "__main__":
    unittest.main()
