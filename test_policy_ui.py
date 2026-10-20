import shutil
import subprocess
import unittest
from pathlib import Path


BOOTSTRAP = r'''
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
const requests = [];
const status = {controller_age_seconds:0, server_time:102, updated_at:102,
  settings:{manual_level:null, retention_days:7, max_log_mb:4096}, requested_level:10,
  fan:{configured:true, confirmed:true, level:10, rpm:900, power:'on', mode:'favorite', verified_at:101, execution:'matched'},
  decision:{}, nodes:['180','181','183','184'].map(id => ({host:'192.168.3.' + id, online:true, cpu_temp:40, gpu_temp:40, board_temp:40})),
  log_bytes:0, dropped_records:0};
const context = {status, document:{getElementById:get, querySelectorAll:()=>[]}, window:{addEventListener(){}},
  Option:function(text, value) { this.textContent = text; this.value = value; },
  setTimeout(){}, setInterval(){}, clearTimeout(){}, AbortSignal:{timeout:()=>({})},
  fetch:async(path, options)=>{requests.push({path, options}); return {ok:true, json:async()=>({settings:JSON.parse(options.body)})};}};
vm.createContext(context);
const source = fs.readFileSync('ui.js', 'utf8');
vm.runInContext(source.slice(0, source.lastIndexOf('refresh(); history();')), context);
const evaluate = code => vm.runInContext(code, context);
const render = () => evaluate('render(status)');
function history(overrides = {}) {
  context.sample = {ts:100, snapshot_at:status.server_time, control:status.settings,
    requested_level:status.requested_level, fan:status.fan, nodes:[], ...overrides};
  evaluate('renderHistory({samples:[sample], from:0, to:103})');
  return get('history-samples').innerHTML;
}
function unknown() {
  render();
  assert.equal(get('actual').textContent, '未确认');
  const row = history();
  assert.match(row, /<td>实际档位未知<small>/);
  assert.ok(!row.includes('记录时实际匹配'));
  assert.notEqual(get('execution').textContent, '实际匹配');
}
'''


