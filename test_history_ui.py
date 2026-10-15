import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from core import Journal


class HistoryUITests(unittest.TestCase):
    def test_history_verification_fields_are_additive_whitelisted_and_legacy_safe(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            trial = dict(requested_at=90, original_deadline=150, destination_requested_at=93,
                         deadline=150, status='pending', **{'from':5, 'to':4})
            execution = dict(requested_at=96, original_deadline=156, destination_requested_at=99,
                             deadline=156, status='pending', original_from=4, original_to=10,
                             **{'from':4, 'to':12})
            completeness = {'host-a': True, 'host-b': False, 'host-c': True, 'host-d': False}
            row = dict(ts=100, snapshot_at=101, learning=dict(mono=95, healthy=True, dropped_records=2, token='private'),
                       decision=dict(trial=trial, execution_trial=execution,
                                     trend_complete=completeness, secret='private'),
                       fan=dict(observed_mono=94, verified_at=100, token='private'),
                       nodes=[dict(host='h', sampled_at=100, source_ts=99.5, requested_mono=94, password='private')])
            journal.append(row)
            journal.append(dict(ts=102, fan=dict(verified_at=102), nodes=[dict(host='h', sampled_at=102)]))
            samples = journal.history(0, 103)['samples']
            self.assertEqual(samples[1]['learning'], dict(mono=95, healthy=True, dropped_records=2))
            self.assertEqual(samples[1]['fan'], dict(observed_mono=94, verified_at=100))
            self.assertEqual(samples[1]['nodes'], [dict(host='h', sampled_at=100, source_ts=99.5, requested_mono=94)])
            self.assertEqual(samples[1]['decision']['trial'], trial)
            self.assertEqual(samples[1]['decision']['execution_trial'], execution)
            self.assertEqual(samples[1]['decision']['trend_complete'], completeness)
            self.assertEqual(len(samples[1]['decision']['trend_complete']), 4)
            self.assertTrue(all(type(value) is bool for value in samples[1]['decision']['trend_complete'].values()))
            self.assertNotIn('trend_complete', samples[0].get('decision', {}))
            self.assertNotIn('execution_trial', samples[0].get('decision', {}))
            self.assertNotIn('private', json.dumps(samples))
            self.assertNotIn('learning', samples[0])
            self.assertEqual(samples[0]['fan'], dict(verified_at=102))
            self.assertEqual(samples[0]['nodes'], [dict(host='h', sampled_at=102)])
            self.assertEqual(json.loads(journal.current.read_text().splitlines()[0]), row)
            incomplete = dict.fromkeys(completeness, False)
            journal.append(dict(ts=102.5, decision=dict(trend_complete=incomplete)))
            self.assertEqual(journal.history(0, 103)['samples'][0]['decision']['trend_complete'], incomplete)

    def test_static_policy_matches_controller_fourteen(self):
        root = Path(__file__).parent
        html = (root / 'ui.html').read_text(encoding='utf-8')
        for retired in ('CPU / GPU 58°C', '绝对硬线为 60°C / 75°C', 'CPU / GPU <b>60°C</b>'):
            self.assertNotIn(retired, html)
        for policy in ('CPU / GPU 60°C 准备至少 10 档', '同一可信传感器当前至少 70°C',
                       '75°C 硬线', '73°C 预缓冲', '≥10°C 观察 30 秒最多降 3 档',
                       '≥5°C 观察 30 秒最多降 2 档', '3–5°C 观察 60 秒降 1 档', '预测余量至少 5°C 时，升温趋势本身不阻止下降', '不计容量学习'):
            self.assertIn(policy, html)

    def test_ambient_history_projects_public_fields_and_preserves_raw_monotonic(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root)
            fan = {"ambient_c": 30.8, "ambient_source": "purifier:70632677:temp_dec",
                   "ambient_verified_at": 100, "ambient_observed_mono": 90, "token": "private"}
            journal.append({"ts": 100, "snapshot_at": 101, "fan": fan, "nodes": []})
            journal.append({"ts": 102, "fan": {"level": 10}, "nodes": []})
            history = journal.history(0, 103)
            self.assertEqual(history["samples"][0]["fan"], {"level": 10})
            self.assertEqual(history["samples"][1]["fan"], {"ambient_c": 30.8,
                             "ambient_source": "purifier:70632677:temp_dec", "ambient_verified_at": 100})
            self.assertNotIn("private", json.dumps(history))
            self.assertEqual(json.loads(journal.current.read_text().splitlines()[0])["fan"]["ambient_observed_mono"], 90)

    @unittest.skipUnless(shutil.which("node"), "Node is required for offline history UI checks")
    def test_exact_rows_freshness_dates_escaping_and_request_generation(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('ui.html', 'utf8');
class Element {
  constructor() { this.events = {}; this.value = ''; this.style = {}; this.children = []; this.innerHTML = ''; this.textContent = ''; }
  addEventListener(event, handler) { this.events[event] = handler; }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll() { return []; }
}
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
const get = id => elements.get(id);
get('hours').value = '1';
const requests = [], intervals = [];
const context = {document:{getElementById:get, querySelectorAll:()=>[]}, window:{addEventListener(){}},
  Option:function(text, value) { this.textContent = text; this.value = value; },
  setTimeout(){}, setInterval(fn){intervals.push(fn);}, clearTimeout(){}, AbortSignal:{timeout:()=>({})},
  fetch:(path)=>new Promise((resolve, reject)=>requests.push({path, resolve, reject}))};
vm.createContext(context);
const source = fs.readFileSync('ui.js', 'utf8');
vm.runInContext(source.slice(0, source.lastIndexOf('refresh(); history();')), context);
const evaluate = code => vm.runInContext(code, context);
assert.match(html, /value="168">最近 7 天/);
assert.match(html, /id="retention"[^>]+value="7"/);
assert.match(html, /id="capacity"[^>]+value="4096"/);
assert.ok(html.includes('当前暂停机器学习，已有学习参数不参与控制。按固定阈值、温度趋势与实际风力反馈自动控制。'));
assert.ok(html.includes('固定功耗阈值为 80 W'));
assert.ok(!html.includes('负载 30%'));
const safetyText = '有效请求包含温度与通信保护。保存设置后仍须等待受限执行和设备读回。';
context.status = {controller_age_seconds:0, server_time:102, updated_at:102,
  settings:{manual_level:8, retention_days:7, max_log_mb:4096}, requested_level:16,
  fan:{configured:true, confirmed:false, level:8, rpm:800, power:'on', mode:'favorite', verified_at:101, execution:'pending'},
  decision:{scenario:'future_scenario', scenario_detail:'观察 <img src=x onerror="bad()"> & \'回温\'', learning_eligible:false},
  nodes:[], log_bytes:0, dropped_records:0};
evaluate('render(status)');
assert.equal(get('policy-detail').textContent, '当前场景：观察 <img src=x onerror="bad()"> & \'回温\'。' + safetyText);
assert.equal(get('policy-detail').innerHTML, '');
assert.equal(get('policy').textContent, '已保存 · 手动 8 档');
assert.equal(get('desired').textContent, 16);
assert.equal(get('actual').textContent, '8 档');
assert.equal(get('execution').textContent, '最新请求待执行');
context.status.decision.trial = {from:5, to:4, status:'invalidated', outcome:'invalidated', requested_at:90};
context.status.decision.execution_trial = {from:4, to:10, status:'pending', outcome:null,
  requested_at:96, original_deadline:156, destination_requested_at:96, planned_step:2};
evaluate('render(status)');
assert.match(get('policy-detail').textContent, /当前执行 4→10 · pending.*上调请求.*原截止 156.000.*下降试验 5→4 · invalidated/);
assert.ok(!get('policy-detail').textContent.includes('计划下降'));
Object.assign(context.status.decision.execution_trial, {from:10, to:8, planned_step:2});
evaluate('render(status)');
assert.match(get('policy-detail').textContent, /当前执行 10→8 · pending.*计划下降 2 档/);
Object.assign(context.status.decision.execution_trial, {from:4, to:10});
context.status.decision.execution_trial.status = 'confirmed';
evaluate('render(status)');
assert.match(get('policy-detail').textContent, /当前执行 4→10 · confirmed/);
delete context.status.decision.trial;
delete context.status.decision.execution_trial;
evaluate('render(status)');
assert.match(get('ambient').textContent, /净化器本地温度未记录/);
const ambientSource = 'purifier:<img src=x onerror="bad()">&\'';
Object.assign(context.status.fan, {ambient_c:30.8, ambient_source:ambientSource, ambient_verified_at:101});
evaluate('render(status)');
assert.match(get('ambient').textContent, /净化器本地温度 30.8 °C.*未校准环境参考/);
assert.ok(get('ambient').textContent.includes(ambientSource));
assert.ok(get('ambient').textContent.includes(evaluate('historyTime(101)')));
assert.equal(get('ambient').innerHTML, '');
for (const [stamp, label] of [[93,'采样有效'], [92,'采样已过期'], [103,'采样时间异常'], [null,'采样时效未知']]) {
  context.status.fan.ambient_verified_at = stamp;
  evaluate('render(status)');
  assert.ok(get('ambient').textContent.includes(label));
}
context.status.fan.ambient_verified_at = 101;
for (const age of [7, Infinity, -1, null, undefined]) {
  context.status.controller_age_seconds = age;
  evaluate('render(status)');
  assert.match(get('ambient').textContent, /当前不可确认/);
  assert.ok(!get('ambient').textContent.includes('采样有效'));
}
context.status.controller_age_seconds = 0;
for (const stamp of [null, undefined, NaN, Infinity]) {
  context.status.server_time = stamp;
  evaluate('render(status)');
  assert.match(get('ambient').textContent, /采样时效未知/);
}
context.status.server_time = 102;
evaluate('render(status)');
const motorAlarm = get('alarm').textContent;
const saved = JSON.parse(JSON.stringify(context.status));
context.status.nodes = Array.from({length:4}, (_, i)=>({host:['192.168.3.180','192.168.3.181','192.168.3.183','192.168.3.184'][i],
  online:true, sampled_at:102, cpu_temp:48, gpu_temp:44, board_temp:53}));
Object.assign(context.status, {requested_level:8, decision:{scenario:'readback_hold', scenario_detail:'设备读回短暂中断，保持至少8档，暂停降档'}});
Object.assign(context.status.fan, {level:null, rpm:null, confirmed:false, execution:'pending', verified_at:null});
evaluate('render(status)');
assert.match(get('alarm').textContent, /通信暂时中断.*8 档.*暂停降档/);
assert.equal(get('actual').textContent, '未确认');
assert.equal(get('rpm').textContent, '尚无有效转速读回');
assert.ok(!get('alarm').textContent.includes('16 档全风请求已匹配'));
context.status = JSON.parse(JSON.stringify(saved));
context.status.nodes = Array.from({length:4}, (_, i)=>({host:['192.168.3.180','192.168.3.181','192.168.3.183','192.168.3.184'][i],
  online:true, sampled_at:102, cpu_temp:60, gpu_temp:40, board_temp:50}));
Object.assign(context.status, {requested_level:10, decision:{scenario:'graded_hold', limits:{cpu:[60,75],gpu:[60,75],board:[65,75]}}});
Object.assign(context.status.fan, {level:10, confirmed:true, execution:'matched', rpm:900});
evaluate('render(status)');
assert.match(get('alarm').textContent, /60°C 准备区.*请求 10，实际 10/);
assert.ok(!get('alarm').textContent.includes('全风请求已匹配'));
context.status.decision.scenario = 'protect';
evaluate('render(status)');
assert.match(get('alarm').textContent, /保护请求 10 档.*已匹配实际读回/);
context.status.requested_level = context.status.fan.level = 16;
evaluate('render(status)');
assert.match(get('alarm').textContent, /已匹配实际读回/);
context.status.fan.execution = 'pending';
evaluate('render(status)');
assert.match(get('alarm').textContent, /实际执行尚未确认/);
context.status.decision.limits.cpu = [66,80];
evaluate("rows = [{ts:100,nodes:[{host:'192.168.3.180',cpu_temp:60}]},{ts:103,nodes:[{host:'192.168.3.180',cpu_temp:61}]}]; draw()");
assert.match(get('chart').innerHTML, /80°C 绝对硬线 · 78°C 实温预缓冲/);
context.status = saved;
evaluate('render(status)');
for (const value of [undefined, null, NaN, Infinity, true, '30.8']) {
  context.status.fan.ambient_c = value;
  evaluate('render(status)');
  assert.match(get('ambient').textContent, /净化器本地温度未记录/);
  assert.equal(get('alarm').textContent, motorAlarm);
}
for (const detail of [undefined, null, '']) {
  context.status.decision.scenario_detail = detail;
  evaluate('render(status)');
  assert.equal(get('policy-detail').textContent, '当前场景：旧记录未标注场景。' + safetyText);
}
context.status.decision = {};
evaluate('render(status)');
assert.equal(get('policy-detail').textContent, '当前场景：旧记录未标注场景。' + safetyText);
context.learning = {published:{'192.168.3.180':{threshold_w:75, source:'spbm', version:1, generated_at:21600}},
  active:{'192.168.3.180':{}}, eligibility:{phase:'blocked', reason:'load'}, next_at:43200,
  candidate:{generated_at:21600, window_from:0, window_to:21600, hosts:{'192.168.3.180':{
    candidate_w:50, threshold_w:75, reason:'validated', train_buckets:19, train_seconds:17043,
    holdout_buckets:4, holdout_seconds:3588, gear:0, source:'spbm'}}}};
evaluate('renderLearning(learning)');
assert.match(get('learning-published').textContent, /已发布 75 W.*当前生效/);
assert.match(get('learning-candidate').textContent, /当前候选 50 W.*训练 19 桶.*后 1 小时 4 桶.*实际 0 档/);
assert.match(get('learning-schedule').textContent, /每 6 小时.*72 小时.*下次/);
context.learning.active = {};
evaluate('renderLearning(learning)');
assert.match(get('learning-published').textContent, /当前条件阻止激活/);
context.learning.enabled = false;
context.learning.active = {'192.168.3.180':{}};
evaluate('renderLearning(learning)');
assert.match(get('learning-state').textContent, /纯规则自动温控.*已暂停/);
assert.match(get('learning-published').textContent, /学习阈值不参与控制/);
assert.doesNotMatch(get('learning-published').textContent, /75 W|当前生效/);
assert.doesNotMatch(get('learning-candidate').textContent, /50 W|训练/);
assert.match(get('learning-schedule').textContent, /不训练、不评估、不发布/);
assert.doesNotMatch(get('learning-schedule').textContent, /每 6 小时|下次/);
assert.match(get('learning-storage').textContent, /原始温度、负载和风力历史继续记录/);
assert.equal(evaluate("powerLabel({host:'192.168.3.183', power_source:'estimate:gpu'})"), '仅 GPU 估计，不是整机功耗；不用于学习发布');
const sample = {ts:100, snapshot_at:102, control:{manual_level:null}, requested_level:0,
  fan:{configured:true, confirmed:true, level:0, rpm:800, power:'on', mode:'favorite', verified_at:101},
  decision:{level:0, reasons:['<img src=x onerror="bad()">', '& reason']},
  nodes:[{host:'192.168.3.180', online:true, sampled_at:100, cpu_temp:0, gpu_temp:null, board_temp:52},
    {host:'192.168.3.181', online:false, sampled_at:100, cpu_temp:null, gpu_temp:null, board_temp:null},
    {host:'192.168.3.183', online:true, sampled_at:80, cpu_temp:40, gpu_temp:41, board_temp:42,
      power_w:9, power_source:'estimate:gpu'}],
  active_power_policy:{'192.168.3.180':{version:1, threshold_w:75, source:'spbm', generated_at:0}}};
context.sample = sample;
context.payload = {rows:[], samples:[sample], samples_truncated:false, from:0, to:103};
const render = () => evaluate('renderHistory(payload)');
render();
assert.match(get('history-samples').innerHTML, /<td>自动<\/td><td>0<\/td><td>0 档/);
assert.match(get('history-samples').innerHTML, /记录时实际匹配/);
assert.match(get('history-samples').innerHTML, /距快照 1.0 秒/);
assert.match(get('history-samples').innerHTML, /0.0 \/ — \/ 52.0 °C/);
assert.match(get('history-samples').innerHTML, /离线 \/ 缺失/);
assert.match(get('history-samples').innerHTML, /采样已过期/);
assert.match(get('history-samples').innerHTML, /仅 GPU 估计/);
assert.match(get('history-samples').innerHTML, /整机 75 W.*发布版本 1/);
assert.match(get('history-samples').innerHTML, /节点记录缺失/);
assert.match(get('history-samples').innerHTML, /&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;/);
assert.match(get('history-samples').innerHTML, /&amp; reason/);
assert.ok(!get('history-samples').innerHTML.includes('<img'));
assert.match(get('history-samples').innerHTML, /净化器本地温度未记录/);
Object.assign(sample.fan, {ambient_c:0, ambient_source:ambientSource, ambient_verified_at:101});
context.status.server_time = 1000000;
evaluate('render(status)');
render();
assert.match(get('history-samples').innerHTML, /净化器本地温度 0.0 °C.*未校准环境参考/);
assert.match(get('history-samples').innerHTML, /来源 purifier:&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;&amp;&#39;/);
assert.ok(!get('history-samples').innerHTML.includes('<img'));
assert.equal((get('history-samples').innerHTML.match(/<td>/g) || []).length, 11);
for (const [stamp, label] of [[93,'采样有效'], [92,'采样已过期'], [103,'采样时间异常'], [null,'采样时效未知']]) {
  sample.fan.ambient_verified_at = stamp;
  render();
  assert.ok(get('history-samples').innerHTML.includes(label));
  assert.match(get('history-samples').innerHTML, /记录时实际匹配/);
}
sample.fan.ambient_verified_at = 101;
const snapshot = sample.snapshot_at;
delete sample.snapshot_at;
render();
assert.match(get('history-samples').innerHTML, /采样时效未知/);
sample.snapshot_at = snapshot;
delete sample.fan.ambient_c;
render();
assert.match(get('history-samples').innerHTML, /净化器本地温度未记录/);
assert.ok(get('history-samples').innerHTML.includes('<small>当前场景：旧记录未标注场景</small>'));
sample.decision.scenario = 'future_<svg onload="bad()">&\'';
sample.decision.scenario_detail = '正常回温 <img src=x onerror="bad()"> & \'观察\'';
sample.decision.learning_eligible = false;
sample.decision.trial = {from:5, to:4, status:'failed', outcome:'failed'};
sample.decision.execution_trial = {from:4, to:10, status:'pending', requested_at:90,
  original_deadline:150, destination_requested_at:93};
render();
assert.match(get('history-samples').innerHTML, /当前执行 4→10 · pending.*原截止 150.000.*下降试验 5→4 · failed/);
sample.decision.execution_trial.status = '<svg onload="bad()">';
render();
assert.ok(!get('history-samples').innerHTML.includes('<svg'));
assert.match(get('history-samples').innerHTML, /&lt;svg/);
delete sample.decision.trial;
delete sample.decision.execution_trial;
render();
assert.ok(get('history-samples').innerHTML.includes('<small>当前场景：正常回温 &lt;img src=x onerror=&quot;bad()&quot;&gt; &amp; &#39;观察&#39;</small>'));
assert.ok(!get('history-samples').innerHTML.includes('future_'));
assert.ok(!get('history-samples').innerHTML.includes('<img'));
for (const detail of [undefined, null, '']) {
  sample.decision.scenario_detail = detail;
  render();
  assert.ok(get('history-samples').innerHTML.includes('<small>当前场景：future_&lt;svg onload=&quot;bad()&quot;&gt;&amp;&#39;</small>'));
  assert.ok(!get('history-samples').innerHTML.includes('<svg'));
}
const savedDecision = sample.decision;
for (const decision of [undefined, {}, {scenario:'', scenario_detail:''}]) {
  sample.decision = decision;
  render();
  assert.ok(get('history-samples').innerHTML.includes('<td>未记录原因<small>当前场景：旧记录未标注场景</small>'));
}
sample.decision = savedDecision;
delete sample.decision.scenario;
render();
assert.ok(get('history-samples').innerHTML.includes(evaluate('historyTime(101)')));
assert.ok(get('history-note').textContent.includes(evaluate('historyTime(0)')));
assert.notEqual(evaluate('historyTime(0)'), '未知');
for (const stamp of [null, undefined, NaN, Infinity, -1]) {
  context.stamp = stamp;
  assert.equal(evaluate('historyTime(stamp)'), '未知');
}
for (const [stamp, label] of [[93,'记录时实际匹配'], [92,'读回已过期'], [103,'读回时间异常'], [null,'读回时效未知']]) {
  sample.fan.verified_at = stamp;
  render();
  assert.match(get('history-samples').innerHTML, new RegExp(label));
}
sample.fan.verified_at = 101;
for (const [field, value] of [['rpm',0], ['rpm',NaN], ['fault',true], ['confirmed',false], ['power','off'], ['mode','auto'], ['level',3], ['configured',false], ['execution','fault']]) {
  const original = sample.fan[field];
  sample.fan[field] = value;
  render();
  assert.ok(!get('history-samples').innerHTML.includes('记录时实际匹配'), field);
  sample.fan[field] = original;
}
sample.fan.mode = '<svg onload="bad()">';
sample.fan.error = '<script>bad()</script>';
render();
assert.match(get('history-samples').innerHTML, /&lt;svg/);
assert.match(get('history-samples').innerHTML, /&lt;script/);
sample.fan.mode = 'favorite';
delete sample.snapshot_at;
delete sample.control;
render();
assert.match(get('history-samples').innerHTML, /未知（旧记录未记录）/);
assert.match(get('history-samples').innerHTML, /读回时效未知/);
assert.match(get('history-samples').innerHTML, /采样时效未知/);
delete sample.fan;
render();
assert.match(get('history-samples').innerHTML, /<td>0<\/td><td>— 档/);
sample.control = {manual_level:0};
render();
assert.match(get('history-samples').innerHTML, /手动 0 档/);
context.payload.samples = Array.from({length:200}, ()=>sample);
context.payload.samples_truncated = true;
render();
assert.equal((get('history-samples').innerHTML.match(/<tr>/g) || []).length, 200);
assert.match(get('history-count').textContent, /显示 200 条/);
assert.match(get('history-count').textContent, /写入顺序倒序/);
assert.match(get('history-count').textContent, /已截断.*更早的结束时间/);
context.payload.samples = [];
context.payload.samples_truncated = false;
render();
assert.match(get('history-samples').innerHTML, /没有记录/);
assert.match(html, /type="datetime-local" step="1"/);
assert.equal((html.match(/<th scope="col">/g) || []).length, 11);
assert.match(html, /class="history-scroll" role="region" tabindex="0"/);
assert.match(get('control-mode').children[0].textContent, /按温度余量降档/);
const response = (marker) => ({ok:true, json:async()=>({rows:[], samples:[{...sample, decision:{reasons:[marker]}}], samples_truncated:false, from:0, to:100})});
(async()=>{
  const first = evaluate('history()');
  get('hours').value = '3';
  const second = get('hours').events.change();
  assert.match(requests[0].path, /hours=1$/);
  assert.match(requests[1].path, /hours=3$/);
  requests[1].resolve(response('newest'));
  await second;
  requests[0].resolve(response('obsolete'));
  await first;
  assert.match(get('history-samples').innerHTML, /newest/);
  assert.ok(!get('history-samples').innerHTML.includes('obsolete'));
  const lateError = evaluate('history()');
  const success = evaluate('history()');
  requests.at(-1).resolve(response('still newest'));
  await success;
  requests.at(-2).reject(Error('old request failure'));
  await lateError;
  assert.match(get('history-samples').innerHTML, /still newest/);
  const abandoned = evaluate('history()');
  get('history-until').value = '2020-01-02T03:04:05';
  get('history-until').events.input();
  requests.at(-1).resolve(response('abandoned'));
  await abandoned;
  assert.match(get('history-note').textContent, /请点击查询/);
  assert.ok(!get('history-samples').innerHTML.includes('abandoned'));
  const older = get('history-query').events.click();
  assert.equal(new URL('http://localhost' + requests.at(-1).path).searchParams.get('until'), String(new Date('2020-01-02T03:04:05').getTime() / 1000));
  requests.at(-1).resolve(response('older period'));
  await older;
  const beforeInvalid = requests.length;
  for (const value of ['bad-date', '2999-01-01T00:00:00']) {
    get('history-until').value = value;
    await get('history-query').events.click();
    assert.match(get('history-note').textContent, /有效且不晚于现在/);
  }
  assert.equal(requests.length, beforeInvalid);
  get('history-until').value = '';
  const limited = evaluate('history()');
  requests.at(-1).resolve({ok:false, status:503, text:async()=>'budget'});
  await limited;
  assert.match(get('history-note').textContent, /缩小时间范围/);
  assert.ok(!get('history-samples').innerHTML.includes('older period'));
  const failure = evaluate('history()');
  requests.at(-1).reject(Error('network'));
  await failure;
  assert.match(get('history-note').textContent, /历史读取失败/);
  evaluate(source.slice(source.lastIndexOf('setInterval(')));
  get('history-until').value = '2020-01-02T03:04:05';
  const beforeRefresh = requests.length;
  intervals[0]();
  assert.equal(requests.length, beforeRefresh);
  get('history-until').value = '';
  intervals[0]();
  assert.equal(requests.length, beforeRefresh + 1);
  assert.ok(!requests.at(-1).path.includes('until='));
  requests.at(-1).resolve(response('live'));
})().catch(error=>{console.error(error); process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).parent,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
