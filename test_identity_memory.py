import unittest
from copy import deepcopy

from core import Controller, normalize
from test_core import HOSTS
from test_scenarios import feedback


ABSENT = object()


class IdentityMemoryTests(unittest.TestCase):
    @staticmethod
    def nodes(now, hostname="DGX", cpu=49, load=25, power=70, hostnames=None):
        nodes = []
        for index, host in enumerate(HOSTS):
            raw = {
                "hostname": "DGX", "timestamp": now,
                "cpu_percent": load, "gpu_percent": load, "system_power_w": power,
                "system_power_source": "spbm" if index < 2 else "estimate:gpu",
                "temperatures": {
                    "gpu": 47,
                    "acpi": [{"zone": "0", "type": "acpitz", "temp": cpu}],
                    "mlx": [{"label": "asic", "temp": 54, "chip": "mlx5"}],
                },
            }
            name = (hostnames or {}).get(index, hostname if index == 0 else "DGX")
            if name is ABSENT:
                raw.pop("hostname")
            else:
                raw["hostname"] = name
            nodes.append(normalize(host, raw, now, now))
        return nodes

    def step(self, controller, now, actual=10, **values):
        return controller.decide(self.nodes(now, **values), now,
            minimum_level=0, feedback=feedback(now, actual))

    def failed_controller(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        for now in range(0, 214, 3):
            decision = self.step(controller, now, actual)
            actual = decision["level"]
        self.assertEqual(actual, 9)
        decision = self.step(controller, 216, actual, cpu=58)
        self.assertTrue(decision["retry_blocked"])
        self.assertEqual(decision["operating"]["failed_floor"]["failed_level"], 9)
        actual = decision["level"]
        for now in range(219, 307, 3):
            decision = self.step(controller, now, actual)
            actual = decision["level"]
        self.assertEqual(actual, 10)
        return controller, actual, deepcopy(decision["operating"]["failed_floor"])

    def recover_to_ten(self, controller, start, actual, **values):
        for now in range(start, start + 241, 3):
            decision = self.step(controller, now, actual, **values)
            self.assertEqual(decision['operating']['observe_credit'], 0)
            if actual > 10:
                self.assertIn(decision['level'], (actual, actual - 1))
            actual = decision['level']
            if controller.operating.confirmed_level == 10 and not controller._pending():
                return now, actual
        self.fail('fresh adjacent recovery did not reach confirmed ten')

    def require_fresh_ordinary_window(self, controller, confirmed_at):
        for now in range(confirmed_at + 3, confirmed_at + 214, 3):
            decision = self.step(controller, now)
            if decision['level'] == 9:
                self.assertGreaterEqual(now - confirmed_at, 180)
                return
            self.assertEqual(decision['level'], 10)
        self.fail('ordinary safe observations did not earn descent')

    def test_unverified_hostname_and_original_identity_recovery_keep_failed_floor(self):
        for hostname in (ABSENT, "", " \t ", None, 17, True, [], {"name": "DGX"},
                         "DGX" + "x" * 78, "DGX\nreplacement", "DGX\x00", "DGX\x7f"):
            with self.subTest(hostname=hostname):
                controller, actual, failure = self.failed_controller()
                decision = self.step(controller, 309, actual, hostname=hostname)
                self.assertTrue(decision["retry_blocked"])
                self.assertEqual(decision["operating"]["failed_floor"], failure)
                for now in range(312, 1000, 3):
                    decision = self.step(controller, now, actual)
                    self.assertEqual(decision["level"], 10)
                    self.assertTrue(decision["retry_blocked"])
                    self.assertEqual(decision["operating"]["failed_floor"], failure)
                    actual = decision["level"]

    def test_display_fallback_does_not_manufacture_affirmative_hostname(self):
        for hostname, display in ((ABSENT, HOSTS[0]), (None, "None"), ("", ""),
                                  (" \t ", " \t "), (17, "17"), (True, "True"),
                                  ([], "[]"), ({"name": "DGX"}, "{'name': 'DGX'}")):
            with self.subTest(hostname=hostname):
                sample = self.nodes(0, hostname=hostname)[0]
                self.assertEqual(sample["hostname"], display)
                self.assertIs(sample["hostname_verified"], False)

    def test_nonblank_valid_strings_are_verified_without_placeholder_blacklists(self):
        for hostname in ("DGX", "replacement-DGX", "DGX.example", " DGX ",
                         "None", HOSTS[0], "D" * 80):
            with self.subTest(hostname=hostname):
                sample = self.nodes(0, hostname=hostname)[0]
                self.assertEqual(sample["hostname"], hostname)
                self.assertIs(sample["hostname_verified"], True)

    def test_verified_replacement_preserves_other_hosts_failures(self):
        controller, actual, _ = self.failed_controller()
        decision = self.step(controller, 309, actual, hostname="replacement-DGX")
        self.assertTrue(decision["retry_blocked"])
        decision = self.step(controller, 312, actual, hostname="replacement-DGX")
        self.assertNotEqual(decision['operating']['failed_floor']['witness']['source'][0], HOSTS[0])
        self.assertEqual(decision["operating"]["observe_credit"], 0)
        for now in range(315, 492, 3):
            decision = self.step(controller, now, actual, hostname="replacement-DGX")
            self.assertEqual(decision["level"], 10)
        self.assertEqual(self.step(controller, 492, actual, hostname="replacement-DGX")["level"], 10)

    def test_replacement_waits_for_every_hostname_to_be_verified(self):
        for missing_index in range(1, len(HOSTS)):
            with self.subTest(missing_index=missing_index):
                controller, actual, failure = self.failed_controller()
                for now in range(309, 372, 3):
                    decision = self.step(controller, now, actual, hostname="replacement-DGX",
                        hostnames={missing_index: ABSENT})
                    self.assertTrue(decision["retry_blocked"])
                    self.assertEqual(decision["operating"]["failed_floor"], failure)
                    self.assertEqual(decision["operating"]["observe_credit"], 0)
                decision = self.step(controller, 372, actual, hostname="replacement-DGX")
                self.assertTrue(decision["retry_blocked"])
                self.assertEqual(decision["operating"]["observe_credit"], 0)

    def test_missing_hostname_cannot_earn_ordinary_descent(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        actual = 10
        for now in range(0, 604, 3):
            decision = self.step(controller, now, actual, hostname=ABSENT)
            self.assertGreaterEqual(decision['level'], actual)
            self.assertEqual(decision['high_wind']['exception'], 'SourceFaultProtection')
            self.assertEqual(decision["operating"]["observe_credit"], 0)
            actual = decision['level']
        confirmed_at, _ = self.recover_to_ten(controller, 606, actual)
        self.require_fresh_ordinary_window(controller, confirmed_at)

    def test_missing_hostname_discards_previous_ordinary_descent_credit(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 181, 3):
            decision = self.step(controller, now)
        self.assertEqual(decision["operating"]["observe_credit"], 150)
        decision = self.step(controller, 183, hostname=ABSENT)
        self.assertEqual(decision["operating"]["observe_credit"], 0)
        self.assertEqual(decision['high_wind']['exception'], 'SourceFaultProtection')
        confirmed_at, _ = self.recover_to_ten(controller, 186, decision['level'])
        self.require_fresh_ordinary_window(controller, confirmed_at)

    def test_missing_hostname_interrupts_failure_improvement_qualification(self):
        controller, actual, failure = self.failed_controller()
        for now in range(309, 345, 3):
            decision = self.step(controller, now, actual, load=10, power=56)
            self.assertTrue(decision["retry_blocked"])
        decision = self.step(controller, 345, actual, hostname=ABSENT, load=10, power=56)
        self.assertTrue(decision["retry_blocked"])
        self.assertEqual(decision["operating"]["failed_floor"], failure)
        self.assertEqual(controller.operating.failure_credit, 0)
        confirmed_at, actual = self.recover_to_ten(controller, 348, decision['level'], load=10, power=56)
        for now in range(confirmed_at + 3, confirmed_at + 94, 3):
            decision = self.step(controller, now, actual, load=10, power=56)
            if now < confirmed_at + 60:
                self.assertTrue(decision['retry_blocked'])
            if not decision['retry_blocked']:
                self.assertGreaterEqual(now - confirmed_at, 60)
                break
        self.assertFalse(decision["retry_blocked"])
        self.assertEqual(decision["operating"]["observe_credit"], 0)

    def test_missing_hostname_during_pending_descent_cancels_and_restarts_observation(self):
        controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
        for now in range(0, 211, 3):
            decision = self.step(controller, now)
        self.assertEqual(decision["level"], 9)
        self.assertEqual(decision["execution_trial"]["status"], "pending")
        deadline = decision['execution_trial']['original_deadline']
        decision = self.step(controller, 213, hostname=ABSENT)
        self.assertEqual(decision["level"], 10)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertEqual(decision['execution_trial']['status'], 'invalidated')
        self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
        self.assertEqual(decision["operating"]["observe_credit"], 0)
        confirmed_at, _ = self.recover_to_ten(controller, 216, decision['level'])
        self.require_fresh_ordinary_window(controller, confirmed_at)

    def test_missing_hostname_does_not_mask_current_eighty_degree_protection(self):
        for hostname in (ABSENT, None, "", " \t "):
            with self.subTest(hostname=hostname):
                controller = Controller(HOSTS, rules_only=True, high_wind_gate=True)
                decision = self.step(controller, 100, actual=2, hostname=hostname, cpu=80)
                self.assertEqual(decision["high_wind_limit"], 16)
                self.assertEqual(decision["demand"], 10)
                self.assertEqual(decision["level"], 4)
                self.assertIsNone(decision['high_wind']['exception'])
                self.assertEqual(self.step(controller, 112, actual=10, hostname=hostname, cpu=80)['level'], 11)

    def test_missing_hostname_and_unknown_motor_feedback_keep_failure_protection(self):
        controller, _, failure = self.failed_controller()
        decision = controller.decide(self.nodes(309, hostname=ABSENT), 309, minimum_level=0,
            feedback=feedback(309, None, confirmed=False, execution="pending", rpm=None))
        self.assertEqual(decision['control_status']['primary_reason'], 'ceiling_exit_fresh_endpoint')
        self.assertEqual(decision['high_wind']['exception'], 'SourceFaultProtection')
        self.assertIsNone(decision['actual_level'])
        self.assertEqual(decision["level"], 10)
        self.assertTrue(decision["retry_blocked"])
        self.assertEqual(decision["operating"]["failed_floor"], failure)

    def test_malformed_or_truncated_hostname_is_not_verified(self):
        for hostname in ("DGX" + "x" * 78, "DGX\nreplacement", "DGX\x00", "DGX\x7f", "DGX\x85"):
            with self.subTest(hostname=hostname):
                sample = self.nodes(0, hostname=hostname)[0]
                self.assertIs(sample["hostname_verified"], False)


if __name__ == "__main__":
    unittest.main()
