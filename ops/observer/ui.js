'use strict';
const hosts = ['192.168.3.180', '192.168.3.181', '192.168.3.183', '192.168.3.184'];
const $ = id => document.getElementById(id);
const display = (v, suffix = '') => typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(1)}${suffix}` : '—';
const timestamp = v => typeof v === 'number' && Number.isFinite(v) ? new Date(v * 1000).toLocaleString() : '无';
function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function renderStatus(data) {
  const recording = data.history_recording || {};
  $('recording').textContent = `观察历史 ${recording.state || '等待'} · 本次持久写入 ${recording.writes || 0} 次 · 写入失败 ${recording.failed_writes || 0} 次 · 最近 ${timestamp(recording.last_timestamp)}${recording.error ? ' · ' + recording.error : ''} · 不参与控制或学习`;
  $('recording').className = recording.state === 'failed' ? 'error' : '';
  $('updated').textContent = `页面查询：${timestamp(data.generated_at)}`;
  $('nodes').replaceChildren(...data.nodes.map(node => {
    const card = el('article', undefined, 'node');
    const title = el('h2', node.host); title.append(el('span', node.fresh ? '读数新鲜' : node.online ? '时间未验证 / 已过期' : '不可用 / 旧读数', `badge${node.fresh ? ' fresh' : ''}`)); card.append(title);
    const metrics = el('div', undefined, 'metrics');
    [['CPU', node.cpu_temp], ['GPU', node.gpu_temp], ['非 GPU 最高', node.non_gpu_temp]].forEach(([name, value]) => {
      const m = el('div', undefined, 'metric'); m.append(el('span', name), el('strong', display(value, '°'))); metrics.append(m);
    }); card.append(metrics);
    card.append(el('p', `功率 ${display(node.power_w, ' W')} · ${node.power_source || '不可用'}\nCPU 负载 ${display(node.cpu_load, '%')} / GPU 负载 ${display(node.gpu_load, '%')}`));
    card.append(el('p', `接收 ${timestamp(node.received_at)} · 接收年龄 ${display(node.reception_age_seconds, 's')}\n源时间 ${timestamp(node.source_ts)} · 源年龄 ${display(node.source_age_seconds, 's')} · 时钟偏差 ${display(node.source_clock_skew_seconds, 's')}`, 'time'));
    if (node.error) card.append(el('p', node.error, 'error'));
    const list = el('ul', undefined, 'sensors');
    (node.sensors || []).forEach(sensor => { const row = el('li'); row.append(el('span', `${sensor.group} · ${sensor.label}${sensor.calibrated ? '' : '（未标定）'}`), el('b', display(sensor.temp, ' °C'))); list.append(row); });
    if (!(node.sensors || []).length) list.append(el('li', '无传感器读数'));
    card.append(list); return card;
  }));
  const p = data.purifier;
  $('purifier').textContent = `${p.fresh ? '新鲜读回' : '不可用或旧读回'} · 电源 ${p.power || '—'} · 模式 ${p.mode || '—'} · 实际档位 ${p.actual_level ?? '未知'} · favorite 设置 ${p.favorite_setting ?? '—'} · 转速 ${display(p.rpm, ' RPM')} · 附近环境 ${display(p.ambient_c, ' °C')}（未经验证为室温） · 接收 ${timestamp(p.received_at)} / 年龄 ${display(p.reception_age_seconds, 's')}${p.error ? ' · ' + p.error : ''}`;
}
async function status() {
  try { const response = await fetch('/api/status', {cache:'no-store'}); if (!response.ok) throw Error(); renderStatus(await response.json()); }
  catch { $('recording').textContent = '写入状态查询失败，无法确认当前采集'; $('updated').textContent = '当前查询失败；屏幕上保留的是旧读数'; document.querySelectorAll('.badge').forEach(n => { n.className = 'badge'; n.textContent = '旧读数 / 查询失败'; }); $('purifier').textContent = '当前净化器读回不可用；没有当前执行确认'; }
  finally { setTimeout(status, 3000); }
}
async function history() {
  $('refresh').disabled = true; $('history-message').className = ''; $('history-message').textContent = '正在预算内读取历史…';
  const query = new URLSearchParams({hours:$('hours').value, source:$('source').value});
  if ($('until').value) query.set('until', String(new Date($('until').value).getTime() / 1000));
  try {
    const response = await fetch('/api/history?' + query, {cache:'no-store'}); const data = await response.json();
    if (!response.ok) { $('history-message').className = 'error'; $('history-message').textContent = `${response.status} · ${data.error || '历史不可读'}${response.status === 503 ? '；请选择更小窗口（1/3/6h）或更早结束时间。已有表格是上次结果。' : ''}`; return; }
    $('records').replaceChildren(...data.samples.map(sample => {
      const tr = el('tr'); const fan = sample.fan || {}; tr.append(el('td', timestamp(sample.ts)));
      hosts.forEach(host => { const n = (sample.nodes || []).find(n => n.host === host) || {}; tr.append(el('td', `${display(n.cpu_temp)} / ${display(n.gpu_temp)} / ${display(n.board_temp)} °C${n.online === false ? '（离线）' : n.fresh !== true ? '（旧读数 / 时间未验证）' : ''}`)); });
      const actual = fan.power === 'on' && fan.mode === 'favorite' ? fan.level ?? '未知' : `未知（favorite设置 ${fan.level ?? '—'}）`;
      [sample.requested_level ?? '—', actual, display(fan.rpm), display(fan.ambient_c, ' °C'), Array.isArray(sample.reasons) ? sample.reasons.join('；') : String(sample.reasons || '')].forEach(v => tr.append(el('td', String(v)))); return tr;
    }));
    $('history-message').textContent = `${timestamp(data.from)} — ${timestamp(data.to)} · ${data.samples.length} 条${data.samples_truncated ? '（仅最近200条）' : ''} · ${data.coverage} · ${data.source === 'control' ? '原控制采集已暂停' : '观察历史，非学习记录'}`;
  } catch { $('history-message').className = 'error'; $('history-message').textContent = '历史查询失败；已有表格是上次结果，未返回空历史替代。'; }
  finally { $('refresh').disabled = false; }
}
$('refresh').addEventListener('click', history);
renderStatus({generated_at:null, nodes:hosts.map(host => ({host, online:false, fresh:false, sensors:[]})), purifier:{fresh:false}});
status(); history();
