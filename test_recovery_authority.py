import unittest

import test_background_probe
import test_core
from test_scenarios import feedback, heat_to_target_risk


class RecoveryAuthorityTests(unittest.TestCase):
    def test_completed_invalidated_probe_does_not_poison_new_wifi_recovery(self):
        controller, requested = test_background_probe.BackgroundProbeTests().pending(wifi=True)
        fixture = test_core.ControlTests()
        now = requested + 3
        controller.decide(fixture.sensor_fleet(now), now, minimum_level=0,
                          feedback=feedback(now, 9))
        for age in (6, 9, 12):
            now = requested + age
            nodes = fixture.sensor_fleet(now)
            for node in nodes:
                node['cpu_load'] = 35
            decision = controller.decide(nodes, now, minimum_level=0,
                                         feedback=feedback(now, 9))
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        now = requested + 15
        controller.decide(fixture.sensor_fleet(now), now, minimum_level=0,
                          feedback=feedback(now, 16))
        now = requested + 18
        decision = controller.decide(fixture.sensor_fleet(now, ()), now, minimum_level=0,
                                     feedback=feedback(now, 16))
        self.assertNotEqual(decision['scenario'], 'protect', decision['reasons'])
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_failed_probe_requires_new_safe_wifi_observation(self):
        controller, requested = test_background_probe.BackgroundProbeTests().pending(wifi=True)
        fixture = test_core.ControlTests()
        now, decision = heat_to_target_risk(controller, requested, fixture.sensor_fleet)
        actual = decision['level']
        self.assertGreaterEqual(actual, 10)
        self.assertEqual(decision['trial']['outcome'], 'failed')
        now += 3
        controller.decide(fixture.sensor_fleet(now), now, minimum_level=0,
                          feedback=feedback(now, actual))
        now += 3
        decision = controller.decide(fixture.sensor_fleet(now, ()), now, minimum_level=0,
                                     feedback=feedback(now, actual))
        self.assertNotEqual(decision['scenario'], 'protect', decision['reasons'])
        self.assertEqual(decision['level'], actual)
        self.assertEqual(decision['trial']['outcome'], 'failed')


if __name__ == '__main__':
    unittest.main()
