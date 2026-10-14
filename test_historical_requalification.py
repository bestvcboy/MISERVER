import unittest

import test_core
from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class HistoricalRequalificationTests(unittest.TestCase):
    def reach(self, target, make_nodes=fleet):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1801, 3):
            decision = controller.decide(make_nodes(now), now, minimum_level=0,
                                         feedback=feedback(now, actual))
            if actual == target and decision['trial'] and decision['trial'].get('outcome') in (
                    'success', 'recovery_released'):
                return controller, now
            actual = decision['level']
        self.fail(f'no confirmed stable {target}')

    def retry(self, controller, now, actual, make_nodes=fleet, target=9):
        for stamp in range(now + 3, now + 1203, 3):
            decision = controller.decide(make_nodes(stamp), stamp, minimum_level=0,
                                         feedback=feedback(stamp, actual))
            actual = decision['level']
            trial = decision['trial']
            if trial and trial['to'] == target and trial['status'] == 'pending':
                return stamp
        self.assertEqual(actual, target, 'history veto survives complete safe current-gear evidence')
        self.fail('no new adjacent request')

    def test_real_heat_failure_can_requalify_after_safe_recovery(self):
        controller, now = self.reach(8)
        trigger = now + 3
        decision = controller.decide(fleet(trigger, cpu=73), trigger, minimum_level=0,
                                     feedback=feedback(trigger, 8))
        self.assertEqual(decision['level'], 16)
        self.assertEqual(decision['trial']['outcome'], 'failed')
        self.assertEqual((decision['trial']['from'], decision['trial']['to']), (9, 8))
        self.retry(controller, trigger, 16, target=8)

    def test_sampling_timeout_has_no_lifetime_retry_count(self):
        fixture = test_core.ControlTests()
        make_nodes = fixture.sensor_fleet
        controller, now = self.reach(10, make_nodes)
        requested = self.retry(controller, now, 10, make_nodes)
        for episode in range(2):
            for age in range(3, 304, 3):
                stamp = requested + age
                readings = (('wifi', 50),) if age == 3 or age % 15 == 0 else ()
                decision = controller.decide(make_nodes(stamp, readings), stamp, minimum_level=0,
                                             feedback=feedback(stamp, 9))
            self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
            self.assertEqual(decision['level'], 10)
            with self.subTest(episode=episode + 1):
                requested = self.retry(controller, stamp, 10, make_nodes)

    def test_resolved_nonsettlement_can_requalify_without_fake_success(self):
        controller, now = self.reach(10)
        requested = self.retry(controller, now, 10)
        for age in range(3, 304, 3):
            stamp = requested + age
            decision = controller.decide(fleet(stamp, cpu=45 + .018 * age), stamp,
                                         minimum_level=0, feedback=feedback(stamp, 9))
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')
        self.assertEqual(decision['level'], 10)
        baseline = 45 + .018 * 303
        self.retry(controller, stamp, 10, lambda tick: fleet(tick, cpu=baseline))


if __name__ == '__main__':
    unittest.main()