class PolicyUITests(unittest.TestCase):
    def run_node(self, script):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for policy UI checks')
        result = subprocess.run([node, '-e', BOOTSTRAP + script], cwd=Path(__file__).parent,
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_high_exit_displays_critical_margin_without_claiming_requested_gear_is_actual(self):
        self.run_node(r'''
status.requested_level = 10;
Object.assign(status.fan, {level:16, rpm:2096, confirmed:false, execution:'pending'});
status.decision = {demand:10, level:16, control_status:{phase:'observing',
  primary_reason:'high_exit_observation', release_basis:'high_exit', margin_reference:'critical75',
  blocking_source:['192.168.3.180', '<svg onload=bad()>'], margin_c:9, measured_margin_c:10,
  trend_rate_c_per_second:0.003, chain_warming_c:0.7, warming_budget_c:2,
  observe_seconds:90, required_seconds:180, next_condition:'high_exit_complete_observation_180s',
  workload_phase:'preparing', ordinary_reason:'warming_budget_exhausted'}};
status.decision.policy_spec = {version:'fixture-exit', high_exit:{trend_seconds:61,
  observation_seconds:181, margin_c:6, warming_budget_c:3}};
render();
const detail = get('policy-detail').textContent;
assert.match(detail, /高档逐级退出/);
assert.match(detail, /距75°C危急线的预测余量 9.0°C/);
assert.match(detail, /实温余量 10.0°C/);
assert.match(detail, /趋势 0.003°C\/s/);
assert.match(detail, /退出链累计温升 0.7 \/ 2.0°C/);
assert.match(detail, /普通低档搜索仍受.*累计温升额度已耗尽/);
assert.match(detail, /90 \/ 180 秒/);
assert.equal(get('actual').textContent, '16 档');
assert.equal(get('desired').textContent, 10);
const row = history({decision:status.decision});
assert.match(row, /高档逐级退出/);
assert.match(row, /距75°C危急线的预测余量/);
assert.ok(row.includes('&lt;svg'));
assert.ok(!row.includes('<svg'));
assert.ok(!detail.includes('同源预测余量'));
assert.match(get('policy-parameters').textContent, /高档退出.*61 秒.*181 秒.*至少6°C.*3°C/);
''')

    def test_enhanced80_distinguishes_normal_ceiling_step_and_actual(self):
        self.run_node(r'''
status.high_wind_gate = true;
status.high_wind_limit = 10;
status.requested_level = 15;
Object.assign(status.fan, {level:16, rpm:2096, confirmed:false, execution:'pending'});
status.decision = {demand:10, level:15,
  policy_spec:{version:'enhanced80-r2', minimum_level:0, maximum_level:16,
    ordinary_ceiling:10, enhanced_admission_c:80, targets_c:{cpu:60, gpu:60, board:65},
    critical_c:75, descent_step:1, observe_seconds:180},
  high_wind:{reason:'recovering', actual_c:65, observation_seconds:0},
  operating:{phase:'descending', observe_credit:0},
  execution_trial:{purpose:'ceiling_exit', from:16, to:15, status:'pending'},
  control_status:{phase:'waiting_execution', release_basis:'ceiling80',
    primary_reason:'ceiling_exit_pending', transition_target:10}};
render();
assert.match(get('policy-detail').textContent, /普通上限 10 档/);
assert.match(get('policy-detail').textContent, /需求 10.*下一允许档 15/);
assert.match(get('policy-detail').textContent, /退出至10档/);
assert.equal(get('actual').textContent, '16 档');
assert.match(get('policy-parameters').textContent, /80°C/);
assert.match(get('policy-parameters').textContent, /不是.*安全上限/);
assert.ok(!get('policy-parameters').textContent.includes('先取本档趋势'));
assert.ok(!get('policy-detail').textContent.includes('当前可写上限'));
assert.match(history({decision:status.decision}), /退出至10档/);
''')

    def test_ceiling_requalification_names_gate_and_keeps_actual_high_endpoint(self):
        self.run_node(r'''
status.high_wind_gate = true;
status.high_wind_limit = 10;
status.requested_level = 10;
Object.assign(status.fan, {level:16, rpm:2096, confirmed:false, execution:'pending'});
for (const [reason, label] of [
  ['ceiling_requalification_material_recovery', '失败来源的负载或功率尚未满足改善条件'],
  ['ceiling_requalification_context_changed', '工况范围或空闲状态改变，重新取证'],
  ['ceiling_requalification_temperature_window', '温度波幅或趋势未通过完整60秒核验'],
]) {
  status.decision = {demand:10, level:16,
    policy_spec:{version:'enhanced80-r4', enhanced_admission_c:80},
    control_status:{phase:'hold', release_basis:'ceiling80',
      primary_reason:'ceiling_exit_requalifying', next_condition:reason,
      observe_seconds:0, required_seconds:60, transition_target:10}};
  render();
  assert.ok(get('policy-detail').textContent.includes(label));
  assert.equal(get('actual').textContent, '16 档');
  assert.notEqual(get('execution').textContent, '实际匹配');
  assert.ok(history({decision:status.decision}).includes(label));
}
''')

    def test_enhanced80_unknown_evidence_does_not_invent_a_temperature(self):
        self.run_node(r'''
status.high_wind_gate = true;
status.high_wind_limit = 10;
status.decision = {policy_spec:{version:'enhanced80-r2', enhanced_admission_c:80},
  high_wind:{reason:'degraded', actual_c:null, detail:'required source expired'},
  control_status:{phase:'hold', release_basis:'ceiling80', primary_reason:'ceiling_exit_source'}};
render();
assert.match(get('policy-detail').textContent, /实温.*°C/);
assert.ok(!get('policy-detail').textContent.includes('实温 80.0'));
assert.match(get('policy-detail').textContent, /来源不完整或已过期/);
''')

    def test_source_fault_ascent_is_a_named_exception_not_a_policy_exit(self):
        self.run_node(r'''
status.high_wind_gate = true;
status.high_wind_limit = 10;
status.requested_level = 12;
Object.assign(status.fan, {level:10, rpm:900, confirmed:false, execution:'pending'});
status.decision = {demand:12, level:12,
  policy_spec:{version:'enhanced80-r2', enhanced_admission_c:80},
  high_wind:{reason:'source_fault', exception:'SourceFaultProtection', actual_c:null,
    detail:'identity_ambiguous', observation_seconds:0},
  control_status:{phase:'waiting_execution', release_basis:'ceiling80',
    primary_reason:'ceiling_exit_source_fault'}};
render();
assert.match(get('policy-detail').textContent, /温度来源故障保护例外/);
assert.match(get('policy-detail').textContent, /不是普通增强/);
assert.ok(!get('policy-detail').textContent.includes('逐档退出至10档'));
assert.equal(get('actual').textContent, '10 档');
const row = history({decision:status.decision});
assert.match(row, /温度来源故障保护例外/);
assert.ok(!row.includes('逐档退出至10档'));
''')

    def test_unknown_exception_cannot_be_presented_as_approved_protection(self):
        self.run_node(r'''
status.high_wind_gate = true;
status.high_wind_limit = 10;
for (const exception of ['FabricatedProtection', 'constructor', 'toString', '__proto__']) {
  status.decision = {policy_spec:{version:'enhanced80-r2', enhanced_admission_c:80},
    high_wind:{reason:'source_fault', exception, actual_c:null}};
  render();
  assert.match(get('policy-detail').textContent, /保护例外类型未知/);
  assert.ok(!get('policy-detail').textContent.includes('温度来源故障保护例外'));
  assert.ok(!get('policy-detail').textContent.includes('实温 80.0'));
}
''')

    def test_high_exit_labels_do_not_reinterpret_ordinary_or_unknown_margins(self):
        self.run_node(r'''
for (const reference of [undefined, 'working', 'unknown']) {
  status.decision.control_status = {phase:'hold', primary_reason:'insufficient_margin',
    margin_reference:reference, margin_c:4};
  render();
  assert.match(get('policy-detail').textContent, /同源预测余量 4.0°C/);
  assert.ok(!get('policy-detail').textContent.includes('距75°C'));
  assert.ok(!get('policy-detail').textContent.includes('退出链累计温升'));
}
status.decision.control_status = {phase:'hold', release_basis:'high_exit', margin_reference:'critical75',
  primary_reason:'high_exit_source', margin_c:null, measured_margin_c:null,
  chain_warming_c:null, warming_budget_c:2, next_condition:'high_exit_fresh_endpoint'};
render();
assert.match(get('policy-detail').textContent, /高档逐级退出/);
assert.match(get('policy-detail').textContent, /取得新鲜正转速端点/);
assert.ok(!get('policy-detail').textContent.includes('预测余量 0.0'));
assert.ok(!get('policy-detail').textContent.includes('累计温升 0.0'));
''')

    def test_release_refusals_show_specific_reasons_without_claiming_movement(self):
        self.run_node(r'''
for (const [reason, text] of [
  ['source_fault_protection', '当前温度来源故障保护'],
  ['failed_descent_restoration', '真实降档热失败，恢复精确安全端点'],
  ['raw_projection_margin', '原始预测余量不足'],
  ['persistent_heat', '持续升温尚未证明风险有界'],
  ['coverage_or_genuine_rise', '覆盖不足或升温风险未解除'],
  ['expanding_return_range', '波动范围扩大，尚未证明回落'],
  ['drift_rate_or_projected_margin', '回温速率或预测余量未满足下降条件']
]) {
  status.decision.control_status = {phase:'hold', primary_reason:reason,
    blocking_source:['192.168.3.183', 'acpi:2:acpitz', 'acpi'],
    observe_seconds:0, required_seconds:180, next_condition:'fresh_complete_safe_evidence'};
  render();
  assert.ok(get('policy-detail').textContent.includes(text), get('policy-detail').textContent);
  assert.ok(history({decision:status.decision}).includes(text));
  assert.equal(get('actual').textContent, '10 档');
}
''')

    def test_returned_cycle_reasons_do_not_claim_a_physical_descent(self):
        self.run_node(r'''
for (const [reason, text] of [
  ['growing_return_cycles', '重复波动幅度仍在增加'],
  ['returned_cycle_observation', '处于既有波动范围，尚未满足执行条件'],
  ['returned_cycle_bound', '已回落的周期波动，仍按单步条件评估']
]) {
  status.decision.control_status = {phase:'observing', primary_reason:reason,
    observe_seconds:150, required_seconds:180, next_condition:'same_source_ready'};
  render();
  assert.ok(get('policy-detail').textContent.includes(text));
  assert.ok(history({decision:status.decision}).includes(text));
  assert.equal(get('actual').textContent, '10 档');
  assert.equal(get('desired').textContent, 10);
  assert.match(get('policy-detail').textContent, /150 \/ 180/);
}
''')

    def test_stationary_proof_copy_does_not_claim_physical_descent(self):
        self.run_node(r'''
status.decision.control_status = {phase:'observing', primary_reason:'stationary_envelope',
  observe_seconds:60, required_seconds:180, next_condition:'complete_safe_observation'};
render();
assert.match(get('policy-detail').textContent, /已验证的安全往返波动/);
assert.match(get('policy-detail').textContent, /60 \/ 180/);
assert.equal(get('actual').textContent, '10 档');
assert.match(history({decision:status.decision}), /已验证的安全往返波动/);
''')

    def test_device_auto_350_never_claims_register_16_as_actual(self):
        self.run_node(r'''
Object.assign(status.fan, {level:16, mode:'auto', rpm:350});
status.requested_level = 16;
unknown();
assert.match(get('fan-setting').textContent, /设备自动.*favorite 设置.*16.*不等于实际档位/);
assert.match(history(), /favorite 设置.*16.*不等于实际档位/);
for (const scenario of ['readback_hold', 'communication_hold', 'graded_hold', 'normal']) {
  status.decision.scenario = scenario;
  status.nodes[0].cpu_temp = scenario === 'graded_hold' ? 60 : 40;
  status.storage_health = {phase:scenario === 'normal' ? 'holding' : 'normal'};
  render();
  assert.ok(!/实际(?:风力为| )16/.test(get('alarm').textContent), get('alarm').textContent);
}
''')

    def test_off_fault_and_invalid_evidence_remain_unknown(self):
        self.run_node(r'''
const base = {...status.fan};
for (const patch of [{power:'off'}, {fault:true}, {execution:'fault'}, {rpm:0}, {rpm:-1},
  {rpm:NaN}, {rpm:Infinity}, {rpm:'900'}, {level:-1}, {level:17}, {level:1.5},
  {level:'10'}, {level:null}, {level:NaN}, {mode:undefined}, {power:undefined}]) {
  status.fan = {...base, ...patch};
  unknown();
}
''')

    def test_stale_and_invalid_timestamps_and_controller_age(self):
        self.run_node(r'''
for (const stamp of [92, 103, null, undefined, NaN, Infinity, -1]) {
  status.fan.verified_at = stamp;
  unknown();
}
status.fan.verified_at = 93;
render();
assert.equal(get('actual').textContent, '10 档');
assert.match(history(), /<td>10 档<small>记录时实际匹配/);
for (const age of [7, -1, null, undefined, NaN, Infinity, '0']) {
  status.controller_age_seconds = age;
  render();
  assert.equal(get('actual').textContent, '未确认');
  assert.equal(get('online-count').textContent, '0 / 4 在线');
}
status.controller_age_seconds = 6;
render();
assert.equal(get('actual').textContent, '10 档');
for (const stamp of [null, undefined, NaN, Infinity, -1]) {
  status.server_time = stamp;
  unknown();
}
''')

    def test_known_favorite_endpoint_is_separate_from_request_matching(self):
        self.run_node(r'''
Object.assign(status.fan, {level:15, confirmed:false, execution:'pending'});
render();
assert.equal(get('actual').textContent, '15 档');
assert.equal(get('desired').textContent, 10);
assert.equal(get('execution').textContent, '最新请求待执行');
assert.match(get('fan-status').textContent, /实际档位已知.*请求尚未确认/);
assert.match(history(), /<td>15 档<small>记录时实际档位已知，未确认请求匹配/);
assert.ok(!history().includes('记录时实际匹配'));
Object.assign(status.fan, {confirmed:true, execution:'matched'});
render();
assert.equal(get('actual').textContent, '15 档');
assert.notEqual(get('execution').textContent, '实际匹配');
assert.ok(!history().includes('记录时实际匹配'));
''')

    def test_normal_matched_including_zero(self):
        self.run_node(r'''
for (const level of [0, 10, 16]) {
  status.fan.level = status.requested_level = level;
  render();
  assert.equal(get('actual').textContent, `${level} 档`);
  assert.equal(get('execution').textContent, '实际匹配');
  assert.ok(history().includes(`<td>${level} 档<small>记录时实际匹配`));
}
''')

    def test_legacy_history_and_raw_setting_escaping(self):
        self.run_node(r'''
for (const overrides of [{snapshot_at:undefined}, {fan:{level:16}}, {fan:undefined},
  {fan:{...status.fan, configured:undefined}}]) {
  assert.match(history(overrides), /<td>实际档位未知<small>/);
}
assert.match(history({fan:{level:16}}), /favorite 设置.*16.*不等于实际档位/);
status.fan.level = '<img src=x onerror="bad()">';
status.fan.mode = '<svg onload="bad()">';
render();
assert.equal(get('actual').textContent, '未确认');
assert.equal(get('fan-setting').innerHTML, '');
assert.ok(get('fan-setting').textContent.includes('<img'));
const row = history();
assert.ok(!row.includes('<img'));
assert.ok(!row.includes('<svg'));
assert.match(row, /&lt;img/);
''')

    def test_manual_minimum_copy_and_unchanged_settings_payload(self):
        self.run_node(r'''
(async()=>{
  status.settings.manual_level = 16;
  render();
  assert.equal(get('policy').textContent, '已保存 · 手动最低档 16 档');
  assert.match(get('policy-detail').textContent, /所选最低档 16 档.*温度与通信保护.*高风门控/);
  assert.match(history(), /手动最低档 16 档/);
  assert.match(html, /手动最低档.*保护.*高风门控/);
  const options = get('control-mode').children;
  assert.match(options[0].textContent, /软件自动/);
  for (let level = 0; level <= 16; level++) {
    assert.match(options[level + 1].textContent, new RegExp(`手动最低档 ${level} 档`));
    assert.equal(options[level + 1].value, String(level));
  }
  assert.match(html, /软件自动.*设备自动/);
  for (const [value, manual] of [['16',16], ['0',0], ['automatic',null]]) {
    get('control-mode').value = value;
    await get('settings-form').events.submit({preventDefault(){}});
    const request = requests.at(-1);
    assert.equal(request.path, '/api/settings');
    assert.deepEqual(JSON.parse(request.options.body), {retention_days:7, max_log_mb:4096, manual_level:manual});
  }
  assert.equal(get('actual').textContent, '10 档');
  render();
  assert.equal(get('policy').textContent, '已保存 · 软件自动温控');
  assert.match(history(), /<td>软件自动温控<\/td>/);
})().catch(error=>{console.error(error); process.exitCode=1;});
''')

    def test_server_spec_blocker_and_persistence_are_visible_and_escaped(self):
        self.run_node(r'''
status.decision.policy_spec = {version:'fixture-version', minimum_level:0, maximum_level:16,
  ordinary_ceiling:9, targets_c:{cpu:61,gpu:62,board:66}, critical_c:76,
  descent_step:1, observe_seconds:181, idle_margin_c:5, loaded_margin_c:8,
  preparation_seconds:61, drift_horizon_seconds:181, drift_rate_c_per_second:0.01, warming_budget_c:2};
status.decision.control_status = {phase:'hold', primary_reason:'warming_budget_exhausted',
  blocking_source:['host', '<script>bad()</script>'], margin_c:12, observe_seconds:120,
  required_seconds:180, next_condition:'safe_comparable_plateau_180s', workload_phase:'settled'};
status.thermal_memory = {state:'failed', descent_allowed:false};
render();
assert.match(get('policy-parameters').textContent, /fixture-version.*上限 9.*CPU 61°C.*危急线 76°C.*181 秒/);
assert.match(get('policy-detail').textContent, /累计温升额度已耗尽.*12.0°C.*120 \/ 180 秒.*新平衡180秒/);
assert.match(get('alarm').textContent, /热记忆保存失败.*暂停下降/);
status.decision.thermal_memory = status.thermal_memory;
const row = history({decision:status.decision});
assert.match(row, /累计温升额度已耗尽/);
assert.match(row, /热记忆保存失败/);
assert.ok(row.includes('&lt;script&gt;'));
assert.ok(!row.includes('<script>'));
status.thermal_memory = {state:'ready', descent_allowed:true};
render();
assert.match(get('policy-detail').textContent, /热记忆已可靠保存/);
assert.ok(!get('policy-detail').textContent.includes('热记忆保存失败'));
delete status.decision.policy_spec;
render();
assert.match(get('policy-parameters').textContent, /不能从旧页面推断已部署策略/);
''')


if __name__ == '__main__':
    unittest.main()
