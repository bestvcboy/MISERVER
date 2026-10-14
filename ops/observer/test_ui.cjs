'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor() { this.children = []; this.textContent = ''; this.value = ''; this.className = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener() {}
}
const elements = Object.fromEntries(['updated','nodes','purifier','hours','until','refresh','records','history-message','recording','source'].map(id => [id, new Element()]));
elements.hours.value = '24'; elements.source.value = 'observer';
const hosts = ['180','181','183','184'].map(n => '192.168.3.' + n);
const live = {history_recording:{state:'recording',writes:5,last_timestamp:99,failed_writes:0}, generated_at:100, nodes:hosts.map(host => ({host, online:true, fresh:true, cpu_temp:50, gpu_temp:55, non_gpu_temp:60, sensors:[{group:'cpu', label:'<unsafe>', temp:50, calibrated:true}]})), purifier:{fresh:true, power:'on', mode:'auto', favorite_setting:16, actual_level:null, rpm:200, ambient_c:31.2, received_at:100, reception_age_seconds:0}};
const past = {from:0, to:100, coverage:'只读记录', samples:[{ts:99, requested_level:12, nodes:hosts.map(host => ({host, cpu_temp:50, gpu_temp:55, board_temp:60})), fan:{power:'on', mode:'favorite', level:16, rpm:2095, ambient_c:31.2}, reasons:['reason']} ]};
let historyCode = 200, failedStatus = false;
const requests = [];
const context = vm.createContext({document:{getElementById:id => elements[id], createElement:() => new Element(), querySelectorAll:() => []}, URLSearchParams, Date, Number, String, Array, Error, setTimeout:() => {}, fetch:async (url, options) => {
  requests.push({url, options});
  if (url === '/api/status') { if (failedStatus) throw Error('offline'); return {ok:true, json:async () => live}; }
  return {ok:historyCode === 200, status:historyCode, json:async () => historyCode === 200 ? past : {error:'超出预算', budget_refused:true}};
}});
vm.runInContext(fs.readFileSync(path.join(__dirname, 'ui.js'), 'utf8'), context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.nodes.children.length, 4);
  assert.match(elements.recording.textContent, /持久写入 5 次/);
  live.history_recording = {state:'failed', writes:5, failed_writes:1,error:'持久写入失败'};
  await vm.runInContext('status()', context);
  assert.match(elements.recording.textContent, /持久写入失败/);
  assert.equal(elements.recording.className, 'error');
  assert.match(elements.purifier.textContent, /实际档位 未知/);
  assert.match(elements.purifier.textContent, /favorite 设置 16/);
  assert.match(elements.purifier.textContent, /未经验证为室温/);
  assert.equal(elements.records.children.length, 1);
  const cells = elements.records.children[0].children;
  assert.equal(cells[0].textContent, new Date(99000).toLocaleString());
  assert.equal(cells[8].textContent, '31.2 °C');
  assert.match(cells[1].textContent, /旧读数/);
  past.samples[0].nodes[0].fresh = true;
  await vm.runInContext('history()', context);
  assert.ok(!elements.records.children[0].children[1].textContent.includes('旧读数'));
  const oldRows = elements.records.children;
  historyCode = 503;
  await vm.runInContext('history()', context);
  assert.strictEqual(elements.records.children, oldRows);
  assert.match(elements['history-message'].textContent, /503/);
  assert.match(elements['history-message'].textContent, /1\/3\/6h/);
  assert.match(elements['history-message'].textContent, /上次结果/);
  assert.equal(elements.refresh.disabled, false);
  failedStatus = true;
  await vm.runInContext('status()', context);
  assert.match(elements.updated.textContent, /旧读数/);
  assert.match(elements.purifier.textContent, /没有当前执行确认/);
  assert.ok(requests.every(r => !r.options.method));
  assert.ok(requests.every(r => r.url.startsWith('/api/status') || r.url.startsWith('/api/history?')));
  const html = fs.readFileSync(path.join(__dirname, 'ui.html'), 'utf8');
  assert.ok(!html.includes('manual') && !html.includes('qr/'));
  console.log('PASS offline UI: four nodes, timestamps, auto gear unknown, ambient, 503 preserves old table, GET-only');
})().catch(error => { console.error(error); process.exitCode = 1; });
