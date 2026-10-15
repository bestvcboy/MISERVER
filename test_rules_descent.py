import unittest
from dataclasses import replace
from unittest.mock import patch
import service
from core import Controller
from test_core import HOSTS
from test_scenarios import fleet, feedback
from test_service import FakeFan


# Whitelisted observed CPU/GPU percentages and W, hosts 180/181/183/184, 3 second cadence.
CAPTURED_WORKLOAD_20261015 = [[[5.4, 0.0, 59.3], [1.0, 0.0, 52.12], [1.2, 0.0, 49.29], [0.6, 0.0, 45.84]],
 [[8.4, 0.0, 52.85], [2.4, 0.0, 56.51], [2.8, 0.0, 55.76], [1.8, 0.0, 54.67]],
 [[6.8, 0.0, 46.99], [4.4, 0.0, 50.06], [2.9, 0.0, 42.81], [7.4, 0.0, 52.87]],
 [[5.7, 0.0, 56.67], [2.6, 0.0, 52.16], [3.0, 0.0, 47.99], [2.8, 0.0, 48.98]],
 [[6.0, 0.0, 48.39], [7.5, 0.0, 64.3], [3.7, 0.0, 59.26], [4.1, 0.0, 57.2]],
 [[8.5, 0.0, 42.96], [4.4, 0.0, 50.57], [3.7, 0.0, 54.93], [7.3, 0.0, 59.74]],
 [[7.0, 0.0, 58.73], [2.4, 0.0, 47.22], [2.9, 0.0, 45.9], [3.3, 0.0, 52.7]],
 [[5.8, 0.0, 75.47], [1.0, 0.0, 45.34], [3.4, 0.0, 52.35], [2.6, 0.0, 42.92]],
 [[7.0, 0.0, 54.73], [1.2, 0.0, 51.92], [6.0, 0.0, 46.46], [0.8, 0.0, 43.91]],
 [[8.3, 0.0, 57.37], [3.6, 0.0, 56.99], [3.7, 0.0, 53.85], [3.6, 0.0, 55.11]],
 [[4.2, 0.0, 47.72], [7.3, 0.0, 65.05], [1.2, 0.0, 57.3], [7.4, 0.0, 54.29]],
 [[6.8, 0.0, 60.68], [2.4, 0.0, 53.91], [2.6, 0.0, 55.06], [1.1, 0.0, 50.44]],
 [[3.7, 0.0, 59.88], [3.5, 0.0, 49.6], [5.4, 0.0, 46.45], [3.1, 0.0, 41.52]],
 [[6.5, 0.0, 56.39], [2.3, 0.0, 47.24], [4.7, 0.0, 46.4], [2.6, 0.0, 49.39]],
 [[10.9, 0.0, 58.15], [2.0, 0.0, 45.87], [2.2, 0.0, 41.25], [2.2, 0.0, 40.01]],
 [[5.5, 0.0, 55.52], [0.5, 0.0, 49.86], [12.1, 0.0, 46.2], [0.7, 0.0, 45.45]],
 [[6.3, 0.0, 59.78], [5.4, 0.0, 40.6], [3.3, 0.0, 44.18], [2.7, 0.0, 49.64]],
 [[10.0, 0.0, 48.4], [5.4, 0.0, 46.89], [1.9, 0.0, 48.79], [1.1, 0.0, 51.31]],
 [[7.0, 0.0, 54.52], [3.1, 0.0, 48.64], [8.6, 0.0, 60.06], [5.7, 0.0, 43.53]],
 [[2.6, 0.0, 45.26], [5.3, 0.0, 53.53], [7.5, 0.0, 50.38], [3.3, 0.0, 51.41]],
 [[3.4, 0.0, 52.3], [3.9, 0.0, 41.56], [2.0, 0.0, 45.54], [4.7, 0.0, 50.07]],
 [[7.3, 0.0, 47.43], [3.0, 0.0, 41.46], [6.1, 0.0, 44.7], [5.6, 0.0, 44.37]],
 [[9.2, 0.0, 57.76], [1.6, 0.0, 51.94], [1.9, 0.0, 46.98], [1.6, 0.0, 50.08]],
 [[3.4, 0.0, 60.1], [1.0, 0.0, 50.74], [1.6, 0.0, 51.09], [1.0, 0.0, 47.76]],
 [[8.4, 0.0, 42.61], [1.5, 0.0, 46.34], [3.7, 0.0, 49.69], [2.0, 0.0, 48.73]],
 [[7.1, 0.0, 56.06], [3.8, 0.0, 44.2], [3.8, 0.0, 43.01], [3.4, 0.0, 42.47]],
 [[3.1, 0.0, 49.58], [4.9, 0.0, 47.17], [1.8, 0.0, 50.09], [1.8, 0.0, 50.02]],
 [[11.4, 0.0, 53.46], [2.3, 0.0, 43.59], [5.8, 0.0, 45.15], [5.2, 0.0, 48.94]],
 [[6.4, 0.0, 52.93], [5.2, 0.0, 54.42], [5.0, 0.0, 57.78], [5.0, 0.0, 51.93]],
 [[6.5, 0.0, 52.81], [0.5, 0.0, 46.19], [0.7, 0.0, 45.73], [0.2, 0.0, 53.0]],
 [[3.9, 0.0, 45.42], [4.4, 0.0, 60.4], [3.8, 0.0, 51.08], [3.4, 0.0, 49.93]],
 [[7.3, 0.0, 50.4], [2.1, 0.0, 51.77], [2.0, 0.0, 48.32], [1.9, 0.0, 44.02]],
 [[4.8, 0.0, 55.3], [4.8, 0.0, 47.9], [2.1, 0.0, 43.78], [5.6, 0.0, 48.42]]]


