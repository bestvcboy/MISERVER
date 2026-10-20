import unittest
from copy import deepcopy

from core import Controller, MotionAuthority
from test_core import HOSTS
from test_scenarios import feedback, fleet


class ControlFixture:
    def controller(self):
        return Controller(HOSTS, rules_only=True, high_wind_gate=True)

    def step(self, controller, now, actual=10, cpu=45, observed=None, **values):
        return controller.decide(fleet(now, cpu=cpu, gpu=47, board=54, **values), now,
            minimum_level=0, feedback=feedback(now, actual) if observed is None else observed)

    def hot(self, actual=10):
        controller = self.controller()
        self.step(controller, 100, actual=actual, cpu=80)
        return controller, controller._policy['operation']['episode']


class RetirementTests(ControlFixture, unittest.TestCase):
    def test_unknown_and_stale_ceiling_retarget_keeps_original_episode(self):
        for observed in (feedback(103, None, confirmed=False, execution='pending', rpm=None),
                         feedback(90, 10)):
            with self.subTest(observed=observed):
                controller, episode = self.hot()
                decision = self.step(controller, 103, observed=observed)
                operation = controller._policy['operation']
                self.assertIs(operation['episode'], episode)
                self.assertEqual((decision['level'], operation['destination']), (10, 10))
                self.assertEqual(operation['destination_requested_at'], 103)
                self.assertIsNone(operation['confirmed_at'])
                self.assertEqual(operation['original_deadline'], 160)
                self.assertEqual(decision['execution_trial']['status'], 'pending')
                self.assertFalse(controller.feedback_matches_request(10, 103, 103))

    def test_repeated_ceiling_does_not_renew_destination_or_deadline(self):
        controller, episode = self.hot(actual=15)
        replacement = None
        for now in (103, 106, 109, 112):
            decision = self.step(controller, now, actual=16)
            operation = controller._policy['operation']
            replacement = operation['episode'] if replacement is None else replacement
            self.assertIs(operation['episode'], replacement)
            self.assertIsNot(replacement, episode)
            self.assertEqual(operation['destination_requested_at'], 103)
            self.assertEqual(operation['original_deadline'], 163)
            self.assertEqual(decision['execution_trial']['original_to'], 15)
            self.assertEqual(decision['level'], 15)
            self.assertEqual(decision['execution_trial']['status'], 'pending')
            self.assertIsNone(controller._policy['last_downward'])
        self.assertFalse(controller.feedback_matches_request(10, 113, 113))
        decision = self.step(controller, 115, actual=15)
        self.assertEqual(decision['execution_trial']['status'], 'confirmed')
        self.assertEqual(controller._policy['last_downward']['edge'], (16, 15))
        self.assertEqual(controller.operating.observe_credit, 0)

    def test_same_protect_preserves_pending_budget_and_independent_critical(self):
        controller, episode = self.hot()
        self.step(controller, 103, observed=feedback(90, 10))
        operation = controller._policy['operation']
        controller.motion = MotionAuthority(103, ('host', 'cpu'), 75, 75)
        for now in (104, 105):
            decision = controller.protect('storage', now)
            self.assertIs(operation['episode'], episode)
            self.assertEqual(operation['destination_requested_at'], 103)
            self.assertEqual(decision['execution_trial']['status'], 'pending')
            self.assertEqual(controller.motion.actual_c, 75)
        self.assertEqual(operation['original_deadline'], 160)

    def test_different_protect_cancels_even_when_motion_remains(self):
        controller, episode = self.hot()
        decision = controller.protect('storage', 103)
        operation = controller._policy['operation']
        self.assertEqual(decision['level'], 12)
        self.assertEqual(decision['execution_trial']['status'], 'invalidated')
        self.assertIsNone(operation['confirmed_at'])
        self.assertIsNone(operation['episode'])
        self.assertEqual(operation['original_deadline'], episode.original_deadline)
        history = deepcopy(operation)
        controller.protect('again', 104)
        self.assertEqual(operation, history)
        self.assertEqual(controller.operating.confirmed_level, 10)

    def test_late_old_write_is_physical_change_not_old_confirmation(self):
        controller, _ = self.hot()
        self.step(controller, 103)
        historical = deepcopy(controller._policy['operation'])
        decision = self.step(controller, 106, actual=16,
            observed=feedback(106, 16, confirmed=False, execution='pending'))
        self.assertEqual(controller.operating.confirmed_level, 16)
        self.assertEqual(decision['level'], 16)
        self.assertEqual(decision['execution_trial']['status'], 'invalidated')
        self.assertEqual(controller._policy['operation'], historical)
        self.assertIsNone(controller._policy['last_downward'])

    def test_expiry_precedes_ceiling_and_same_timestamp_confirmation(self):
        controller, _ = self.hot()
        decision = self.step(controller, 160, actual=16)
        self.assertEqual(decision['execution_trial']['status'], 'execution_timeout')
        self.assertEqual(decision['execution_trial']['to'], 11)
        self.assertIsNone(controller._policy['operation']['confirmed_at'])
        self.assertTrue(controller._policy['startup_required'])
        self.step(controller, 163, observed=feedback(160, 10))
        self.assertTrue(controller._policy['startup_required'])
        decision = self.step(controller, 166)
        self.assertFalse(controller._policy['startup_required'])
        self.assertEqual(decision['execution_trial']['status'], 'execution_timeout')
        self.assertEqual(controller.operating.observe_credit, 0)

    def test_timeout_during_unknown_communication_hold_cannot_renew(self):
        controller, _ = self.hot()
        for now in (160, 163, 220, 400):
            observed = feedback(now, None, confirmed=False, execution='pending', rpm=None,
                readback_hold=dict(level=10, until=now + 20, failures=1))
            decision = self.step(controller, now, observed=observed)
            self.assertEqual(decision['execution_trial']['status'], 'execution_timeout')
            self.assertEqual(decision['execution_trial']['original_deadline'], 160)
            self.assertTrue(controller._policy['startup_required'])
            controller.protect('still interrupted', now)
            self.assertFalse(controller._pending())
        self.assertEqual(controller._policy['operation']['transition']['at'], 160)

    def test_protect_at_deadline_retires_before_later_poll(self):
        controller, _ = self.hot()
        operation = controller._policy['operation']
        decision = controller.protect('storage', 160)
        self.assertEqual(decision['execution_trial']['status'], 'execution_timeout')
        self.assertTrue(controller._policy['startup_required'])
        decision = self.step(controller, 163, actual=10, cpu=80)
        self.assertEqual(decision['execution_trial']['original_deadline'], 160)
        self.assertEqual(decision['execution_trial']['status'], 'execution_timeout')
        decision = self.step(controller, 166, actual=10, cpu=80)
        self.assertEqual(operation['original_deadline'], 160)
        self.assertIsNone(operation['episode'])
        self.assertEqual(decision['level'], 11)
        self.assertEqual(decision['execution_trial']['original_deadline'], 226)
        self.assertIsNot(controller._policy['operation'], operation)

    def test_thermal_invalidation_and_aba_do_not_end_execution(self):
        for production in (False, True):
            with self.subTest(production=production):
                controller = Controller(HOSTS, rules_only=production, high_wind_gate=production)
                controller._request(7, 10, 100, None, purpose='release', profile='rules')
                episode = controller._policy['operation']['episode']
                for target, now in ((10, 103), (7, 106), (10, 109)):
                    controller._request(target, 10, now, None, purpose='rollback', profile='ordinary')
                    operation = controller._policy['operation']
                    self.assertIs(operation['episode'], episode)
                    self.assertEqual(operation['outcome'], 'invalidated')
                    self.assertEqual(controller._trial_view(operation, execution=True)['status'], 'pending')
                    self.assertFalse(controller.feedback_matches_request(target, now, now))
                    self.assertTrue(controller.feedback_matches_request(target, now + .1, now + .1))
                self.assertEqual(controller.trial['to'], 7)
                trial = deepcopy(controller.trial)
                controller._retire_execution('execution_timeout', 160, cause='expiry')
                if production:
                    self.assertEqual(controller.trial['transition']['at'], 103)
                    self.assertEqual(controller.trial, trial)
                else:
                    self.assertIs(controller.trial, controller._policy['operation'])
                    self.assertEqual(controller.trial['outcome'], 'execution_timeout')
                self.assertEqual(controller._trial_view(controller._policy['operation'], execution=True)['status'], 'execution_timeout')

    def test_terminal_projection_wins_over_historical_confirmation(self):
        controller = self.controller()
        controller._request(9, 10, 100, None, purpose='release', profile='rules')
        controller._policy['operation'].update(episode=None, confirmed_at=103, status='confirmed')
        controller._retire_execution('invalidated', 106, cause='storage')
        view = controller._trial_view(controller._policy['operation'], execution=True)
        self.assertEqual(view['status'], 'invalidated')
        self.assertEqual(controller._policy['operation']['confirmed_at'], 103)

    def test_unknown_actual_cannot_open_episode(self):
        controller = self.controller()
        controller._request(10, None, 100, None, purpose='protect', profile='protect')
        self.assertIsNone(controller._policy['operation'])
        self.assertEqual(controller.level, 10)


