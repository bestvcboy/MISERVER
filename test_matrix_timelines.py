import json
import unittest

from core import Controller
from learning import Learning
from test_core import HOSTS
from test_learning import SETTINGS, frame
from test_scenarios import feedback, fleet


class MatrixTimelineTests(unittest.TestCase):
    def step(self, controller, now, actual, *, readback=None, minimum=0, manual=None, **values):
        return controller.decide(fleet(now, **values), now, minimum_level=minimum,
                                 manual_level=manual,
                                 feedback=feedback(now, actual) if readback is None else readback)

    def reach(self, level, **values):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 3601, 3):
            decision = self.step(controller, now, actual, **values)
            if actual == level and decision['level'] == level and decision['trial'] and decision['trial']['outcome'] in (
                    'recovery_released', 'success', 'manual_complete'):
                return controller, now
            actual = decision['level']
        self.fail(f'public timeline did not confirm {level}')

    def test_cold_nine_through_zero_has_only_release_outcomes(self):
        controller, start = self.reach(9, cpu=45)
        actual, changes = 9, []
        learner = Learning(HOSTS)
        for age in range(3, 301, 3):
            now = start + age
            decision = self.step(controller, now, actual, cpu=45)
            row = frame(now, gear=actual)
            row.update(nodes=fleet(now, cpu=45), decision=decision, requested_level=decision['level'])
            learner.observe(row)
            self.assertFalse(decision['learning_eligible'])
            self.assertNotEqual(decision['trial']['outcome'], 'success')
            if decision['level'] != actual:
                changes.append((age, decision['level']))
                self.assertIn('[cold-release]', decision['scenario_detail'])
            actual = decision['level']
        self.assertEqual(changes, [(30, 8), (63, 7), (96, 6), (129, 5), (162, 4),
                                   (195, 3), (228, 2), (261, 1), (294, 0)])
        self.assertEqual(sum(bucket['seconds'] for bucket in learner.buckets.values()), 0)
        self.assertEqual(learner.published, {})

    def test_45_to_46_rebound_has_one_fixed_cap_then_hysteresis_release(self):
        controller, start = self.reach(8, cpu=45)
        deadline = start + 93
        for age in range(3, 67, 3):
            decision = self.step(controller, start + age, 8, cpu=46 if age % 6 else 45.9)
            self.assertEqual(decision['level'], 8)
            self.assertFalse(decision['learning_eligible'])
            if age < 63:
                self.assertIn(f'deadline={deadline}', decision['scenario_detail'])
        for age in range(69, 100, 3):
            decision = self.step(controller, start + age, 8, cpu=46)
        self.assertEqual(decision['level'], 7)
        self.assertEqual(decision['trial']['kind'], 'release')
        self.assertFalse(decision['retry_blocked'])

    def test_47_point_1_exit_holds_low_gear_without_reserve(self):
        controller, start = self.reach(8, cpu=45)
        for age in range(3, 121, 3):
            decision = self.step(controller, start + age, 8, cpu=47.1)
            self.assertEqual(decision['level'], 8)
            self.assertFalse(decision['learning_eligible'])
            self.assertNotIn('[cold-release]', decision['scenario_detail'])
        self.assertNotEqual(decision['trial']['outcome'], 'failed')

    def test_entry_threshold_board_load_and_acceleration_stop_cold(self):
        for change in (dict(cpu=46), dict(board=55.1), dict(load=10)):
            controller, start = self.reach(10, cpu=46)
            for age in range(3, 46, 3):
                decision = self.step(controller, start + age, 10, **change)
                self.assertEqual(decision['level'], 10)
                self.assertNotIn('[cold-release]', decision['scenario_detail'])
        controller, start = self.reach(9, cpu=42)
        for age in range(3, 31, 3):
            decision = self.step(controller, start + age, 9, cpu=42 + .04 * age)
            self.assertEqual(decision['level'], 9)

    def test_flat_70_rising_70_and_falling_73_are_distinct(self):
        for field in ('cpu', 'gpu'):
            controller, start = self.reach(10, cpu=51)
            for age in (3, 6, 9):
                decision = self.step(controller, start + age, 10, **{field: 70})
                self.assertEqual(decision['level'], 10)
            decision = self.step(controller, start + 12, 10, **{field: 70.2})
            self.assertIn(decision['level'], (10, 16))
            decision = self.step(controller, start + 15, 10, **{field: 70.4})
            self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['scenario'], 'protect')
        for field in ('cpu', 'gpu', 'board'):
            controller, start = self.reach(10, cpu=51)
            self.step(controller, start + 3, 10, **{field: 73.2})
            decision = self.step(controller, start + 6, 16, **{field: 73})
            self.assertEqual(decision['level'], 16)

    def test_covered_falling_61_does_not_earn_inadequacy(self):
        controller, start = self.reach(10, cpu=51)
        for age in range(3, 124, 3):
            decision = self.step(controller, start + age, 10, cpu=63 - .02 * age)
            self.assertEqual(decision['level'], 10)
            self.assertFalse(decision['learning_eligible'])
        self.assertNotEqual(decision['scenario'], 'graded_pending')

    def test_exact_targets_prepare_without_graded_credit_for_300_seconds(self):
        for field, target in (('cpu', 60.0), ('gpu', 60.0), ('board', 65.0)):
            controller, start = self.reach(10, cpu=51)
            actual = 10
            for age in range(3, 304, 3):
                decision = self.step(controller, start + age, actual, **{field: target})
                self.assertEqual(decision['level'], 10)
                self.assertFalse(decision['learning_eligible'])
                actual = decision['level']

    def test_60_prepares_low_and_never_directly_reduces_high(self):
        controller, start = self.reach(7, cpu=42)
        decision = self.step(controller, start + 3, 7, cpu=60)
        self.assertEqual(decision['level'], 10)
        controller, start = self.reach(14, cpu=51)
        decision = self.step(controller, start + 3, 14, cpu=60)
        self.assertEqual(decision['level'], 14)

    def test_stationary_58_does_not_acquire_preparation_reserve(self):
        controller, start = self.reach(4, cpu=42)
        for age in range(3, 124, 3):
            decision = self.step(controller, start + age, 4, cpu=58)
            self.assertEqual(decision['level'], 4)
            self.assertFalse(decision['learning_eligible'])
        decision = self.step(controller, start + 126, 4, cpu=60)
        self.assertEqual(decision['level'], 10)
        self.step(controller, start + 129, 10, cpu=60)
        for age in range(132, 256, 3):
            decision = self.step(controller, start + age, 10, cpu=58)
            self.assertEqual(decision['level'], 10)
            self.assertFalse(decision['learning_eligible'])
        controller, start = self.reach(4, cpu=42)
        self.step(controller, start + 3, 4, cpu=57)
        self.step(controller, start + 6, 4, cpu=57.2)
        decision = self.step(controller, start + 9, 4, cpu=57.4)
        self.assertEqual(decision['level'], 10)

    def test_power_reserve_exposes_current_original_episode(self):
        controller, start = self.reach(4, cpu=42)
        completed = controller.trial
        self.assertEqual((completed['from'], completed['to'], completed['outcome']),
                         (5, 4, 'recovery_released'))
        for age in (3, 6):
            warning = self.step(controller, start + age, 4, cpu=42, power=80)
            self.assertEqual((warning['level'], warning['scenario']), (4, 'power_warning'))
        requested = start + 9
        decision = self.step(controller, requested, 4, cpu=42, power=80)
        trial = decision['execution_trial']
        self.assertEqual(decision['level'], 10)
        self.assertEqual((trial['from'], trial['to'], trial['status'], trial['outcome']),
                         (4, 10, 'pending', None))
        self.assertEqual(trial['requested_at'], requested)
        self.assertEqual(trial['original_deadline'], requested + 60)
        self.assertEqual(trial['destination_requested_at'], requested)
        self.assertEqual((decision['trial']['from'], decision['trial']['to'], decision['trial']['outcome']),
                         (5, 4, 'invalidated'))
        self.assertEqual(decision['trial']['requested_at'], start - 3)
        self.assertEqual(decision['trial']['destination_requested_at'], start - 3)
        json.dumps(decision, allow_nan=False)
        decision = self.step(controller, requested + 3, 4, cpu=42, power=80, manual=12)
        trial = decision['execution_trial']
        self.assertEqual((trial['from'], trial['to']), (4, 12))
        self.assertEqual((trial['original_from'], trial['original_to']), (4, 10))
        self.assertEqual(trial['requested_at'], requested)
        self.assertEqual(trial['original_deadline'], requested + 60)
        self.assertEqual(trial['destination_requested_at'], requested + 3)
        json.dumps(decision, allow_nan=False)
        decision = self.step(controller, requested + 6, 12, cpu=42, power=80, manual=12)
        self.assertEqual(decision['execution_trial']['status'], 'confirmed')
        self.assertEqual(controller._policy['operation']['status'], 'confirmed')
        self.assertEqual((decision['execution_trial']['from'], decision['execution_trial']['to']), (4, 12))
        self.assertEqual(decision['trial']['requested_at'], start - 3)

    def test_staged_dwell_pending_and_witness_switch(self):
        controller, start = self.reach(10, cpu=51)
        onset = start + 3
        self.step(controller, onset, 10, cpu=61)
        for age in range(3, 31, 3):
            decision = self.step(controller, onset + age, 10, cpu=61)
            self.assertEqual(decision['level'], 11 if age == 30 else 10)
        requested = onset + 30
        for age in range(3, 19, 3):
            decision = self.step(controller, requested + age, 10, cpu=61,
                                 readback=feedback(requested + age, 10, execution='pending', confirmed=False))
            self.assertEqual(decision['level'], 11)
        confirmed = requested + 21
        self.step(controller, confirmed, 11, cpu=61)
        for age in range(3, 16, 3):
            self.step(controller, confirmed + age, 11, cpu=61)
        switched = confirmed + 18
        self.step(controller, switched, 11, cpu=40, gpu=61)
        for age in range(3, 31, 3):
            decision = self.step(controller, switched + age, 11, cpu=40, gpu=61)
            self.assertEqual(decision['level'], 12 if age == 30 else 11)

    def test_pending_original_deadline_survives_context_manual_and_protect(self):
        for event in ('context', 'manual', 'protect'):
            controller, start = self.reach(9, cpu=42)
            for age in range(3, 31, 3):
                decision = self.step(controller, start + age, 9, cpu=42)
            requested = start + 30
            deadline = requested + 60
            for age in range(3, 61, 3):
                now = requested + age
                if event == 'protect':
                    controller.protect('storage')
                decision = self.step(controller, now, 9, cpu=42, power=50 if event == 'context' else 30,
                                     manual=12 if event == 'manual' else None,
                                     readback=feedback(now, 9, execution='pending', confirmed=False))
                self.assertEqual(decision['trial']['deadline'], deadline)
                self.assertEqual(decision['trial']['requested_at'], requested)
                self.assertEqual(decision['trial']['original_deadline'], deadline)
                self.assertEqual(decision['execution_trial']['destination_requested_at'],
                                 requested + 3 if event == 'manual' else requested)
                self.assertFalse(decision['learning_eligible'])
                if age == 60:
                    self.assertEqual(decision['level'], 16)
                    self.assertEqual(decision['trial']['outcome'], 'execution_timeout')

    def test_superseded_abandoned_readback_and_exact_deadline_never_confirm(self):
        controller, start = self.reach(9, cpu=42)
        for age in range(3, 31, 3):
            self.step(controller, start + age, 9, cpu=42)
        requested = start + 30
        self.step(controller, requested + 3, 9, cpu=42, manual=12)
        decision = self.step(controller, requested + 6, 8, cpu=42, manual=12)
        self.assertEqual(decision['level'], 12)
        self.assertEqual(decision['trial']['requested_at'], requested)
        self.assertEqual(decision['trial']['original_deadline'], requested + 60)
        self.assertEqual(decision['execution_trial']['destination_requested_at'], requested + 3)
        self.assertIsNone(decision['trial']['observation_elapsed'])
        for age in range(9, 61, 3):
            decision = self.step(controller, requested + age, 12 if age == 60 else 8,
                                 cpu=42, manual=12)
        self.assertEqual(decision['level'], 16)
        self.assertEqual(decision['trial']['outcome'], 'execution_timeout')

    def test_probe_diagnostics_keep_original_stamps_during_observation(self):
        import json
        controller, start = self.reach(10, cpu=51)
        for age in range(3, 151, 3):
            decision = self.step(controller, start + age, 10, cpu=51)
        requested = start + 150
        self.assertEqual(decision['level'], 9)
        decision = self.step(controller, requested + 3, 9, cpu=51)
        self.assertEqual(decision['trial']['deadline'], requested + 303)
        self.assertEqual(decision['trial']['requested_at'], requested)
        self.assertEqual(decision['trial']['original_deadline'], requested + 60)
        self.assertEqual(decision['trial']['destination_requested_at'], requested)
        json.dumps(decision, allow_nan=False)

    def test_unknown_power_acknowledges_physical_drop_but_never_earns_credit(self):
        for acknowledged in (False, True):
            controller, start = self.reach(5, cpu=42)
            for age in range(3, 31, 3):
                decision = self.step(controller, start + age, 5, cpu=42)
            requested = start + 30
            self.assertEqual(decision['level'], 4)
            for age in range(3, 79, 3):
                now = requested + age
                nodes = fleet(now, cpu=42)
                nodes[0]['power_w'] = None
                decision = controller.decide(nodes, now, minimum_level=0,
                    feedback=feedback(now, 4 if acknowledged else 5))
                self.assertFalse(decision['learning_eligible'])
                if acknowledged:
                    self.assertEqual(decision['level'], 4)
                    self.assertNotEqual(decision['trial']['outcome'], 'execution_timeout')
                elif age >= 60:
                    self.assertEqual(decision['level'], 16)
                    self.assertEqual(decision['trial']['outcome'], 'execution_timeout')

    def test_repeat_feedback_breaks_cold_and_gap_requires_startup(self):
        controller, start = self.reach(9, cpu=42)
        for age in range(3, 37, 3):
            decision = self.step(controller, start + age, 9, cpu=42,
                                 readback=feedback(start + age - (3 if age == 15 else 0), 9))
            self.assertEqual(decision['level'], 8 if age == 36 else 9)
        decision = self.step(controller, start + 45, 8, cpu=42)
        self.assertEqual(decision['level'], 16)
        for age in range(48, 108, 3):
            decision = self.step(controller, start + age, 16, cpu=42)
            self.assertEqual(decision['level'], 16)
        self.assertEqual(self.step(controller, start + 108, 16, cpu=42)['level'], 15)

    def test_hot_recovery_uses_former_witness_and_separate_release_clocks(self):
        controller, start = self.reach(10, cpu=51)
        self.step(controller, start + 3, 10, cpu=73)
        for age in range(6, 70, 3):
            decision = self.step(controller, start + age, 16, cpu=66 - .02 * (age - 6))
            if decision['level'] == 15:
                requested = start + age
                break
        self.assertEqual(decision['level'], 15)
        self.assertFalse(decision['learning_eligible'])
        self.step(controller, requested + 3, 15, cpu=64.7)
        for age in range(6, 40, 3):
            decision = self.step(controller, requested + age, 15, cpu=64.7 - .02 * (age - 3))
        self.assertEqual(decision['level'], 14)

    def test_manual_minimum_overrides_but_safety_and_false_learning_remain(self):
        controller, start = self.reach(8, cpu=42)
        decision = self.step(controller, start + 3, 8, cpu=42, manual=14)
        self.assertEqual(decision['level'], 14)
        self.assertFalse(decision['learning_eligible'])
        decision = self.step(controller, start + 6, 14, cpu=73, manual=14)
        self.assertEqual(decision['level'], 16)

    def test_source_wall_age_spends_clock_budget_and_monotonic_elapsed(self):
        for age, fault in ((-.5, False), (-.500001, True), (5.499999, False),
                           (5.5, True), (6, True), (6.000001, True), (-2, True), (900, True)):
            controller = Controller(HOSTS)
            nodes = fleet(0, cpu=42)
            for sample in nodes:
                sample.update(sampled_at=10000, source_ts=10000 - age)
            decision = controller.decide(nodes, 0, minimum_level=0, feedback=feedback(0, 16))
            self.assertEqual(decision['level'], 16)
            self.assertEqual(any('来源时间无效、过期' in reason for reason in decision['reasons']), fault)
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 64, 3):
            nodes = fleet(now, cpu=42)
            for sample in nodes:
                sample.update(sampled_at=10000 + now, source_ts=9100 + now)
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, actual))
            self.assertEqual(decision['level'], 16)
            actual = decision['level']
        self.assertTrue(all(node['sampled_at'] - node['source_ts'] == 900 for node in nodes))
        self.assertIn('来源时间无效、过期', ' '.join(decision['reasons']))
        for source_age in (2.499999, 2.5, 3, 3.000001):
            controller = Controller(HOSTS)
            nodes = fleet(100, cpu=42)
            for sample in nodes:
                sample.update(sampled_at=1000000000, source_ts=1000000000 - source_age)
            decision = controller.decide(nodes, 103, minimum_level=0, feedback=feedback(103, 16))
            self.assertEqual(any('来源时间无效、过期' in reason for reason in decision['reasons']), source_age >= 2.5)

    def test_old_step_cannot_amplify_current_rise_but_fast_projection_protects(self):
        for field in ('cpu', 'gpu', 'board'):
            controller, start = self.reach(10, cpu=51)
            for age, temperature in ((3, 57), (6, 57.2), (9, 57.4)):
                decision = self.step(controller, start + age, 10, **{field: temperature})
            self.assertEqual(decision['level'], 10)
            controller, start = self.reach(10, cpu=51)
            for age, temperature in ((3, 65), (6, 66), (9, 67)):
                decision = self.step(controller, start + age, 10, **{field: temperature})
            self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['scenario'], 'protect')

    def test_strict_learning_window_forgets_old_variation_and_rejects_interruptions(self):
        for event in ('spike', 'duplicate', 'gap', 'source', 'identity', 'power', 'gear'):
            with self.subTest(event=event):
                controller, start = self.reach(10, cpu=51)
                actual = 10
                for age in range(3, 604, 3):
                    decision = self.step(controller, start + age, actual, minimum=10, cpu=51)
                    actual = decision['level']
                self.assertTrue(decision['learning_eligible'])
                now = start + 606
                nodes = fleet(now, cpu=51)
                readback = feedback(now, actual)
                if event == 'spike':
                    nodes[0]['cpu_load'] = 45
                elif event == 'duplicate':
                    readback['observed_mono'] = now - 3
                elif event == 'gap':
                    now += 6
                    nodes, readback = fleet(now, cpu=51), feedback(now, actual)
                elif event == 'source':
                    nodes[0]['power_source'] = 'new-source'
                elif event == 'identity':
                    nodes[0]['hostname'] = 'new-host'
                elif event == 'power':
                    nodes[0]['power_w'] = 42
                elif event == 'gear':
                    readback['level'] = 11
                decision = controller.decide(nodes, now, minimum_level=10, feedback=readback)
                self.assertFalse(decision['learning_eligible'])
                actual = decision['level']
                for elapsed in range(3, 121, 3):
                    decision = self.step(controller, now + elapsed, actual, minimum=10, cpu=51)
                    self.assertFalse(decision['learning_eligible'])
                    actual = decision['level']
                for elapsed in range(123, 604, 3):
                    decision = self.step(controller, now + elapsed, actual, minimum=10, cpu=51)
                    actual = decision['level']
                self.assertTrue(decision['learning_eligible'])

    def test_small_idle_variation_earns_learning_only_after_covered_strict_window(self):
        controller, start = self.reach(10, cpu=51)
        actual = 10
        for age in range(3, 604, 3):
            nodes = fleet(start + age, cpu=51, load=0 if age < 300 else 9)
            decision = controller.decide(nodes, start + age, minimum_level=10,
                                         feedback=feedback(start + age, actual))
            if 300 <= age < 420:
                self.assertFalse(decision['learning_eligible'])
            actual = decision['level']
        self.assertTrue(decision['learning_eligible'])
        for changing in ('load', 'power'):
            controller, start = self.reach(10, cpu=51)
            actual = 10
            for age in range(3, 804, 3):
                values = dict(cpu=51, power=50)
                if changing == 'load':
                    values['load'] = 0 if age % 6 else 9
                else:
                    values['power'] = 51 if age % 6 else 70
                decision = self.step(controller, start + age, actual, minimum=9, **values)
                self.assertFalse(decision['learning_eligible'])
                actual = decision['level']

    def test_terminal_execution_evidence_survives_new_faults_until_startup_qualifies(self):
        controller, start = self.reach(4, cpu=42)
        for age in (3, 6):
            self.step(controller, start + age, 4, cpu=42, power=80)
        requested, deadline = start + 9, start + 69
        for now in range(requested, deadline + 1, 3):
            decision = self.step(controller, now, 4, cpu=42, power=80)
        for now in range(deadline + 3, deadline + 34, 3):
            nodes = fleet(now, cpu=42)
            nodes[0]['online'] = False
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 4))
            controller.protect('storage fault')
            operation = decision['execution_trial']
            self.assertEqual((operation['requested_at'], operation['original_deadline'], operation['outcome']),
                             (requested, deadline, 'execution_timeout'))
            self.assertEqual(operation['destination_requested_at'], requested)
            self.assertEqual(decision['level'], 16)
            self.assertFalse(decision['learning_eligible'])
        end = now
        for now in range(end + 3, end + 61, 3):
            decision = self.step(controller, now, 16, cpu=42)
            self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['execution_trial']['outcome'], 'execution_timeout')
        decision = self.step(controller, end + 63, 16, cpu=42)
        self.assertEqual(decision['level'], 15)
        self.assertEqual(decision['execution_trial']['requested_at'], end + 63)

    def test_known_wifi_absence_preserves_identity_but_earns_no_missing_interval(self):
        from test_core import ControlTests
        controller = Controller(HOSTS)
        for now in range(0, 70, 3):
            nodes = ControlTests().sensor_fleet(now, () if now == 60 else (('wifi', 50),))
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, 16))
            self.assertFalse(decision['learning_eligible'])
            self.assertEqual(decision['level'], 15 if now >= 66 else 16)
            if now == 60:
                self.assertIn('Wi-Fi', ' '.join(decision['reasons']))

    def pending_cold_eight(self):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 292, 3):
            decision = self.step(controller, now, actual, cpu=42)
            if now < 291:
                actual = decision['level']
        self.assertEqual(actual, 9)
        self.assertEqual(decision['level'], 8)
        self.assertEqual(decision['execution_trial']['original_deadline'], 351)
        return controller

    def test_pending_cold_release_headroom_loss_revokes_unconfirmed_destination(self):
        controller = self.pending_cold_eight()
        decision = self.step(controller, 294, 9, cpu=58.01)
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertEqual(decision['execution_trial']['original_deadline'], 351)
        self.assertEqual(decision['execution_trial']['destination_requested_at'], 294)
        for now in (297, 300):
            readback = dict(feedback(now, 9), execution='pending', confirmed=False)
            decision = self.step(controller, now, 9, readback=readback, cpu=58.01)
        decision = self.step(controller, 303, 8, cpu=58.01)
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertNotEqual(decision['scenario'], 'recovery_released')

    def test_pending_cold_release_known_load_change_restores_confirmed_gear(self):
        controller = self.pending_cold_eight()
        decision = self.step(controller, 294, 9, cpu=42, load=10)
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertEqual(decision['execution_trial']['original_deadline'], 351)
        self.assertEqual(decision['execution_trial']['destination_requested_at'], 294)
        self.assertFalse(decision['learning_eligible'])

    def test_pending_cold_release_current_continuing_warming_revokes_authority(self):
        controller = self.pending_cold_eight()
        for now in range(294, 331, 3):
            decision = self.step(controller, now, 9, cpu=42 + (now - 291) * .1)
        self.assertIn('[warming]', ' '.join(decision['reasons']))
        self.assertEqual(decision['level'], 9)
        self.assertEqual(decision['trial']['outcome'], 'invalidated')
        self.assertEqual(decision['execution_trial']['original_deadline'], 351)
        self.assertFalse(decision['learning_eligible'])

    def test_pending_ordinary_release_lost_headroom_restores_actual_with_fixed_deadline(self):
        controller = Controller(HOSTS)
        for now in range(0, 61, 3):
            decision = self.step(controller, now, 16, cpu=58)
        self.assertEqual(decision['level'], 15)
        for now in range(63, 121, 3):
            decision = self.step(controller, now, 16, cpu=58.01)
            self.assertEqual(decision['level'], 16)
            self.assertEqual(decision['trial']['outcome'], 'invalidated')
            self.assertEqual(decision['trial']['original_deadline'], 120)
            self.assertEqual(decision['execution_trial']['original_deadline'], 120)
            self.assertFalse(decision['learning_eligible'])


if __name__ == '__main__':
    unittest.main()