class RulesDescentTests(unittest.TestCase):
    def step(self, c, t, actual, floor=3, **values):
        return c.decide(fleet(t, cpu=values.pop('cpu',49), gpu=values.pop('gpu',47),
                             board=values.pop('board',59), power=values.pop('power',60), **values),
                        t, minimum_level=floor, feedback=feedback(t,actual))

    def prepared(self, actual=10, **values):
        c=Controller(HOSTS,rules_only=True)
        for t in range(0,61,3):
            self.step(c,t,16,**values)
        c.level=actual
        c._policy['state']=dict(tag='hold')
        return c

    def test_ten_to_three_in_150_seconds_with_real_next_poll_confirmation(self):
        c=self.prepared();actual=10;edges=[];confirmed_at=None
        for t in range(63,334,3):
            d=self.step(c,t,actual)
            self.assertFalse(d['learning_eligible'])
            if d['level']<actual:
                edges.append((t,actual,d['level']))
                self.assertEqual(d['execution_trial']['planned_step'],actual-d['level'])
            if actual==3 and confirmed_at is None: confirmed_at=t
            actual=d['level']
            self.assertGreaterEqual(actual,3)
        self.assertEqual([(a,b) for _,a,b in edges],[(10,8),(8,6),(6,4),(4,3)])
        self.assertLessEqual(confirmed_at-63,150)
        self.assertTrue(all(b[0]-a[0]>=30 for a,b in zip(edges,edges[1:])))
        self.assertEqual(d['execution_trial']['planned_step'],1)

    def test_zero_is_reachable_without_crossing_reserve(self):
        c=self.prepared();actual=10
        for t in range(63,360,3):
            d=self.step(c,t,actual,floor=0)
            self.assertGreaterEqual(d["level"],0)
            actual=d["level"]
        self.assertEqual(actual,0)
        self.assertFalse(d["learning_eligible"])

    def test_headroom_tiers_and_manual_adjacent(self):
        for board,cpu,gpu,step,delay in ((54,49,47,3,30),(59,49,47,2,30),(61,49,47,1,60),(62.1,49,47,0,120)):
            with self.subTest(board=board):
                c=self.prepared(board=board,cpu=cpu,gpu=gpu)
                first=None
                for t in range(63,190,3):
                    d=self.step(c,t,10,board=board,cpu=cpu,gpu=gpu)
                    if d['level']<10:
                        first=t;break
                self.assertEqual(d['level'],10-step)
                if step:self.assertGreaterEqual(first-63,delay)
        c=self.prepared(board=50,cpu=45,gpu=40)
        for t in range(63,100,3):
            d=c.decide(fleet(t,cpu=45,gpu=40,board=50,power=60),t,minimum_level=3,
                       manual_level=3,feedback=feedback(t,10))
        self.assertEqual(d['level'],9)
        self.assertEqual(d['execution_trial']['planned_step'],1)

    def test_falling_temperatures_are_eligible(self):
        c=self.prepared(board=59)
        for t in range(63,100,3):d=self.step(c,t,10,board=59-(t-60)*.04)
        self.assertEqual(d['level'],8)

    def test_pending_warning_revokes_jump_and_deadline_wins(self):
        c=self.prepared()
        for t in range(63,94,3):d=self.step(c,t,10)
        deadline=d['execution_trial']['original_deadline']
        d=self.step(c,96,10,power=81)
        self.assertEqual(d['level'],10)
        self.assertEqual(d['execution_trial']['original_deadline'],deadline)
        d=self.step(c,deadline,10)
        self.assertEqual(d['level'],16)
        self.assertIn('execution_timeout',d['scenario_detail'])

    def test_pending_tier_or_identity_loss_preserves_original_deadline(self):
        for original, changed in ((55,55.1),(60,60.1)):
            c=self.prepared(board=original)
            for t in range(63,94,3):d=self.step(c,t,10,board=original)
            deadline=d["execution_trial"]["original_deadline"]
            d=self.step(c,96,10,board=changed)
            self.assertEqual(d["level"],10)
            self.assertEqual(d["execution_trial"]["original_deadline"],deadline)
        c=self.prepared()
        for t in range(63,94,3):d=self.step(c,t,10)
        deadline=d["execution_trial"]["original_deadline"]
        nodes=fleet(96,cpu=49,gpu=47,board=59,power=60)
        nodes[0]["hostname"]="replaced"
        d=c.decide(nodes,96,minimum_level=3,feedback=feedback(96,10))
        self.assertEqual(d["level"],10)
        self.assertEqual(d["execution_trial"]["original_deadline"],deadline)

    def test_single_load_sample_keeps_credit_and_raw_power_reserve_remains(self):
        c=self.prepared()
        for t in range(63,88,3):d=self.step(c,t,10)
        d=self.step(c,90,10,load=20)
        self.assertEqual(d["level"],10)
        self.assertEqual(c._policy["state"]["earned"],27)
        for t in (93,96,99):d=self.step(c,t,8,power=81)
        self.assertGreaterEqual(d["level"],10)

    def test_mild_bump_has_no_rebound_and_danger_restores_entire_safe_edge(self):
        c=self.prepared()
        for t in range(63,94,3):d=self.step(c,t,10)
        d=self.step(c,96,8)
        self.assertEqual(d["trial"]["outcome"],"recovery_released")
        for t in range(99,133,3):
            d=self.step(c,t,8,board=59.3 if t==99 else 59)
            self.assertNotEqual(d["scenario"],"rebound")
        self.assertEqual(d["level"],6)
        c=self.prepared()
        for t in range(63,94,3):d=self.step(c,t,10)
        self.step(c,96,8)
        for t in range(99,127,3):
            d=self.step(c,t,8,board=59+(t-96)*.15)
            if d["level"]>8:break
        self.assertGreaterEqual(d["level"],10)
        self.assertFalse(d["learning_eligible"])

    def test_target_minus_two_plateau_restores_complete_previous_safe_edge(self):
        c=self.prepared()
        for t in range(63,94,3):self.step(c,t,10)
        d=self.step(c,96,8)
        self.assertEqual(d["trial"]["outcome"],"recovery_released")
        for t in (99,102,105):
            d=self.step(c,t,8,cpu=58)
            self.assertGreaterEqual(d["level"],10)
            self.assertFalse(d["learning_eligible"])

    def test_captured_natural_load_jitter_qualifies_without_instantaneous_span_resets(self):
        c=self.prepared()
        requested=None
        reference=None
        for index, readings in enumerate(CAPTURED_WORKLOAD_20261015):
            t=63+index*3
            nodes=fleet(t,cpu=49,gpu=47,board=59,power=60)
            for node, (cpu,gpu,power) in zip(nodes,readings):
                node.update(cpu_load=cpu,gpu_load=gpu,power_w=power)
            d=c.decide(nodes,t,minimum_level=3,feedback=feedback(t,10))
            if d["level"]<10:
                requested=t
                break
            if index>0:
                self.assertIsNone(c._policy["state"].get("release_reset"))
                self.assertEqual(c._policy["state"]["workload_reference"],reference)
            else:
                reference=dict(c._policy["state"]["workload_reference"])
        self.assertEqual(requested,93)
        self.assertEqual(d["level"],8)
        self.assertFalse(d["learning_eligible"])

    def test_sustained_mean_increases_reset_credit_with_visible_reason(self):
        for metric in ("cpu_load","gpu_load","power_w"):
            with self.subTest(metric=metric):
                c=self.prepared(board=61)
                for t in range(63,85,3):self.step(c,t,10,board=61)
                reset=None
                for t in range(87,118,3):
                    nodes=fleet(t,cpu=49,gpu=47,board=61,power=60)
                    nodes[0][metric]=75 if metric=="power_w" else 25
                    d=c.decide(nodes,t,minimum_level=3,feedback=feedback(t,10))
                    if "均值增加" in d["scenario_detail"]:
                        reset=t
                        self.assertEqual(c._policy["state"]["earned"],0)
                        self.assertNotIn("继续观察",d["scenario_detail"])
                        break
                self.assertIsNotNone(reset)
                self.assertEqual(d["level"],10)

    def test_pending_sustained_mean_increase_revokes_without_extending_deadline(self):
        for metric in ("cpu_load","gpu_load","power_w"):
            with self.subTest(metric=metric):
                c=self.prepared()
                for t in range(63,94,3):d=self.step(c,t,10)
                deadline=d["execution_trial"]["original_deadline"]
                frozen=dict(c._policy["operation"]["reference"])
                revoked=False
                for t in range(96,130,3):
                    nodes=fleet(t,cpu=49,gpu=47,board=59,power=60)
                    nodes[0][metric]=75 if metric=="power_w" else 25
                    d=c.decide(nodes,t,minimum_level=3,feedback=feedback(t,10,confirmed=False,execution="pending"))
                    self.assertEqual(d["execution_trial"]["original_deadline"],deadline)
                    self.assertEqual(c._policy["operation"]["reference"],frozen)
                    if d["level"]>=10:
                        revoked=True
                        self.assertTrue(any("均值增加" in reason for reason in d["reasons"]))
                        break
                self.assertTrue(revoked)

    def test_falling_mean_does_not_reset_credit_or_revoke_pending_jump(self):
        c=self.prepared(load=20,power=75)
        for t in range(63,94,3):
            d=self.step(c,t,10,load=0,power=50)
        self.assertEqual(d["level"],8)
        deadline=d["execution_trial"]["original_deadline"]
        for t in (96,99,102):
            d=c.decide(fleet(t,cpu=49,gpu=47,board=59,power=40,load=0),t,
                       minimum_level=3,feedback=feedback(t,10,confirmed=False,execution="pending"))
            self.assertEqual(d["level"],8)
            self.assertEqual(d["execution_trial"]["original_deadline"],deadline)

    def test_missing_workload_coverage_resets_reference_and_reports_pause(self):
        c=self.prepared()
        for t in range(63,88,3):self.step(c,t,10)
        with patch.object(c,"_workload_mean",return_value=None):d=self.step(c,90,10)
        self.assertEqual(c._policy["state"]["earned"],0)
        self.assertIsNone(c._policy["state"]["workload_reference"])
        self.assertIn("覆盖缺失",d["scenario_detail"])
        self.assertNotIn("继续观察",d["scenario_detail"])
        d=self.step(c,93,10)
        self.assertEqual(c._policy["state"]["earned"],0)

    def test_raw_load_pause_and_source_change_do_not_wait_for_means(self):
        c=self.prepared()
        for t in range(63,88,3):self.step(c,t,10)
        d=self.step(c,90,10,load=30)
        self.assertEqual(d["level"],10)
        self.assertIsNone(c._policy["state"]["last"])
        nodes=fleet(93,cpu=49,gpu=47,board=59,power=60)
        nodes[0]["power_source"]="replacement"
        d=c.decide(nodes,93,minimum_level=3,feedback=feedback(93,10))
        self.assertEqual(c._policy["state"]["earned"],0)
        self.assertIsNone(c._policy["state"]["workload_reference"])
        self.assertIn("覆盖缺失",d["scenario_detail"])

    def test_uncommanded_low_actual_restores_previous_request(self):
        c=self.prepared()
        d=self.step(c,63,4)
        self.assertEqual(d['level'],10)
        self.assertFalse(d['learning_eligible'])


