import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet
import test_window_policy


class ReboundEnvelopeTests(unittest.TestCase):
    def test_holds_cannot_keep_low_gear_after_authority_loss(self):
        for change in ('source', 'load', 'power', 'gear', 'impulse_source'):
            with self.subTest(change=change):
                controller, actual, requested = Controller(HOSTS), 16, None
                impulse = change == 'impulse_source'
                rate, stop = (0, 120) if impulse else (.01, 180)
                for now in range(0, 1000, 3):
                    age = 0 if requested is None else now - requested - 3
                    decision = controller.decide(fleet(now, cpu=51 + rate * max(0, age)), now,
                                                 minimum_level=9, feedback=feedback(now, actual))
                    actual = decision['level']
                    if requested is None and actual == 9:
                        requested = now
                    if requested is not None and age == stop:
                        break
                self.assertEqual(decision['trial']['outcome'], 'success')
                invalidated_frames = 0
                for elapsed in range(3, 43, 3):
                    stamp = now + elapsed
                    nodes = fleet(stamp, cpu=54.2 if impulse else 51 + rate * (stop + elapsed))
                    if change in ('source', 'impulse_source'):
                        nodes[0]['power_source'] = 'changed-source'
                    elif change in ('load', 'power'):
                        for node in nodes:
                            node['cpu_load' if change == 'load' else 'power_w'] = 25 if change == 'load' else 45
                    decision = controller.decide(nodes, stamp, minimum_level=9,
                                                 feedback=feedback(stamp, 5 if change == 'gear' else actual))
                    actual = decision['level']
                    if decision['trial']['outcome'] == 'invalidated':
                        invalidated_frames += 1
                        self.assertFalse(decision['learning_eligible'])
                        self.assertFalse(decision['retry_blocked'])
                        self.assertGreaterEqual(actual, 10)
                self.assertGreaterEqual(invalidated_frames, 3)

    def test_unexpected_low_readback_after_release_keeps_recovery_floor(self):
        for impulse in (False, True):
            with self.subTest(impulse=impulse):
                controller, actual = Controller(HOSTS), 16
                for now in range(0, 904, 3):
                    decision = controller.decide(fleet(now, cpu=46 + .01 * now), now,
                                                 minimum_level=0, feedback=feedback(now, actual))
                    actual = decision['level']
                self.assertEqual(actual, 10)
                self.assertEqual(decision['trial']['outcome'], 'recovery_released')
                for now in (906, 909, 912):
                    decision = controller.decide(fleet(now, cpu=55.5 if impulse else 46 + .01 * now), now,
                                                 minimum_level=0, feedback=feedback(now, 5))
                    self.assertGreaterEqual(decision['level'], 10)
                    self.assertEqual(decision['trial']['outcome'], 'invalidated')
                    self.assertFalse(decision['learning_eligible'])
                    self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_warming_completes_recovery_to_ten_but_blocks_low_gear_probe(self):
        for rate in (.01, .012):
            for delay in (3, 45):
                with self.subTest(rate=rate, delay=delay):
                    controller, actual, pending = Controller(HOSTS), 16, None
                    landed = False
                    for now in range(0, 903, 3):
                        if pending and now - pending[0] >= delay:
                            actual, pending = pending[1], None
                        decision = controller.decide(fleet(now, cpu=46 + rate * now), now,
                                                     minimum_level=0, feedback=feedback(now, actual))
                        if actual == 10:
                            landed = True
                            self.assertEqual(decision['trial']['kind'], 'release')
                            self.assertEqual(decision['trial']['outcome'], 'recovery_released')
                            self.assertEqual(decision['level'], 10)
                        self.assertGreaterEqual(decision['level'], 10)
                        if decision['level'] >= actual:
                            actual, pending = decision['level'], None
                        elif pending is None:
                            pending = (now, decision['level'])
                    self.assertTrue(landed)
                    self.assertEqual(actual, 10)
                    hot = controller.decide(fleet(now + 3, cpu=73), now + 3,
                                            minimum_level=0, feedback=feedback(now + 3, actual))
                    self.assertEqual(hot['level'], 16)

    def test_rebound_above_two_degrees_can_settle_without_failure(self):
        for quantized in (False, True):
            with self.subTest(quantized=quantized):
                controller, requested = test_window_policy.WindowPolicyTests().pending()
                successes = []
                for age in range(0, 300, 3):
                    now = requested + 3 + age
                    temp = 51 + min(age * .025, 2.3) + (.2 * ((age // 3) % 2) if quantized else 0)
                    decision = controller.decide(fleet(now, cpu=temp), now, minimum_level=9,
                                                 feedback=feedback(now, 9))
                    self.assertEqual(decision['level'], 9, decision['scenario_detail'])
                    self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
                    if decision['trial']['outcome'] == 'success':
                        successes.append(age)
                self.assertTrue(successes)
                self.assertGreaterEqual(successes[0], 120)

    def test_detectable_continuing_heat_cannot_keep_descending_after_success(self):
        for rate in (.01, .012):
            with self.subTest(rate=rate):
                controller, requested = test_window_policy.WindowPolicyTests().pending(minimum=0)
                actual = 9
                for age in range(0, 1000, 3):
                    now = requested + 3 + age
                    temp = 51 + age * rate
                    decision = controller.decide(fleet(now, cpu=temp), now, minimum_level=0,
                                                 feedback=feedback(now, actual))
                    self.assertGreaterEqual(decision['level'], 9, decision['scenario_detail'])
                    actual = decision['level']
                    if actual >= 10:
                        break
                self.assertGreaterEqual(actual, 10)
                self.assertLess(temp, 60)

    def test_active_unsettled_rise_still_has_bounded_observation(self):
        controller, requested = test_window_policy.WindowPolicyTests().pending()
        for age in range(0, 304, 3):
            now = requested + 3 + age
            decision = controller.decide(fleet(now, cpu=51 + age * .025), now,
                                         minimum_level=9, feedback=feedback(now, 9))
            self.assertNotEqual(decision['trial']['outcome'], 'success')
            if decision['level'] >= 10:
                break
        self.assertGreaterEqual(decision['level'], 10)
        self.assertLessEqual(age, 300)

    def test_sub_detection_drift_still_hits_fixed_absolute_envelope(self):
        controller, requested = test_window_policy.WindowPolicyTests().pending(minimum=0)
        actual = 9
        for age in range(0, 2600, 3):
            now = requested + 3 + age
            temp = 51 + .004 * age
            decision = controller.decide(fleet(now, cpu=temp), now, minimum_level=0,
                                         feedback=feedback(now, actual))
            actual = decision['level']
            if actual >= 10:
                break
        self.assertGreaterEqual(actual, 10)
        self.assertLess(temp, 60)


if __name__ == '__main__':
    unittest.main()