class ObservationTests(ControlFixture, unittest.TestCase):
    def test_idle_epoch_clears_before_starting_fresh_180_window(self):
        controller = self.controller()
        for now in range(0, 91, 3):
            self.step(controller, now, load=35, power=70)
        self.assertEqual(controller._workload_cycle.phase, 'stable_loaded')
        cleared_at = None
        for now in range(93, 241, 3):
            self.step(controller, now, load=5, power=60)
            if controller._workload_cycle.phase == 'idle':
                cleared_at = now
                break
        self.assertIsNotNone(cleared_at)
        self.assertGreaterEqual(cleared_at - 93, 60)
        self.assertEqual(controller.operating.observe_credit, 0)
        for now in range(cleared_at + 3, cleared_at + 180, 3):
            decision = self.step(controller, now)
            self.assertEqual(decision['level'], 10)
        self.assertEqual(self.step(controller, cleared_at + 180)['level'], 9)

    def test_manual_timing_and_single_step_remain_unchanged(self):
        controller = self.controller()
        actual, edges = 10, []
        for now in range(0, 180, 3):
            decision = controller.decide(fleet(now, cpu=45, gpu=47, board=54, load=5, power=60), now,
                minimum_level=0, manual_level=0, feedback=feedback(now, actual))
            if decision['level'] < actual:
                edges.append((now, actual, decision['level']))
            actual = decision['level']
        self.assertEqual(edges[:2], [(60, 10, 9), (123, 9, 8)])

    def prime(self):
        controller = self.controller()
        for now in range(0, 181, 3):
            self.step(controller, now, load=5, power=60)
        self.assertEqual(controller.operating.observe_credit, 150)
        return controller

    def test_gap_and_eligibility_interruptions_reset_entire_window(self):
        for values in (dict(board=60.1), dict(load=30), dict(power=80), dict(power=None)):
            with self.subTest(values=values):
                controller = self.prime()
                nodes = fleet(183, cpu=45, gpu=47, board=values.get('board', 54),
                    load=values.get('load', 5), power=values.get('power', 60))
                controller.decide(nodes, 183, minimum_level=0, feedback=feedback(183, 10))
                self.assertEqual(controller.operating.observe_credit, 0)
                self.step(controller, 186, load=5, power=60)
                self.assertEqual(controller.operating.observe_credit, 0)
        controller = self.prime()
        self.step(controller, 189)
        self.assertEqual(controller.operating.observe_credit, 0)

    def test_pending_time_is_not_banked_after_confirmation(self):
        controller = self.controller()
        for now in range(0, 211, 3):
            decision = self.step(controller, now)
        self.assertEqual(decision['level'], 9)
        for now in range(213, 240, 3):
            decision = self.step(controller, now)
            self.assertEqual(decision['execution_trial']['status'], 'pending')
            self.assertEqual(controller.operating.observe_credit, 0)
        self.step(controller, 240, actual=9)
        self.assertEqual(controller.operating.observe_credit, 0)
        for now in range(243, 420, 3):
            decision = self.step(controller, now, actual=9)
            self.assertEqual(decision['level'], 9)
        decision = self.step(controller, 420, actual=9)
        self.assertEqual(decision['level'], 8)

    def test_pause_cancels_pending_descent_and_keeps_tombstone(self):
        controller = self.controller()
        for now in range(0, 211, 3):
            self.step(controller, now)
        decision = controller.pause_release('storage', 211)
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['execution_trial']['status'], 'invalidated')
        self.assertEqual(decision['execution_trial']['original_deadline'], 270)
        for now in range(213, 390, 3):
            decision = self.step(controller, now)
            self.assertEqual(decision['level'], 10)

    def failed_controller(self):
        controller = self.controller()
        actual = 10
        confirmed = None
        for now in range(0, 500, 3):
            decision = self.step(controller, now, actual, cpu=58 if confirmed is not None else 49,
                load=25, power=70)
            actual = decision['level']
            if decision['scenario'] == 'recovery_released':
                confirmed = now
            if decision['retry_blocked']:
                return controller, now, actual
        self.fail('fixture did not produce attributable failure')

    def test_failure_requires_both_measures_and_fresh_search_window(self):
        controller, failed_at, actual = self.failed_controller()
        failure = deepcopy(controller.operating.failed_floor)
        for now in range(failed_at + 3, failed_at + 153, 3):
            decision = self.step(controller, now, actual, load=25, power=56)
            actual = decision['level']
            self.assertEqual(controller.operating.failed_floor, failure)
        improved_at = now + 3
        for now in range(improved_at, improved_at + 61, 3):
            decision = self.step(controller, now, actual, load=10, power=56)
            actual = decision['level']
            if now < improved_at + 60:
                self.assertTrue(decision['retry_blocked'])
        self.assertFalse(decision['retry_blocked'])
        self.assertEqual(controller.operating.observe_credit, 0)
        for now in range(improved_at + 63, improved_at + 243, 3):
            decision = self.step(controller, now, actual, load=10, power=56)
            self.assertEqual(decision['level'], actual)
        decision = self.step(controller, improved_at + 243, actual, load=10, power=56)
        self.assertEqual(decision['level'], actual - 1)

    def test_failure_improvement_progress_breaks_on_missing_or_warming(self):
        for interruption in ('missing', 'warming', 'duplicate'):
            with self.subTest(interruption=interruption):
                controller, failed_at, actual = self.failed_controller()
                for now in range(failed_at + 3, failed_at + 153, 3):
                    decision = self.step(controller, now, actual, load=25, power=70)
                    actual = decision['level']
                start = now + 3
                for now in range(start, start + 33, 3):
                    decision = self.step(controller, now, actual, load=10, power=56)
                    actual = decision['level']
                self.assertGreater(controller.operating.failure_credit, 0)
                now += 3
                if interruption == 'missing':
                    self.step(controller, now, actual, load=10, power=None)
                    self.assertEqual(controller.operating.failure_credit, 0)
                elif interruption == 'warming':
                    for offset in range(0, 18, 3):
                        self.step(controller, now + offset, actual, cpu=46 + offset * .05, load=10, power=56)
                    self.assertEqual(controller.operating.failure_credit, 0)
                else:
                    credit = controller.operating.failure_credit
                    self.step(controller, now, actual, load=10, power=56, observed=feedback(now - 3, actual))
                    self.assertEqual(controller.operating.failure_credit, credit)
                self.assertIsNotNone(controller.operating.failed_floor)

    def test_manual_switch_and_safe_gear_cooling_do_not_erase_automatic_failure(self):
        controller, failed_at, actual = self.failed_controller()
        failure = deepcopy(controller.operating.failed_floor)
        for now in range(failed_at + 3, failed_at + 900, 3):
            decision = controller.decide(fleet(now, cpu=45, gpu=47, board=54, load=25, power=70), now,
                minimum_level=0, manual_level=10, feedback=feedback(now, actual))
            actual = decision['level']
            self.assertEqual(controller.operating.failed_floor, failure)

    def test_later_cancellation_keeps_failed_trial_cause(self):
        controller, failed_at, actual = self.failed_controller()
        trial = deepcopy(controller.trial)
        controller.protect('storage interruption', failed_at + 1)
        self.step(controller, failed_at + 3, actual=8,
            observed=feedback(failed_at + 3, 8, confirmed=False, execution='pending'))
        self.assertEqual(controller.trial, trial)


if __name__ == '__main__':
    unittest.main()
