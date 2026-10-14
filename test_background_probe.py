import math
import unittest
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from core import Controller
from test_core import HOSTS
from test_scenarios import feedback, fleet, heat_to_target_risk


def replay_trace(name, mode):
    path = Path(__file__).parent / '.audit' / name
    raw = path.read_bytes()
    expected = {'lower-airflow-live-trace.json': 'f9bd0d7b28f1e31bf22ac197a88a930be5d9436e03d11f23db6fbfb2ea8286aa',
                'background-probe-long-trace.json': '1ed4a4b9c0e0227cce1644060c5f3f8aa47b401145257b86a12a0b58a4d58c89'}
    assert hashlib.sha256(raw).hexdigest() == expected[name], 'immutable trace changed'
    rows = json.loads(raw)
    controller, actual, pending = Controller(HOSTS), 16, None
    events, confirmations, successes, outcomes, invalidations = [], {}, {}, {}, []
    previous = None
    for row in rows:
        now = row['learning']['mono']
        if mode == 'delayed' and pending and row['fan']['observed_mono'] - pending[0] >= 45:
            actual, pending = pending[1], None
        readback = row['fan'] if mode == 'original' else dict(row['fan'], level=actual, confirmed=True, execution='matched')
        decision = controller.decide(row['nodes'], now, minimum_level=0, feedback=readback,
                                     power_thresholds=row['active_power_policy'])
        trial = controller.trial
        if trial and trial['outcome'] is not None:
            if trial['outcome'] == 'invalidated' and outcomes.get(trial['requested_at']) != 'invalidated':
                invalidations.append(dict(decision['execution_trial'], actual=readback['level'],
                    requested=decision['level'], maximum_load=max(max(node['cpu_load'], node['gpu_load']) for node in row['nodes'])))
            outcomes[trial['requested_at']] = trial['outcome']
        state = (decision['level'], trial['status'] if trial else None)
        seconds = round(row['ts'] - rows[0]['ts'], 3)
        if state != previous:
            events.append((seconds, *state))
        previous = state
        if trial and trial['kind'] == 'probe' and trial['confirmed_at'] is not None:
            key = trial['requested_at']
            assert trial['confirmed_at'] > key, 'confirmation predates request'
            if key not in confirmations:
                assert readback['level'] == trial['to'] and readback['confirmed']
                assert now == trial['confirmed_at'], 'confirmation must start at receipt'
                assert key < readback['observed_mono'] <= trial['confirmed_at']
                if mode == 'delayed':
                    assert trial['confirmed_at'] - key >= 45, 'delayed readback predates execution'
                confirmations[key] = (seconds, trial['to'], trial['confirmed_at'])
            if trial['outcome'] in ('success', 'manual_complete') and key not in successes:
                assert now - trial['confirmed_at'] >= 120, 'premature success'
                assert now < trial['deadline'], 'success past deadline'
                successes[key] = (seconds, trial['to'])
        if mode == 'following':
            actual = decision['level']
        elif mode == 'delayed':
            if decision['level'] >= actual:
                actual, pending = decision['level'], None
            elif pending is None or pending[1] != decision['level']:
                pending = (now, decision['level'])
    return {'mode': mode, 'frames': len(rows), 'events': events,
            'confirmations': list(confirmations.values()), 'successes': list(successes.values()),
            'outcomes': list(outcomes.values()), 'invalidations': invalidations}


