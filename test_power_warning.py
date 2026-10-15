import unittest

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet


class PowerWarningTests(unittest.TestCase):
    def step(self, controller, now, actual, power=60, host=0, nodes=None, **options):
        samples = fleet(now, power=60) if nodes is None else nodes
        samples[host]['power_w'] = power
        return controller.decide(samples, now, minimum_level=0,
                                 feedback=options.pop('readback', feedback(now, actual)), **options)

    def reach(self, level=9, cpu=51, power=60):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 2401, 3):
            decision = controller.decide(fleet(now, cpu=cpu, power=power), now, minimum_level=0,
                                         feedback=feedback(now, actual))
            if actual == level and decision['trial'] and decision['trial']['status'] in (
                    'observing', 'recovery_released') and decision['level'] == level:
                return controller, now
            actual = decision['level']
        self.fail('public controller timeline did not reach requested level')

    def test_isolated_80_point_04_at_93_seconds_keeps_probe_and_resume_credit(self):
        controller, start = self.reach()
        for age in range(3, 93, 3):
            self.step(controller, start + age, 9, power=60)
        trial, anchor = controller.trial, controller.search_anchor
        deadline, earned = trial['deadline'], controller._policy['state']['earned']
        decision = self.step(controller, start + 93, 9, power=80.04)
        self.assertEqual((decision['level'], decision['scenario'], decision['trial']['status']),
                         (9, 'power_warning', 'observing'))
        self.assertIs(controller.trial, trial)
        self.assertIs(controller.search_anchor, anchor)
        self.assertEqual((trial['deadline'], controller._policy['state']['tag'],
                          controller._policy['state']['earned']), (deadline, 'probe_observe', earned))
        self.assertFalse(decision['learning_eligible'])
        self.step(controller, start + 96, 9, power=60)
        self.assertEqual(controller._policy['state']['earned'], earned)
        self.step(controller, start + 99, 9, power=60)
        self.assertEqual(controller._policy['state']['earned'], earned + 3)
        self.assertEqual(controller.trial['deadline'], deadline)

    def test_sustained_three_samples_over_six_seconds_requests_reserve(self):
        controller, start = self.reach()
        for age in (3, 6):
            decision = self.step(controller, start + age, 9, power=80)
            self.assertEqual((decision['level'], decision['scenario']), (9, 'power_warning'))
        decision = self.step(controller, start + 9, 9, power=80)
        self.assertEqual((decision['level'], decision['trial']['outcome']), (10, 'invalidated'))
        self.assertIn('连续至少3样本跨度=6/6s', ' '.join(decision['reasons']))

    def test_fast_cadence_waits_for_six_seconds_without_discarding_exposure(self):
        controller, start = self.reach()
        for age in range(1, 7):
            decision = self.step(controller, start + age, 9, power=80)
            self.assertEqual((decision['level'], decision['scenario']), (9, 'power_warning'))
        decision = self.step(controller, start + 7, 9, power=80)
        self.assertEqual(decision['level'], 10)

    def test_alternating_hosts_do_not_pool_samples(self):
        controller, start = self.reach()
        for age, host in ((3, 0), (6, 1), (9, 0), (12, 1), (15, 0)):
            decision = self.step(controller, start + age, 9, power=80, host=host)
            self.assertEqual((decision['level'], decision['scenario']), (9, 'power_warning'))

    def test_identity_and_source_changes_restart_exposure(self):
        for change in ('hostname', 'power_source', 'sensor'):
            with self.subTest(change=change):
                controller, start = self.reach()
                self.step(controller, start + 3, 9, power=80)
                samples = fleet(start + 6)
                if change == 'sensor':
                    samples[0]['sensors'].append(dict(id='new:acpi', group='cpu', label='extra',
                                                     temp=51, calibrated=True))
                else:
                    samples[0][change] = 'changed'
                decision = self.step(controller, start + 6, 9, power=80, nodes=samples)
                self.assertIn('连续样本=1/3 跨度=0/6s', ' '.join(decision['reasons']))
                self.assertFalse(decision['learning_eligible'])

    def test_bad_sampling_clears_exposure_before_recovery(self):
        for failure in ('gap', 'duplicate', 'missing', 'offline', 'power', 'fan'):
            with self.subTest(failure=failure):
                controller, start = self.reach()
                self.step(controller, start + 3, 9, power=80)
                now = start + (12 if failure == 'gap' else 6)
                samples, options = fleet(now), {}
                if failure == 'duplicate':
                    samples[0]['source_ts'] = start + 3
                elif failure == 'missing':
                    samples.pop()
                elif failure == 'offline':
                    samples[0]['online'] = False
                elif failure == 'power':
                    samples[0]['power_source'] = '不可用'
                elif failure == 'fan':
                    options['readback'] = feedback(now, 9, fault=True)
                decision = self.step(controller, now, 9, power=80, nodes=samples, **options)
                self.assertFalse(decision['learning_eligible'])
                later = self.step(controller, now + 3, 16, power=80)
                self.assertIn('连续样本=1/3 跨度=0/6s', ' '.join(later['reasons']))

    def test_learned_threshold_including_80_remains_immediate(self):
        for threshold in (50, 80):
            with self.subTest(threshold=threshold):
                controller, start = self.reach()
                decision = self.step(controller, start + 3, 9, power=threshold,
                    power_thresholds={HOSTS[0]: dict(source='spbm', threshold_w=threshold)})
                self.assertEqual(decision['level'], 10)
                self.assertIn('已发布整机功耗阈值', ' '.join(decision['reasons']))

    def test_warning_cannot_mask_thermal_or_feedback_protection(self):
        for event in ('target', 'hard', 'fan', 'forecast'):
            with self.subTest(event=event):
                controller, start = self.reach()
                now, options = start + 3, {}
                samples = fleet(now, cpu=60 if event == 'target' else 73 if event == 'hard' else 51)
                if event == 'fan':
                    options['readback'] = feedback(now, 9, fault=True)
                if event == 'forecast':
                    for age in range(3, 94, 3):
                        now = start + age
                        samples = fleet(now, cpu=51 + age * .1)
                        decision = self.step(controller, now, 9, power=80.04 if age % 6 else 60,
                                             nodes=samples)
                        if decision['level'] >= 10:
                            break
                else:
                    decision = self.step(controller, now, 9, power=80.04, nodes=samples, **options)
                self.assertGreaterEqual(decision['level'], 10)
                self.assertNotEqual(decision['scenario'], 'power_warning')
                if event in ('hard', 'fan'):
                    self.assertEqual((decision['level'], decision['scenario']), (16, 'protect'))

    def test_pending_descent_warning_restores_original_without_extending_deadline(self):
        controller, start = self.reach(10)
        for age in range(3, 1201, 3):
            now = start + age
            decision = self.step(controller, now, 10)
            if decision['level'] == 9:
                break
        self.assertEqual(decision['trial']['status'], 'pending')
        deadline = decision['execution_trial']['original_deadline']
        requested = decision['execution_trial']['requested_at']
        decision = self.step(controller, now + 3, 9, power=80.04)
        self.assertEqual((decision['level'], decision['trial']['outcome']), (10, 'invalidated'))
        self.assertEqual((decision['execution_trial']['original_deadline'],
                          decision['execution_trial']['requested_at']), (deadline, requested))
        self.assertEqual(decision['execution_trial']['to'], 10)
        for stamp in range(int(now + 6), int(deadline + 1), 3):
            decision = self.step(controller, stamp, 9, power=60)
        self.assertEqual((decision['level'], decision['execution_trial']['outcome']),
                         (16, 'execution_timeout'))

    def test_probe_warning_never_extends_absolute_expiry(self):
        controller, start = self.reach(power=70)
        deadline = controller.trial['deadline']
        for age in range(3, 301, 3):
            decision = self.step(controller, start + age, 9, power=80.04 if age % 6 else 70,
                                 nodes=fleet(start + age, power=70))
            if start + age < deadline:
                self.assertEqual(decision['level'], 9)
                self.assertEqual(controller.trial['deadline'], deadline)
                self.assertFalse(decision['learning_eligible'])
        self.assertEqual((decision['level'], decision['trial']['outcome']), (10, 'inconclusive_timeout'))

    def test_cold_warning_holds_credit_without_lending_paused_interval(self):
        controller, start = self.reach(9, cpu=42)
        for age in (3, 6, 9):
            self.step(controller, start + age, 9, nodes=fleet(start + age, cpu=42))
        earned = controller._policy['state']['earned']
        decision = self.step(controller, start + 12, 9, power=80.04, nodes=fleet(start + 12, cpu=42))
        self.assertEqual((decision['level'], decision['scenario']), (9, 'power_warning'))
        self.assertEqual(controller._policy['state']['earned'], earned)
        self.step(controller, start + 15, 9, nodes=fleet(start + 15, cpu=42))
        self.assertEqual(controller._policy['state']['earned'], earned)
        self.step(controller, start + 18, 9, nodes=fleet(start + 18, cpu=42))
        self.assertEqual(controller._policy['state']['earned'], earned + 3)

    def test_startup_and_recovery_warning_do_not_earn_descent_credit(self):
        for phase in ('startup', 'recovery'):
            with self.subTest(phase=phase):
                controller, actual = Controller(HOSTS), 16
                if phase == 'recovery':
                    for now in range(0, 40, 3):
                        decision = self.step(controller, now, actual)
                        actual = decision['level']
                    now += 3
                    self.step(controller, now, actual)
                else:
                    for now in (0, 3, 6, 9):
                        self.step(controller, now, actual)
                before = controller._policy['state']['earned']
                tag = controller._policy['state']['tag']
                decision = self.step(controller, now + 3, actual, power=80.04)
                self.assertEqual((decision['level'], controller._policy['state']['earned'],
                                  controller._policy['state']['tag']), (actual, before, tag))
                self.assertFalse(decision['learning_eligible'])
                self.step(controller, now + 6, actual)
                self.assertEqual(controller._policy['state']['earned'], before)

    def test_ordinary_search_warning_preserves_earned_and_identity(self):
        controller, start = self.reach(10)
        for age in range(3, 91, 3):
            self.step(controller, start + age, 10)
        state, window = controller._policy['state'], controller.gear_window
        self.assertEqual(state['tag'], 'ordinary_search')
        earned = state['earned']
        decision = self.step(controller, start + 93, 10, power=80.04)
        self.assertEqual((decision['level'], decision['scenario']), (10, 'power_warning'))
        self.assertIs(controller._policy['state'], state)
        self.assertIs(controller.gear_window, window)
        self.assertEqual(state['earned'], earned)
        self.step(controller, start + 96, 10)
        self.assertEqual(state['earned'], earned)

    def test_warning_cannot_reuse_wifi_grace_for_missing_sensor(self):
        import test_core
        controller = Controller(HOSTS)
        fixture = test_core.ControlTests()
        for now in range(0, 64, 3):
            samples = fixture.sensor_fleet(now)
            decision = self.step(controller, now, 16, power=80.04 if now == 63 else 30,
                                 nodes=samples)
        self.assertFalse(decision['learning_eligible'])
        decision = self.step(controller, 66, 16, power=30, nodes=fixture.sensor_fleet(66, ()))
        self.assertEqual((decision['level'], decision['scenario']), (16, 'protect'))
        self.assertIn('传感器消失', ' '.join(decision['reasons']))

    def test_warning_with_other_deficit_cannot_release_hot_recovery(self):
        controller = Controller(HOSTS)
        for now in range(0, 61, 3):
            self.step(controller, now, 16, power=70, nodes=fleet(now, power=70))
        for age in range(3, 124, 3):
            now = 60 + age
            samples = fleet(now, power=70)
            for index, temperature in ((0, max(60, 73 - (age - 3) * .08)), (1, 61)):
                samples[index]['sensors'][0]['temp'] = temperature
                samples[index]['cpu_temp'] = temperature
            decision = self.step(controller, now, 16, power=80.04 if age == 123 else 70,
                                 nodes=samples)
            if age == 120:
                self.assertEqual((controller._policy['state']['tag'],
                                  controller._policy['state']['earned']), ('high_recovery', 27))
        self.assertEqual((decision['level'], decision['scenario']), (16, 'graded_hold'))
        self.assertFalse(decision['learning_eligible'])


if __name__ == '__main__':
    unittest.main()