class PredictiveReleaseTests(unittest.TestCase):
    def test_slow_warming_far_below_target_releases_after_thirty_seconds(self):
        c=Controller(HOSTS,rules_only=True)
        actual=16
        edges=[]
        for t in range(0,154,3):
            d=c.decide(fleet(t,cpu=40+t*.03,gpu=38,board=50,power=45),t,
                       minimum_level=0,feedback=feedback(t,actual))
            self.assertFalse(d["learning_eligible"])
            if d["level"]!=actual:
                edges.append((t,actual,d["level"]))
            actual=d["level"]
        self.assertTrue(edges,"low-temperature warming never received a release")
        self.assertEqual(edges[0],(90,16,13))
        self.assertTrue(all(b[0]-a[0]>=30 for a,b in zip(edges,edges[1:])))
        self.assertTrue(all(0<a-b<=3 for _,a,b in edges))

    def test_far_below_target_persistent_two_degree_rise_does_not_restore_full_edge(self):
        c=Controller(HOSTS,rules_only=True)
        actual=16
        for t in range(0,151,3):
            d=c.decide(fleet(t,cpu=40+max(0,t-93)*.04,gpu=38,board=50,power=45),t,
                       minimum_level=0,feedback=feedback(t,actual))
            if t>93:self.assertLessEqual(d["level"],13)
            actual=d["level"]
        self.assertLess(actual,13)

    def with_frame(self, controller, adjust, decide):
        advance=controller._advance
        def boundary(frame):
            adjust(frame)
            return advance(frame)
        with patch.object(controller,"_advance",side_effect=boundary):
            return decide()

    def test_exact_five_allows_warming_and_below_five_pauses_earned_credit(self):
        helper=RulesDescentTests()
        c=helper.prepared()
        for t in range(63,88,3):helper.step(c,t,10)
        earned=c._policy["state"]["earned"]
        def adjust(frame):frame.update(margin=4.99,warming=True)
        d=self.with_frame(c,adjust,lambda:helper.step(c,90,10))
        self.assertEqual(d["level"],10)
        self.assertEqual(c._policy["state"]["earned"],earned)
        self.assertIn("预测余量不足5C",d["scenario_detail"])
        def enough(frame):frame.update(margin=5,warming=True)
        for t in (93,96,99):d=self.with_frame(c,enough,lambda:helper.step(c,t,10))
        self.assertEqual(d["level"],8)
        self.assertEqual(d["execution_trial"]["planned_step"],2)

    def test_pending_warming_retains_exact_original_tier_and_deadline(self):
        helper=RulesDescentTests()
        for board,margin,expected in ((54,10,7),(54,9.99,10),(59,5,8),(59,4.99,10)):
            with self.subTest(board=board,margin=margin):
                c=helper.prepared(board=board)
                for t in range(63,94,3):d=helper.step(c,t,10,board=board)
                deadline=d["execution_trial"]["original_deadline"]
                def adjust(frame):frame.update(margin=margin,warming=True,persistent_heat=True)
                d=self.with_frame(c,adjust,lambda:helper.step(c,96,10,board=board))
                self.assertEqual(d["level"],expected)
                self.assertEqual(d["execution_trial"]["original_deadline"],deadline)
                self.assertFalse(d["learning_eligible"])

    def test_secondary_restore_uses_same_covered_sensor_and_strict_five_degree_proximity(self):
        helper=RulesDescentTests()
        for projected,covered,temperature,expected in ((60,True,59,8),(60.01,True,59,10),
                                                        (60.01,False,59,8),(60.01,True,58,8)):
            with self.subTest(projected=projected,covered=covered,temperature=temperature):
                c=helper.prepared(board=56)
                for t in range(63,94,3):helper.step(c,t,10,board=56)
                helper.step(c,96,8,board=56)
                def adjust(frame):
                    for sensor in frame["evidence"].values():
                        if sensor["group"]=="board":
                            sensor.update(temp=temperature,projected=projected,persistent_heat=True,covered=covered)
                d=self.with_frame(c,adjust,lambda:helper.step(c,99,8,board=56))
                self.assertEqual(d["level"],expected)
        c=helper.prepared(board=56)
        for t in range(63,94,3):helper.step(c,t,10,board=56)
        helper.step(c,96,8,board=56)
        def unrelated(frame):
            for sensor in frame["evidence"].values():
                if sensor["group"]=="cpu":
                    sensor.update(temp=52,projected=52,persistent_heat=True)
                elif sensor["group"]=="board":
                    sensor.update(projected=60.01,persistent_heat=False)
        d=self.with_frame(c,unrelated,lambda:helper.step(c,99,8,board=56))
        self.assertEqual(d["level"],8)

    def test_accepted_near_target_acceleration_and_hard_temperature_remain_protected(self):
        helper=RulesDescentTests()
        c=helper.prepared(actual=16,cpu=40,gpu=38,board=50,power=45)
        for t in range(63,94,3):d=helper.step(c,t,16,cpu=40,gpu=38,board=50,power=45)
        self.assertEqual(d["level"],13)
        helper.step(c,96,13,cpu=40,gpu=38,board=50,power=45)
        for t,cpu in ((99,42.4),(102,43.9),(105,45.4)):
            d=helper.step(c,t,13,cpu=cpu,gpu=38,board=50,power=45)
        self.assertEqual(d["level"],16)
        self.assertEqual(d["execution_trial"]["purpose"],"rollback")
        c=helper.prepared()
        d=helper.step(c,63,10,cpu=73)
        self.assertEqual(d["level"],16)
        self.assertEqual(d["scenario"],"protect")

    def test_real_accepted_hard_prediction_remains_immediate(self):
        helper=RulesDescentTests()
        c=helper.prepared(cpu=40,gpu=38,board=50,power=45)
        protected=None
        for t in range(63,91,3):
            cpu=60+(t-63)*.3
            d=helper.step(c,t,10,cpu=cpu,gpu=38,board=50,power=45)
            if d["scenario"]=="protect":
                protected=cpu
                break
        self.assertIsNotNone(protected)
        self.assertLess(protected,70)
        self.assertEqual(d["level"],16)
        self.assertTrue(any("[hard]" in reason for reason in d["reasons"]))

    def test_saved_accepted_59_640_projection_restores_even_at_actual_45_4(self):
        helper=RulesDescentTests()
        c=helper.prepared()
        for t in range(63,94,3):helper.step(c,t,10)
        helper.step(c,96,8)
        def accepted(frame):
            for sensor in frame["evidence"].values():
                if sensor["group"]=="cpu":
                    sensor.update(temp=45.4,projected=59.640,covered=True,persistent_heat=False)
        d=self.with_frame(c,accepted,lambda:helper.step(c,99,8,cpu=45.4))
        self.assertEqual(d["level"],10)
        self.assertEqual(d["execution_trial"]["purpose"],"rollback")

    def test_manual_and_legacy_learning_mode_remain_single_step(self):
        for rules,manual in ((True,3),(False,None)):
            with self.subTest(rules=rules,manual=manual):
                c=Controller(HOSTS,rules_only=rules)
                actual=16
                edges=[]
                for t in range(0,154,3):
                    d=c.decide(fleet(t,cpu=40+t*.03,gpu=38,board=50,power=45),t,
                               minimum_level=0 if manual is None else manual,
                               manual_level=manual,feedback=feedback(t,actual))
                    if d["level"]<actual:edges.append(actual-d["level"])
                    actual=d["level"]
                self.assertTrue(edges)
                self.assertTrue(all(distance==1 for distance in edges))

    def test_raw_load_revokes_even_with_large_predicted_margin(self):
        helper=RulesDescentTests()
        c=helper.prepared(cpu=43,gpu=41,board=51,power=45)
        for t in range(63,94,3):d=helper.step(c,t,10,cpu=43,gpu=41,board=51,power=45)
        deadline=d["execution_trial"]["original_deadline"]
        d=helper.step(c,96,10,cpu=43,gpu=41,board=51,power=45,load=30)
        self.assertEqual(d["level"],10)
        self.assertEqual(d["execution_trial"]["original_deadline"],deadline)