def background(now, *, cpu=51):
    nodes = fleet(now, cpu=cpu)
    for index, node in enumerate(nodes):
        node['cpu_load'] = 45 if (now // 3 + index * 2) % 9 == 0 else 5
        node['gpu_load'] = 0
        node['power_w'] = 51 + 10 * ((now // 3 + index) % 2)
    return nodes


class BackgroundProbeTests(unittest.TestCase):
    def pending(self, wifi=False):
        from test_core import ControlTests
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1200, 3):
            nodes = ControlTests().sensor_fleet(now) if wifi else fleet(now)
            decision = controller.decide(nodes, now, minimum_level=0, feedback=feedback(now, actual))
            if decision['level'] == 9:
                return controller, now
            actual = decision['level']
        self.fail('no pending probe')

    def step(self, controller, now, actual=9, nodes=None, **options):
        return controller.decide(fleet(now) if nodes is None else nodes, now, minimum_level=0,
                                 feedback=options.pop('feedback', feedback(now, actual)), **options)

    def test_immutable_integrated_replays_and_readback_truth(self):
        for name in ('lower-airflow-live-trace.json', 'background-probe-long-trace.json'):
            following = replay_trace(name, 'following')
            expected_minimum = 15 if name == 'lower-airflow-live-trace.json' else 14
            self.assertEqual(min(gear for _, gear, _ in following['events']), expected_minimum)
            self.assertEqual(following['confirmations'], [])
            self.assertEqual(following['successes'], [])
            self.assertNotIn('failed', following['outcomes'])
            self.assertEqual(set(following['outcomes']), {'recovery_released'})
            for mode in ('original', 'delayed'):
                with self.subTest(trace=name, mode=mode):
                    result = replay_trace(name, mode)
                    if mode == 'original':
                        self.assertEqual(min(gear for _, gear, _ in result['events']), 16)
                        self.assertEqual(result['confirmations'], [])
                        self.assertEqual(result['successes'], [])
                    else:
                        self.assertEqual(result['confirmations'], [])
                        self.assertEqual(result['successes'], [])
                        self.assertNotIn('failed', result['outcomes'])
                        self.assertEqual(min(gear for _, gear, _ in result['events']), 15)
                        expected_outcomes = {'invalidated'} if name == 'lower-airflow-live-trace.json' else {'invalidated', 'recovery_released'}
                        self.assertEqual(set(result['outcomes']), expected_outcomes)
                        self.assertEqual(len(result['invalidations']), 1)
                        revoked = result['invalidations'][0]
                        self.assertGreaterEqual(revoked['maximum_load'], 10)
                        self.assertEqual((revoked['original_from'], revoked['original_to'], revoked['to'], revoked['actual'], revoked['requested']),
                                         (16, 15, 16, 16, 16))
                        self.assertEqual(revoked['original_deadline'], revoked['requested_at'] + 60)
                        self.assertTrue(revoked['requested_at'] < revoked['destination_requested_at'] < revoked['original_deadline'])

    def test_stable_temperatures_with_delayed_execution_confirm_before_success(self):
        controller, actual, pending = Controller(HOSTS), 16, None
        confirmed = False
        for now in range(0, 1800, 3):
            if pending and now - pending[0] >= 45:
                actual, pending = pending[1], None
            decision = self.step(controller, now, actual)
            trial = controller.trial
            if trial and trial['kind'] == 'probe' and trial['confirmed_at'] is not None:
                self.assertGreaterEqual(trial['confirmed_at'] - trial['requested_at'], 45)
                self.assertEqual(actual, trial['to'])
                confirmed = True
                if trial['outcome'] == 'success':
                    self.assertGreaterEqual(now - trial['confirmed_at'], 120)
                    self.assertEqual(actual, 9)
                    break
            if decision['level'] >= actual:
                actual, pending = decision['level'], None
            elif pending is None or pending[1] != decision['level']:
                pending = (now, decision['level'])
        self.assertTrue(confirmed)
        self.assertEqual(trial['outcome'], 'success')
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_workload_mean_weights_preceding_intervals_and_clips_boundary(self):
        controller = Controller(HOSTS)
        for now in [0, 3, 9] + list(range(15, 67, 6)) + [66]:
            load = 0 if now <= 3 else 10 if now == 9 else 30 if now == 66 else 20
            controller.decide(fleet(now, load=load), now, minimum_level=16, feedback=feedback(now))
            if now == 63:
                self.assertEqual(controller._workload_mean(now, 60)[HOSTS[0]], (19, 19, 30))
        self.assertEqual(controller._workload_mean(66, 60)[HOSTS[0]], (20, 20, 30))

    def test_fixed_reference_thresholds_and_single_spike(self):
        for component, delta in (('cpu_load', 15), ('gpu_load', 15), ('power_w', 10)):
            for change in (delta - .001, delta):
                with self.subTest(component=component, change=change):
                    controller, requested = self.pending()
                    self.step(controller, requested + 3)
                    reference = deepcopy(controller.search_reference)
                    for age in range(6, 36, 3):
                        nodes = fleet(requested + age)
                        nodes[-1][component] += change
                        decision = self.step(controller, requested + age, nodes=nodes)
                    self.assertEqual(decision['level'], 10 if change == delta else 9)
                    self.assertEqual(decision['trial']['outcome'], 'invalidated' if change == delta else None)
                    self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
                    if change < delta:
                        self.assertEqual(controller.search_reference, reference)
        controller, requested = self.pending()
        for age in range(3, 127, 3):
            nodes = fleet(requested + age, load=70 if age in (30, 123) else 0)
            decision = self.step(controller, requested + age, nodes=nodes)
            self.assertEqual(decision['level'], 9)
            if age == 123:
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['trial']['outcome'], 'success')

    def test_same_frame_hard_heat_confounds_never_lock(self):
        for phase in ('release', 'probe', 'success'):
            for event in ('source', 'identity', 'power', 'load', 'manual', 'gear', 'feedback', 'missing_power'):
                with self.subTest(phase=phase, event=event):
                    from test_scenarios import ScenarioTests
                    fixture = ScenarioTests()
                    if phase == 'release':
                        controller, end = fixture.reach(15)
                        actual = 15
                    else:
                        controller, end = self.pending()
                        actual = 9
                        for now in range(end + 3, end + (124 if phase == 'success' else 4), 3):
                            self.step(controller, now)
                        end = now
                    if event == 'load':
                        for now in (end + 3, end + 6):
                            self.step(controller, now, actual, fleet(now, load=30))
                        end += 6
                    now = end + 3
                    nodes, options = fleet(now, cpu=73), {}
                    if event == 'source':
                        nodes[-1]['power_source'] = 'changed'
                    elif event == 'identity':
                        nodes[-1]['sensors'].append(dict(id='new', group='other', temp=40, calibrated=True))
                    elif event == 'power':
                        nodes[-1]['power_w'] = 80
                    elif event == 'load':
                        nodes[-1]['cpu_load'] = nodes[-1]['gpu_load'] = 30
                    elif event == 'manual':
                        options['manual_level'] = 0
                    elif event == 'gear':
                        actual += 1
                    elif event == 'feedback':
                        options['feedback'] = feedback(now, actual, fault=True)
                    else:
                        nodes[-1]['power_w'] = None
                    decision = self.step(controller, now, actual, nodes, **options)
                    self.assertEqual(decision['level'], 16)
                    self.assertEqual(decision['trial']['outcome'], 'invalidated')
                    self.assertEqual(decision['trial']['outcome'], 'invalidated')

    def test_success_attribution_cannot_revive_after_continuity_returns(self):
        from test_scenarios import ScenarioTests
        for event in ('source', 'identity', 'power', 'mode', 'pending', 'protect'):
            with self.subTest(event=event):
                controller, end = ScenarioTests().reach(9)
                nodes, options = fleet(end + 3), {}
                if event == 'source':
                    nodes[-1]['power_source'] = 'changed'
                elif event == 'identity':
                    nodes[-1]['sensors'].append(dict(id='new', group='other', temp=40, calibrated=True))
                elif event == 'power':
                    nodes[-1]['power_w'] = None
                elif event == 'mode':
                    options['manual_level'] = 0
                elif event == 'pending':
                    options['feedback'] = feedback(end + 3, 9, execution='pending', confirmed=False)
                else:
                    controller.protect('external interruption')
                decision = self.step(controller, end + 3, nodes=nodes, **options)
                self.assertEqual(decision['trial']['outcome'], 'invalidated')
                decision = self.step(controller, end + 6, nodes=fleet(end + 6, cpu=73))
                self.assertEqual(decision['level'], 16)
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_deadline_confounds_outrank_timeout(self):
        from test_core import ControlTests
        for event in ('source', 'power', 'manual', 'gear', 'fault'):
            with self.subTest(event=event):
                controller, requested = self.pending(wifi=True)
                for age in range(3, 304, 3):
                    now = requested + age
                    nodes = ControlTests().sensor_fleet(now, (('wifi', 50),) if age == 3 or age % 15 == 0 else ())
                    options, actual = {}, 9
                    if age == 303:
                        if event == 'source':
                            nodes[-1]['power_source'] = 'changed'
                        elif event == 'power':
                            nodes[-1]['power_w'] = None
                        elif event == 'manual':
                            options['manual_level'] = 0
                        elif event == 'gear':
                            actual = 10
                        else:
                            nodes[-1]['online'] = False
                    decision = self.step(controller, now, actual, nodes, **options)
                self.assertEqual(decision['trial']['outcome'], 'invalidated')
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_wifi_coverage_exact_boundaries_and_grace_expiry(self):
        from test_core import ControlTests
        for interval, tick, every in ((6, 3, 2), (6.1, 3.05, 2), (12, 3, 4), (15, 3, 5), (30, 3, 10)):
            with self.subTest(interval=interval):
                controller, requested = self.pending(wifi=True)
                confirmed = requested + 3
                for index in range(math.ceil(123 / tick) + 1):
                    now = confirmed + index * tick
                    present = index % 10 in (0, 4, 7) if interval == 12 else index % every == 0
                    readings = (('wifi', 50),) if present else ()
                    decision = self.step(controller, now, nodes=ControlTests().sensor_fleet(now, readings))
                    self.assertEqual(decision['level'], 9)
                    if not readings:
                        self.assertFalse(decision['learning_eligible'])
                        if decision['trial']['outcome'] is None:
                            self.assertEqual(decision['trial']['status'], 'observing')
                    if decision['trial']['outcome'] == 'success':
                        self.assertTrue(readings)
                        break
                self.assertIsNone(decision['trial']['outcome'])
                resume = now
                for age in range(3, 124, 3):
                    now = resume + age
                    decision = self.step(controller, now, nodes=ControlTests().sensor_fleet(now))
                self.assertEqual(decision['trial']['outcome'], 'success')
        controller, requested = self.pending(wifi=True)
        now = requested + 3
        self.step(controller, now, nodes=ControlTests().sensor_fleet(now))
        for age in range(3, 31, 3):
            decision = self.step(controller, now + age, nodes=ControlTests().sensor_fleet(now + age, ()))
            self.assertEqual(decision['level'], 9)
        decision = self.step(controller, now + 30.001, nodes=ControlTests().sensor_fleet(now + 30.001, ()))
        self.assertEqual(decision['level'], 16)
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_entry_anchor_requires_covered_real_thirty_second_history(self):
        from test_core import ControlTests
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1203, 3):
            nodes = ControlTests().sensor_fleet(now, (('wifi', 50),) if now % 15 == 0 else ())
            decision = self.step(controller, now, actual, nodes)
            actual = decision['level']
            self.assertGreaterEqual(actual, 10)
        self.assertEqual(actual, 16)
        self.assertIsNone(controller.search_anchor)
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_workload_six_second_boundary_and_bounded_storage_fail_closed(self):
        for gap in (6, 6.1):
            controller, requested = self.pending()
            decision = self.step(controller, requested + gap)
            self.assertEqual(decision['level'], 9 if gap == 6 else 16)
            self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
        for tick, outcome in ((.25, 'invalidated'), (.5, 'inconclusive_timeout')):
            with self.subTest(tick=tick):
                controller, requested = self.pending()
                self.step(controller, requested + 3)
                for index in range(1, int(300 / tick) + 1):
                    decision = self.step(controller, requested + 3 + index * tick)
                    self.assertTrue(all(len(points) <= 64 for points in controller.workload_history.values()))
                    self.assertNotIn('samples', controller.trial)
                    self.assertTrue(all(len(points) <= 128 for histories in controller.history.values()
                                        for points in histories.values()))
                    if decision['trial']['outcome'] is not None:
                        break
                self.assertEqual(decision['trial']['outcome'], outcome)
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
                self.assertFalse(decision['retry_blocked'])
                if tick == .25:
                    deadline = decision['execution_trial']['original_deadline']
                    stamp = controller.last_evaluation
                    while stamp < deadline:
                        stamp = min(stamp + tick, deadline)
                        decision = self.step(controller, stamp, 9)
                        self.assertEqual(decision['trial']['outcome'], 'invalidated')
                        self.assertEqual(decision['level'], 10 if stamp < deadline else 16)
                        self.assertFalse(decision['learning_eligible'])
                    self.assertEqual(decision['execution_trial']['outcome'], 'execution_timeout')
                    self.assertEqual(decision['execution_trial']['original_deadline'], deadline)
                    decision = self.step(controller, deadline + tick, 10)
                    self.assertEqual(decision['level'], 16)
                    self.assertEqual(decision['execution_trial']['outcome'], 'execution_timeout')
                else:
                    for index in range(1, 241):
                        decision = self.step(controller, requested + 303 + index * tick, 10)
                        self.assertEqual(decision['level'], 10)
                        self.assertFalse(decision['retry_blocked'])
                        self.assertTrue(all(len(points) <= 128 for histories in controller.history.values()
                                            for points in histories.values()))

    def test_whole_fleet_wifi_permission_is_retired_by_intervening_events(self):
        from test_core import ControlTests
        for event in ('heat', 'power', 'source', 'manual', 'feedback', 'protect', 'sustained'):
            for reverse in (False, True):
                with self.subTest(event=event, reverse=reverse):
                    controller = Controller(HOSTS)
                    for now in range(0, 64, 3):
                        nodes = ControlTests().sensor_fleet(now)
                        options = {}
                        if event == 'sustained' and now >= 57:
                            nodes[0]['cpu_load'] = 30
                        if now == 63:
                            if event == 'heat':
                                nodes[0] = fleet(now, cpu=73)[0]
                            elif event == 'power':
                                nodes[0]['power_w'] = 80
                            elif event == 'source':
                                nodes[0]['power_source'] = 'changed'
                            elif event == 'manual':
                                options['manual_level'] = 0
                            elif event == 'feedback':
                                options['feedback'] = feedback(now, 16, fault=True)
                            elif event == 'protect':
                                nodes = ControlTests().sensor_fleet(now, ())
                                controller.protect('external')
                        if reverse:
                            nodes.reverse()
                        self.step(controller, now, 16, nodes, **options)
                    decision = self.step(controller, 66, 16, ControlTests().sensor_fleet(66, ()))
                    self.assertEqual(decision['level'], 16)
                    self.assertIn('传感器消失', ' '.join(decision['reasons']))

    def test_complete_history_sustained_fast_wifi_loss_remains_unsafe(self):
        from test_core import ControlTests
        controller = Controller(HOSTS)
        for now in range(0, 67, 3):
            temp = 20 if now <= 60 else 20 + .4 * (now - 60)
            decision = self.step(controller, now, 16, ControlTests().sensor_fleet(now, (('wifi', temp),)))
        self.assertEqual(decision['level'], 15)
        decision = self.step(controller, 69, 16, ControlTests().sensor_fleet(69, ()))
        self.assertEqual(decision['level'], 16)
        self.assertIn('传感器消失', ' '.join(decision['reasons']))

    def test_rebound_protection_retires_grace_until_a_later_real_observation(self):
        from test_core import ControlTests
        controller, requested = self.pending(wifi=True)
        for age in range(3, 124, 3):
            self.step(controller, requested + age, nodes=ControlTests().sensor_fleet(requested + age))
        now, decision = heat_to_target_risk(controller, requested + 123, ControlTests().sensor_fleet)
        self.assertGreaterEqual(decision['level'], 10)
        decision = self.step(controller, now + 3, decision['level'], ControlTests().sensor_fleet(now + 3, ()))
        self.assertEqual(decision['level'], 16)
        self.assertIn('传感器消失', ' '.join(decision['reasons']))

    def test_quantized_staircase_cannot_use_background_noise_exception(self):
        controller, requested = self.pending()
        for age in range(3, 304, 3):
            decision = self.step(controller, requested + age,
                                 nodes=fleet(requested + age, cpu=51 + (age - 3) // 30))
            if age <= 90:
                self.assertEqual(decision['level'], 9)
                self.assertIsNone(decision['trial']['outcome'])
                self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')
            if decision['level'] >= 10:
                break
        self.assertLessEqual(age, 303)
        self.assertLess(51 + (age - 3) // 30, 60)
        self.assertGreaterEqual(decision['level'], 10)
        self.assertEqual(decision['trial']['outcome'], 'failed')
        self.assertEqual(decision['trial']['outcome'], 'failed')

    def test_persistent_warming_holds_then_settled_plateau_allows_first_probe(self):
        controller, actual = Controller(HOSTS), 16
        held = False
        for now in range(0, 903, 3):
            decision = self.step(controller, now, actual, fleet(now, cpu=46 + .03 * min(now, 240)))
            actual = decision['level']
            held |= decision['scenario'] == 'warming_hold'
            if actual == 9:
                break
        self.assertEqual(actual, 9)
        self.assertEqual(decision['trial']['status'], 'pending')
        self.assertTrue(held)

    def test_projected_margin_loss_blocks_success_without_aborting_safe_observation(self):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 1203, 3):
            decision = self.step(controller, now, actual, fleet(now, cpu=57))
            actual = decision['level']
            if actual == 9:
                break
        self.assertEqual(actual, 9)
        requested = now
        for age in range(3, 304, 3):
            now = requested + age
            decision = self.step(controller, now, 9, fleet(now, cpu=57.1))
            if age < 303:
                self.assertEqual(decision['level'], 9)
                self.assertIsNone(decision['trial']['outcome'])
        self.assertEqual(decision['level'], 10)
        self.assertEqual(decision['trial']['outcome'], 'inconclusive_timeout')

    def test_background_readiness_never_supplies_strict_learning(self):
        controller, actual = Controller(HOSTS), 16
        for now in range(0, 2403, 3):
            decision = controller.decide(background(now), now, minimum_level=9, feedback=feedback(now, actual))
            self.assertFalse(decision['learning_eligible'])
            actual = decision['level']
        self.assertEqual(actual, 9)
        for elapsed in range(3, 121, 3):
            now = 2400 + elapsed
            decision = controller.decide(fleet(now, power=50), now, minimum_level=9, feedback=feedback(now, 9))
            self.assertFalse(decision['learning_eligible'])
        for now in range(2523, 2734, 3):
            decision = controller.decide(fleet(now, power=50), now, minimum_level=9, feedback=feedback(now, 9))
        self.assertTrue(decision['learning_eligible'])

    def test_cold_background_can_settle_and_continue_to_zero_without_fixed_floor(self):
        controller, actual = Controller(HOSTS), 16
        successes = set()
        for now in range(0, 8103, 3):
            decision = controller.decide(background(now), now, minimum_level=0,
                                         feedback=feedback(now, actual))
            self.assertGreaterEqual(decision['level'], 0)
            if decision['level'] < actual:
                self.assertEqual(decision['level'], actual - 1)
            trial = decision.get('trial')
            if trial and trial['outcome'] == 'success':
                successes.add(trial['to'])
            actual = decision['level']
        self.assertTrue({9, 8}.issubset(successes), successes)
        self.assertEqual(actual, 0)
        self.assertNotEqual((decision['trial'] or {}).get('outcome'), 'failed')

    def test_decelerating_rebound_does_not_restore_routine_floor_after_success(self):
        controller, actual = Controller(HOSTS), 16
        first_low = None
        successes = set()
        for now in range(0, 3603, 3):
            if actual < 10 and first_low is None:
                first_low = now
            cpu = 51 if first_low is None else 52 - math.exp(-(now - first_low) / 20)
            decision = controller.decide(background(now, cpu=cpu), now, minimum_level=0,
                                         feedback=feedback(now, actual))
            if first_low is not None:
                self.assertLess(decision['level'], 10, decision['scenario_detail'])
            trial = decision.get('trial')
            if trial and trial['outcome'] == 'success':
                successes.add(trial['to'])
            actual = decision['level']
        self.assertIsNotNone(first_low, 'background experiment was never admitted')
        self.assertTrue({9, 8}.issubset(successes), successes)


if __name__ == '__main__':
    unittest.main()
