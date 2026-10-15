import unittest
from dataclasses import replace
from unittest.mock import patch
import service
from core import Controller
from test_core import HOSTS
from test_scenarios import fleet, feedback
from test_service import FakeFan


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

    def test_load_change_resets_qualification_and_power_reserve_remains(self):
        c=self.prepared()
        for t in range(63,88,3):d=self.step(c,t,10)
        d=self.step(c,90,10,load=20)
        self.assertEqual(d["level"],10)
        self.assertEqual(c._policy["state"]["earned"],0)
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

    def test_uncommanded_low_actual_restores_previous_request(self):
        c=self.prepared()
        d=self.step(c,63,4)
        self.assertEqual(d['level'],10)
        self.assertFalse(d['learning_eligible'])


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