class AuthorizedMotorTests(unittest.TestCase):
    def setUp(self):
        self.now=100
        self.clock=patch('service.time.monotonic',lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
        self.request=service.FanRequest(8,release=service.ReleaseAuthority(10,8,2,90,150))

    def run_request(self,request,actual=None,schedule=None):
        fan=FakeFan(actual or ['on','favorite',10,1500])
        result=service.fan_apply(fan,request if callable(request) else lambda:request,
                                 schedule or service.MotorSchedule())
        return fan,result

    def test_authorized_jump_is_one_write_and_thirty_second_guard_remains(self):
        schedule=service.MotorSchedule()
        fan,d=self.run_request(self.request,schedule=schedule)
        self.assertEqual(fan.writes,[8]);self.assertTrue(d['confirmed'])
        self.now=109
        req=service.FanRequest(6,release=service.ReleaseAuthority(8,6,2,103,163))
        fan,d=self.run_request(req,['on','favorite',8,1500],schedule)
        self.assertEqual(fan.writes,[])

    def test_three_step_and_scalar_adjacent(self):
        fan,_=self.run_request(service.FanRequest(7,release=service.ReleaseAuthority(10,7,3,90,150)))
        self.assertEqual(fan.writes,[7])
        fan,_=self.run_request(7);self.assertEqual(fan.writes,[9])

    def test_invalid_or_expired_authority_is_adjacent(self):
        for authority in (replace(self.request.release,requested_at=float('nan')),
                          replace(self.request.release,from_level=9),
                          replace(self.request.release,original_deadline=100),
                          replace(self.request.release,max_drop=True),
                          replace(self.request.release,destination=7)):
            with self.subTest(authority=authority):
                fan,_=self.run_request(replace(self.request,release=authority))
                self.assertEqual(fan.writes,[] if authority.original_deadline == 100 else [9])
        fan,_=self.run_request(self.request,['on','favorite',9,1500]);self.assertEqual(fan.writes,[8])

    def test_whole_request_refresh_revokes_same_destination_and_does_not_borrow_new_episode(self):
        for newer,expected in ((service.FanRequest(8),[9]),
                               (replace(self.request,release=replace(self.request.release,requested_at=95,original_deadline=155)),[9]),
                               (service.FanRequest(10,protected=True),[]),
                               (service.FanRequest(8,read_only_floor=10),[]),
                               (service.FanRequest(9),[9]),
                               (service.FanRequest(7,release=service.ReleaseAuthority(10,7,3,95,155)),[9])):
            with self.subTest(newer=newer):
                count=0
                def target():
                    nonlocal count
                    count+=1
                    return self.request if count<=3 else newer
                fan,_=self.run_request(target)
                self.assertEqual(fan.writes,expected)

    def test_authority_expiring_at_final_refresh_prevents_downward_write(self):
        calls=0
        def target():
            nonlocal calls
            calls+=1
            if calls==4:self.now=150
            return self.request
        fan,_=self.run_request(target)
        self.assertEqual(fan.writes,[])

    def test_persistence_failure_prevents_authorized_motor_write(self):
        schedule=service.MotorSchedule()
        with patch.object(schedule, "persist_debt_then_set_budget", return_value=False):
            fan,result=self.run_request(self.request,schedule=schedule)
        self.assertEqual(fan.writes,[])
        self.assertEqual(result["execution"],"fault")

    def test_zero_rpm_or_wrong_mode_has_no_jump_authority(self):
        for actual in (['on','favorite',10,0],['off','favorite',10,1500],['on','auto',10,1500]):
            fan,_=self.run_request(self.request,actual)
            self.assertIn(9,fan.writes)
            self.assertNotIn(8,fan.writes)


if __name__=='__main__':unittest.main()
