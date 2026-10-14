import shutil
import subprocess
import unittest
from pathlib import Path


class QRUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is optional for offline UI interaction checks")
    def test_qr_consent_confirmation_lost_response_cancel_and_canvas(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const events = {}, timers = [];
const ids = [...fs.readFileSync('ui.html', 'utf8').matchAll(/id="(qr-[^"]+)"/g)].map(match=>match[1]);
const elements = new Map(ids.map(id=>[id, {disabled:false, checked:false, hidden:true, events:{}, value:'', children:[], replaceChildren(...items){this.children=items;this.value='';},
  addEventListener(name, fn){this.events[name]=fn;}, getContext(){return {drawImage(){draws++;}};}}]));
const get = id=>elements.get(id);
let draws=0, closes=0, phase='ready', failStart=false, failBind=false, calls=[], deferStart=false, delayedStart,
  deferCancel=false, delayedCancel, failStatus=false;
let devices=[{did:'70632677', name:'<script>unsafe</script>', model:'zhimi.airpurifier.m2', ip:'192.168.3.24', online:true, selectable:true, reason:''},
  {did:'999', name:'音箱', model:'xiaomi.wifispeaker.lx06', ip:null, online:false, selectable:false, reason:'unsupported_device'},
  {did:'sensor', name:'未命名设备', model:'', ip:null, online:null, selectable:false, reason:'unsupported_device'}];
const payload = ()=>({phase, remaining_seconds:120, code:phase!=='ready'||devices.some(device=>device.selectable)?'':devices.some(device=>device.reason==='credential_unavailable')?'credential_unavailable':'target_missing', devices, image_ready:phase==='waiting'});
const context = {Option:function(text,value){this.textContent=text;this.value=value;}, $:get, Date, Uint8Array, crypto:require('node:crypto').webcrypto,
  window:{addEventListener(name, fn){events[name]=fn;}}, clearTimeout(){},
  setTimeout(fn, ms){timers.push({fn, ms}); return timers.length;}, AbortSignal:{timeout:()=>({})},
  createImageBitmap:async()=>({close(){closes++;}}),
  fetch:async(path, options)=>{calls.push({path, body:JSON.parse(options.body)}); return {ok:true, blob:async()=>({})};},
  api:async(path, body)=>{
    calls.push({path, body});
    if(path.endsWith('/start') && deferStart) return new Promise(resolve=>{delayedStart=resolve;});
    if(path.endsWith('/cancel') && deferCancel) return new Promise(resolve=>{delayedCancel=resolve;});
    if(path.endsWith('/status') && failStatus) throw Error('timeout');
    if(path.endsWith('/start') && failStart) throw Error('timeout');
    if(path.endsWith('/bind')) {if(failBind) throw Error('timeout'); phase='bound';}
    if(path.endsWith('/cancel')) phase='canceled';
    return payload();
  }};
const source=fs.readFileSync('ui.js', 'utf8');
vm.runInNewContext(source.slice(source.indexOf('let qrOwner = null;'), source.lastIndexOf('refresh(); history();')), context);
const fire=(id, event='click')=>get(id).events[event]();
const starts=()=>calls.filter(call=>call.path.endsWith('/start')).length;
const binds=()=>calls.filter(call=>call.path.endsWith('/bind')).length;
(async()=>{
  assert.equal(get('qr-start').disabled, true);
  await fire('qr-start');
  assert.equal(calls.length, 0);
  get('qr-consent').checked=true;
  fire('qr-consent', 'change');
  assert.equal(get('qr-start').disabled, false);
  await fire('qr-start');
  assert.equal(starts(), 1);
  assert.equal(get('qr-bind-consent').disabled, true);
  assert.equal(get('qr-devices').value, '');
  assert.equal(get('qr-bind').disabled, true);
  assert.equal(binds(), 0);
  const owner=calls[0].body.owner;
  assert.match(owner, /^[0-9a-f]{64}$/);
  assert.equal(calls[0].body.consent, 'community-qr-c4db715-v1');
  assert.equal(calls[1].body.owner, owner);
  await fire('qr-bind');
  assert.equal(binds(), 0);
  assert.equal(get('qr-devices').children[2].disabled, true);
  assert.match(get('qr-devices').children[2].textContent, /尚未验证此物理设备的控制范围，不支持绑定/);
  assert.equal(get('qr-devices').children.length, 4);
  assert.match(get('qr-devices').children[1].textContent, /zhimi.airpurifier.m2 · 192.168.3.24 · DID 70632677/);
  assert.match(get('qr-devices').children[2].textContent, /音箱 · xiaomi.wifispeaker.lx06 · IP 未知 · DID 999/);
  assert.match(get('qr-devices').children[3].textContent, /未命名设备 · 型号未知 · IP 未知 · DID sensor · 在线状态未知/);
  assert.equal(get('qr-devices').children[3].disabled, true);
  assert.match(fs.readFileSync('ui.html', 'utf8'), /米家设备列表/);
  assert.ok(get('qr-devices').children[1].textContent.includes('<script>unsafe</script>'));
  get('qr-devices').value='70632677';
  fire('qr-devices', 'change');
  get('qr-bind-consent').checked=true;
  fire('qr-bind-consent', 'change');
  context.testOwner=owner;
  await vm.runInNewContext('qrPoll(testOwner)', context);
  assert.equal(get('qr-devices').value, '70632677');
  assert.equal(get('qr-bind-consent').checked, true);
  get('qr-devices').value='';
  fire('qr-devices', 'change');
  assert.equal(get('qr-bind-consent').checked, false);
  assert.equal(get('qr-bind').disabled, true);
  get('qr-devices').value='999';
  fire('qr-devices', 'change');
  get('qr-bind-consent').checked=true;
  await fire('qr-bind');
  assert.equal(binds(), 0);
  get('qr-devices').value='70632677';
  fire('qr-devices', 'change');
  get('qr-bind-consent').checked=true;
  fire('qr-bind-consent', 'change');
  assert.equal(get('qr-bind').disabled, false);
  await fire('qr-bind');
  assert.equal(binds(), 1);
  assert.equal(calls.find(call=>call.path.endsWith('/bind')).body.did, '70632677');
  assert.equal(calls.find(call=>call.path.endsWith('/bind')).body.consent, 'save-fixed-device-and-protected-control-v1');
  assert.equal(get('qr-bind-consent').checked, false);
  assert.equal(get('qr-start').disabled, true);
  assert.match(get('qr-result').textContent, /仍须.*实际转速/);
  phase='waiting';
  failStart=true;
  get('qr-consent').checked=true;
  fire('qr-consent', 'change');
  await fire('qr-start');
  assert.equal(starts(), 2);
  assert.equal(draws, 1);
  assert.equal(closes, 1);
  assert.equal(get('qr-image').hidden, false);
  assert.equal(get('qr-bind').disabled, true);
  assert.equal(calls.at(-1).path, '/api/xiaomi/qr/image');
  assert.equal(calls.at(-1).body.owner, calls.filter(call=>call.path.endsWith('/start')).at(-1).body.owner);
  await fire('qr-cancel');
  assert.equal(get('qr-image').hidden, true);
  assert.equal(get('qr-bind').disabled, true);
  assert.equal(get('qr-consent').checked, false);
  phase='ready'; failStart=false; failBind=true;
  get('qr-consent').checked=true;
  fire('qr-consent', 'change');
  await fire('qr-start');
  get('qr-devices').value='70632677'; fire('qr-devices', 'change');
  get('qr-bind-consent').checked=true;
  fire('qr-bind-consent', 'change');
  await fire('qr-bind');
  const before=binds();
  await fire('qr-bind');
  assert.equal(binds(), before);
  assert.equal(get('qr-bind').disabled, true);
  assert.match(get('qr-result').textContent, /结果未知/);
  assert.equal(starts(), 3);
  await fire('qr-cancel');
  deferStart=true;
  get('qr-consent').checked=true;
  fire('qr-consent', 'change');
  const stale=fire('qr-start');
  await fire('qr-cancel');
  deferStart=false; phase='ready';
  get('qr-consent').checked=true;
  fire('qr-consent', 'change');
  await fire('qr-start');
  delayedStart({phase:'failed', remaining_seconds:0});
  await stale;
  assert.equal(get('qr-bind-consent').disabled, true);
  assert.equal(get('qr-start').disabled, true);
  assert.match(get('qr-result').textContent, /枚举完成/);
  assert.equal(source.includes('localStorage'), false);
  assert.equal(source.includes('sessionStorage'), false);
  assert.equal(source.includes('createObjectURL'), false);
  deferCancel=true;
  const canceling=fire('qr-cancel');
  assert.equal(get('qr-bind-consent').disabled, true);
  assert.equal(get('qr-bind').disabled, true);
  context.testOwner=calls.at(-1).body.owner;
  failStatus=true;
  await vm.runInNewContext('qrPoll(testOwner)', context);
  assert.equal(get('qr-cancel').disabled, true);
  failStatus=false;
  await vm.runInNewContext('qrPoll(testOwner)', context);
  assert.equal(get('qr-bind-consent').disabled, true);
  assert.equal(get('qr-cancel').disabled, true);
  delayedCancel({phase:'canceled', remaining_seconds:0});
  await canceling;
  deferStart=true;
  get('qr-consent').checked=true;
  fire('qr-consent', 'change');
  const lateStart=fire('qr-start');
  const pendingCancel=fire('qr-cancel');
  delayedStart({phase:'ready', remaining_seconds:120});
  await lateStart;
  assert.equal(get('qr-bind-consent').disabled, true);
  assert.equal(get('qr-cancel').disabled, true);
  delayedCancel({phase:'canceled', remaining_seconds:0});
  await pendingCancel;
  deferStart=false;
  deferCancel=false;
  get('qr-consent').checked=true;
  fire('qr-consent', 'change');
  await fire('qr-start');
  get('qr-devices').value='70632677'; fire('qr-devices', 'change');
  get('qr-bind-consent').checked=true;
  devices=devices.slice(1);
  context.testOwner=calls.filter(call=>call.path.endsWith('/start')).at(-1).body.owner;
  await vm.runInNewContext('qrPoll(testOwner)', context);
  assert.equal(get('qr-devices').value, '');
  assert.equal(get('qr-bind-consent').checked, false);
  assert.equal(get('qr-bind').disabled, true);
  assert.match(get('qr-result').textContent, /本次列表.*未找到/);
  assert.equal(get('qr-devices').disabled, false);
  assert.equal(get('qr-devices').children[1].disabled, true);
  devices=[{did:'70632677', name:'共享净化器', model:'zhimi.airpurifier.m2', ip:'192.168.3.24', online:true,
    selectable:false, reason:'credential_unavailable'}];
  await vm.runInNewContext('qrPoll(testOwner)', context);
  assert.equal(get('qr-devices').children[1].disabled, true);
  assert.match(get('qr-devices').children[1].textContent, /本次未返回控制凭据，无法绑定/);
  assert.match(get('qr-result').textContent, /已找到固定净化器.*无法绑定/);
  get('qr-devices').value='70632677'; fire('qr-devices', 'change');
  get('qr-bind-consent').checked=true;
  const beforeMissing=binds();
  await fire('qr-bind');
  assert.equal(binds(), beforeMissing);
  assert.equal(get('qr-bind').disabled, true);
  assert.equal(get('qr-bind-consent').disabled, true);
  phase='fetching';
  await vm.runInNewContext('qrPoll(testOwner)', context);
  assert.match(get('qr-result').textContent, /账号设备和家庭设备/);
  assert.match(fs.readFileSync('ui.html', 'utf8'), /重启后自动读取，无需再次扫码/);
  events.pagehide();
  assert.equal(get('qr-image').hidden, true);
  assert.equal(calls.at(-1).path, '/api/xiaomi/qr/cancel');
  assert.ok(timers.every(timer=>timer.ms>=2000));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).parent,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
