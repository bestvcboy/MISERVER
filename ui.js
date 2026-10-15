const hosts = ['192.168.3.180', '192.168.3.181', '192.168.3.183', '192.168.3.184'];
const colors = ['#47dfb6', '#73b6ff', '#ffbf66', '#b7a0ff'];
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const numeric = (value, digits = 1) => Number.isFinite(value) ? value.toFixed(digits) : '—';
let current = null;
let rows = [];
let historyGeneration = 0;
let metric = 'cpu_temp';
let initialized = false;
let settingsDirty = false;
let settingsSaving = false;
let settingsUnknown = false;
const settingsControls = ['retention', 'capacity', 'control-mode', 'settings-save', 'settings-cancel'];
const modeOptions = [new Option('自动温控 · 按温度余量降档 · 保护至 16', 'automatic'),
  ...Array.from({length:17}, (_, level) => new Option(`手动 ${level} 档${level === 0 ? ' · 仍在运转' : ''}`, String(level)))];
$('control-mode').replaceChildren(...modeOptions);
$('control-mode').value = '16';

async function api(path, body) {
  const options = {signal: AbortSignal.timeout(10000)};
  if (body) Object.assign(options, {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const response = await fetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    const error = Error(text.startsWith('{') ? JSON.parse(text).error : text);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function render(data) {
  current = data;
  const fresh = data.controller_age_seconds <= 6 && data.server_time - data.updated_at <= 9;
  const online = data.nodes.filter(node => node.online && fresh).length;
  $('online-count').textContent = `${online} / 4 在线`;
  $('live-text').textContent = fresh ? '实时采集' : '采集异常';
  $('live-dot').style.background = fresh ? '#47dfb6' : '#ff8791';
  $('updated').textContent = data.updated_at ? `更新于 ${new Date(data.updated_at * 1000).toLocaleTimeString('zh-CN')}` : '等待采样';
  $('policy').textContent = data.settings.manual_level === null ? '已保存 · 自动预防性温控' : `已保存 · 手动 ${data.settings.manual_level} 档`;
  $('policy-detail').textContent = `当前场景：${data.decision?.scenario_detail || '旧记录未标注场景'}。有效请求包含升温与故障保护。保存设置后仍须等待受限执行和设备读回。` + trialDetails(data.decision);
  renderLearning(data.learning || {});
  $('desired').textContent = data.requested_level;
  $('gear-bar').innerHTML = Array.from({length:16}, (_, index) => `<i class="${index < data.requested_level ? 'on' : ''}"></i>`).join('');
  const fanFresh = Number.isFinite(data.fan.verified_at) && Number.isFinite(data.server_time)
    && data.server_time - data.fan.verified_at >= 0 && data.server_time - data.fan.verified_at <= 9
    && data.controller_age_seconds <= 6;
  const fanConfirmed = data.fan.configured && data.fan.confirmed && fanFresh && data.fan.level === data.requested_level
    && data.fan.power === 'on' && data.fan.mode === 'favorite'
    && Number.isFinite(data.fan.rpm) && data.fan.rpm > 0 && !data.fan.fault && data.fan.execution === 'matched';
  $('actual').textContent = !data.fan.configured ? '待绑定' : fanFresh && data.fan.level !== null ? `${data.fan.level} 档` : '未确认';
  $('rpm').textContent = fanFresh ? `${numeric(data.fan.rpm, 0)} RPM · ${data.fan.mode || '模式未知'}` : '尚无有效转速读回';
  $('readback-time').textContent = `读回于 ${historyTime(data.fan.verified_at)}${fanFresh ? '' : ' · 当前不可确认'}`;
  const ambientAt = Number.isFinite(data.controller_age_seconds) && data.controller_age_seconds >= 0
    && data.controller_age_seconds <= 6 ? data.server_time : null;
  $('ambient').textContent = ambientReadback(data.fan, ambientAt)
    + (Number.isFinite(ambientAt) ? '' : ' · 当前不可确认');
  $('fan-status').textContent = fanConfirmed ? '电源、模式、档位与转速已读回' : data.fan.error || '等待当前请求的设备读回';
  $('execution').textContent = data.fan.fault ? '故障锁定 · 只读观察' : data.fan.execution === 'pending' ? '最新请求待执行' : fanConfirmed ? '实际匹配' : '等待读回';
  $('pair-state').textContent = data.fan.configured ? '凭据已配置' : '需要连接';
  $('binding-state').textContent = !data.fan.configured ? '尚未配置本地执行凭据。' : fanConfirmed ? '本地凭据已配置，当前请求已获实际读回确认。现场散热标定仍待完成。' : '本地凭据已配置，当前执行尚未确认。' + (data.fan.error || '等待读回。');
  let alarm = '';
  if (!fresh) alarm = '采集循环异常。当前温度不可视为实时，保护目标保持全风。';
  else if (online < 4) alarm = '部分节点采集失败，保护目标已升至最大风力。';
  else if (data.decision?.scenario === 'readback_hold') alarm = fanConfirmed
    ? '设备读回已恢复，正在确认通信稳定，暂停降档。'
    : `设备读回暂时中断，保持当前请求 ${data.requested_level} 档并暂停降档，正在重试。`;
  else if (data.decision?.scenario === 'protect' || data.nodes.some(node =>
           node.cpu_temp >= (data.decision?.limits?.cpu?.[1] ?? 75) - 2
           || node.gpu_temp >= (data.decision?.limits?.gpu?.[1] ?? 75) - 2
           || node.board_temp >= (data.decision?.limits?.board?.[1] ?? 75) - 2 || node.thermal_throttle))
    alarm = `当前处于温度或故障保护。${fanConfirmed && data.requested_level === 16 && data.fan.level === 16 ? '16 档全风请求已匹配读回；RPM 不证明散热能力。' : '16 档全风尚未匹配确认，请检查实际读回与保护原因。'}`;
  else if (data.nodes.some(node => node.cpu_temp >= (data.decision?.limits?.cpu?.[0] ?? 60)
           || node.gpu_temp >= (data.decision?.limits?.gpu?.[0] ?? 60)))
    alarm = `温度达到 60°C 准备区，需求至少 10 档。当前请求 ${data.requested_level}，实际 ${fanFresh ? data.fan.level : "未确认"} 档；持续缺口另行逐级评估。`;
  else if (!fanConfirmed) alarm = data.fan.error || '风力尚未确认。温度记录正在运行。';
  else if (data.settings_error || data.storage_error) alarm = data.settings_error || data.storage_error;
  $('alarm').className = `alarm ${alarm ? 'bad' : 'ok'}`;
  $('alarm').textContent = alarm || '四台节点实时采集，散热设备读回正常。';
  const expanded = new Set([...$('nodes').querySelectorAll('details[open]')].map(item => item.dataset.host));
  $('nodes').innerHTML = hosts.map(host => {
    const node = data.nodes.find(item => item.host === host) || {sensors:[]};
    const valid = node.online && fresh;
    const slope = data.decision.rise_c_per_min?.[host];
    return `<article class="node ${valid ? '' : 'offline'}"><div class="node-head"><b>${host}</b><span>${valid ? '● 在线' : '● 数据缺失'}</span></div><div class="node-name">${escape(node.hostname || '等待节点响应')}</div><div class="temp-main"><b>${valid ? numeric(node.cpu_temp) : '—'}</b><span> °C</span><small>${node.cpu_proxy === false ? 'CPU / 平台保护最高温' : 'CPU 保护代理 · ACPI 最热区'}</small></div><div class="temp-sub"><span>GPU <b>${valid ? numeric(node.gpu_temp) : '—'}°</b></span><span>非GPU硬件最高 <b>${valid ? numeric(node.board_temp) : '—'}°</b></span></div><div class="node-load"><span>CPU ${valid ? numeric(node.cpu_load, 0) : '—'}%</span><span>GPU ${valid ? numeric(node.gpu_load, 0) : '—'}%</span><span>${valid ? numeric(node.power_w, 0) : '—'} W</span></div><small>${escape(powerLabel(node))}</small><div class="rise">${valid && slope >= 0.1 ? `↑ 最大升温趋势 ${numeric(slope)}°C / 分钟` : valid ? '当前未发现明显升温趋势' : '缺失数据触发全风保护'}</div><details data-host="${host}" ${expanded.has(host) ? 'open' : ''}><summary>查看 ${node.sensors?.length || 0} 个传感器</summary><table><tbody>${(node.sensors || []).map(sensor => `<tr class="${sensor.calibrated ? '' : 'uncalibrated'}"><td>${escape(sensor.group)} / ${escape(sensor.label)}${sensor.calibrated ? '' : ' · 校准差异'}</td><td>${numeric(sensor.temp)}°C</td></tr>`).join('')}</tbody></table></details></article>`;
  }).join('');
  $('reasons').innerHTML = (data.decision.reasons || []).map(reason => `<li>${escape(reason)}</li>`).join('');
  $('log-size').textContent = `${numeric(data.log_bytes / 1048576, 2)} / ${data.settings.max_log_mb} MiB`;
  $('usage-fill').style.width = `${Math.min(100, data.log_bytes / (data.settings.max_log_mb * 1048576) * 100)}%`;
  $('storage-status').textContent = data.storage_error || `3 秒采样 · 保留 ${data.settings.retention_days} 天 · 丢失记录 ${data.dropped_records}`;
  if ((!initialized || !settingsDirty) && !settingsSaving && !settingsUnknown) adoptSettings(data.settings);
  if (settingsUnknown && !data.settings_pending) {
    settingsUnknown = false;
    if (!settingsDirty) adoptSettings(data.settings);
    $('settings-result').textContent = (data.settings_error || '已刷新服务器保存状态。原保存响应未确认，设备执行以实际读回为准。')
      + (settingsDirty ? ' 新草稿仍未保存。' : ' 请核对已保存选项。');
  }
}

function adoptSettings(settings) {
  $('retention').value = settings.retention_days;
  $('capacity').value = settings.max_log_mb;
  $('control-mode').value = settings.manual_level === null ? 'automatic' : String(settings.manual_level);
  initialized = true;
}

function powerLabel(node) {
  return node.power_source === 'spbm' && hosts.slice(0, 2).includes(node.host) ? '整机功耗 · spbm'
    : node.power_source === 'estimate:gpu' ? '仅 GPU 估计，不是整机功耗；不用于学习发布'
    : `整机功耗来源未验证 · ${node.power_source || '未知'}`;
}

function renderLearning(learning) {
  if (learning.enabled === false) {
    $('learning-state').textContent = '纯规则自动温控 · 机器学习已暂停';
    $('learning-published').textContent = '已有学习阈值不参与控制，使用固定功耗、温度趋势和故障保护规则。';
    $('learning-candidate').textContent = '每次降档至少间隔 30 秒；目标余量 ≥10°C 最多降 3 档，≥5°C 最多降 2 档，3–5°C 观察 60 秒降 1 档。仍需有效采样与实际设备确认。';
    $('learning-schedule').textContent = '不训练、不评估、不发布学习参数；旧学习记录保留。';
    $('learning-storage').textContent = `保留学习文件 ${numeric((learning.preserved_bytes || 0) / 1048576, 2)} MiB；容量内预留 ${numeric((learning.reserved_bytes || 0) / 1048576, 2)} MiB。原始温度、负载和风力历史继续记录。`;
    return;
  }
  const labels = {health:'健康或设置条件未满足', gap:'采样或存储记录中断', source:'整机来源缺失或采集时间异常',
    identity:'节点或传感器身份变化', temperature:'温度不满足空闲基线条件', load:'负载未低于 10%',
    trend:'30 秒完整趋势未稳定', motor:'实际档位或近期读回未确认', stabilizing:'等待 120 秒稳定实际档位',
    insufficient:'训练或后时段验证证据不足', margin:'功耗余量或干扰验证未通过', validated:'后时段验证通过',
    evidence_lost:'支持证据已被保留时间或容量清理'};
  $('learning-state').textContent = learning.error || (learning.eligibility?.phase === 'eligible' ? '可纳入空闲功耗基线'
    : labels[learning.eligibility?.reason] || '等待已落盘的真实观测');
  $('learning-published').textContent = hosts.slice(0, 2).map(host => {
    const policy = learning.published?.[host];
    return policy ? `${host} 已发布 ${numeric(policy.threshold_w, 0)} W · ${policy.source} · 版本 ${policy.version} · ${learning.active?.[host] ? '当前生效' : '当前条件阻止激活'} · 生成 ${historyTime(policy.generated_at)}` : `${host} 尚无有效发布，固定 80 W 常规前馈`;
  }).join('；');
  const report = learning.candidate || {};
  $('learning-candidate').textContent = hosts.slice(0, 2).map(host => {
    const proof = report.hosts?.[host];
    return proof ? `${host} 当前候选 ${numeric(proof.candidate_w, 0)} W / 限步 ${numeric(proof.threshold_w, 0)} W · ${labels[proof.reason] || proof.reason} · 训练 ${proof.train_buckets} 桶 / ${numeric(proof.train_seconds, 0)} 秒 · 后 1 小时 ${proof.holdout_buckets} 桶 / ${numeric(proof.holdout_seconds, 0)} 秒 · 实际 ${proof.gear} 档 · ${proof.source}` : `${host} 候选证据不足`;
  }).join('；');
  $('learning-schedule').textContent = `每 6 小时评估最近最多 72 小时。报告生成 ${historyTime(report.generated_at)}；窗口 ${historyTime(report.window_from)} 至 ${historyTime(report.window_to)}；下次 ${historyTime(learning.next_at)}。`;
  $('learning-storage').textContent = `学习检查点 ${numeric((learning.checkpoint_bytes || 0) / 1048576, 2)} MiB；容量内预留 ${numeric((learning.reserved_bytes || 0) / 1048576, 2)} MiB。保留清理 ${learning.loss?.retention_buckets || 0} 桶，容量清理 ${learning.loss?.cap_buckets || 0} 桶，原始写入失败 ${learning.loss?.raw_append || 0} 次；已恢复崩溃尾文件 ${numeric(learning.loss?.crash_bytes || 0, 0)} 字节。`;
}

function draw() {
  $('legend').innerHTML = hosts.map((host, i) => `<span><i class="color-${i}"></i>${host}</span>`).join('');
  if (!rows.length) { $('chart').textContent = '尚无真实历史记录，采集开始后将显示曲线。'; return; }
  const values = rows.flatMap(row => row.nodes.map(node => node[metric])).filter(Number.isFinite);
  if (!values.length) { $('chart').textContent = '这个时间范围内没有该类温度记录。'; return; }
  const width = 1000, height = 250, left = 46, right = 12, top = 12, bottom = 32;
  const min = Math.max(0, Math.floor((Math.min(...values) - 4) / 5) * 5);
  const hard = current?.decision?.limits?.[metric.split('_')[0]]?.[1] ?? 75;
  const max = Math.ceil((Math.max(hard, ...values) + 3) / 5) * 5;
  const start = rows[0].ts, end = Math.max(start + 60, rows.at(-1).ts);
  const x = stamp => left + (stamp - start) / (end - start) * (width - left - right);
  const y = value => top + (max - value) / (max - min) * (height - top - bottom);
  let body = '';
  for (let i = 0; i <= 4; i++) {
    const value = min + (max - min) * i / 4;
    body += `<line x1="${left}" x2="${width-right}" y1="${y(value)}" y2="${y(value)}" stroke="#263241"/><text x="${left-9}" y="${y(value)+4}" text-anchor="end" fill="#91a2b6" font-size="10">${value.toFixed(0)}°</text>`;
  }
  body += `<line x1="${left}" x2="${width-right}" y1="${y(hard)}" y2="${y(hard)}" stroke="#a57552" stroke-dasharray="5 5"/><text x="${width-right}" y="${y(hard)-5}" text-anchor="end" fill="#d5a36e" font-size="10">${hard}°C 绝对硬线 · ${hard - 2}°C 实温预缓冲</text>`;
  hosts.forEach((host, index) => {
    let path = '', connected = false;
    rows.forEach(row => {
      const node = row.nodes.find(item => item.host === host);
      const value = node?.[metric];
      if (!Number.isFinite(value)) { connected = false; return; }
      if (node?.gap) {
        body += `<circle cx="${x(row.ts)}" cy="${y(value)}" r="2.5" fill="${colors[index]}"/>`;
        connected = false; return;
      }
      path += `${connected ? 'L' : 'M'}${x(row.ts).toFixed(1)},${y(value).toFixed(1)} `;
      connected = true;
    });
    body += `<path d="${path}" fill="none" stroke="${colors[index]}" stroke-width="2" stroke-linejoin="round"/>`;
  });
  for (let i = 0; i <= 4; i++) {
    const stamp = start + (end - start) * i / 4;
    body += `<text x="${x(stamp)}" y="${height-8}" text-anchor="${i===0?'start':i===4?'end':'middle'}" fill="#91a2b6" font-size="10">${new Date(stamp * 1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</text>`;
  }
  $('chart').innerHTML = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${body}</svg>`;
  $('history-note').textContent = `实际记录从 ${new Date(start * 1000).toLocaleString('zh-CN')} 开始。容量和保留时间可能缩短所选范围。`;
}

async function refresh() {
  try { render(await api('/api/status')); }
  catch {
    if (current) render({...current, controller_age_seconds: Infinity});
    $('alarm').className = 'alarm bad'; $('alarm').textContent = '无法连接温控服务。当前读数已隐藏，不能视为实时数据。';
    $('live-text').textContent = '连接中断'; $('live-dot').style.background = '#ff8791';
  }
  setTimeout(refresh, 3000);
}
function historyTime(stamp) {
  return Number.isFinite(stamp) && stamp >= 0 ? new Date(stamp * 1000).toLocaleString('zh-CN') : '未知';
}

function ambientReadback(fan, at) {
  if (!Number.isFinite(fan.ambient_c)) return '净化器本地温度未记录 · 未校准环境参考，仅记录，尚未用于控制';
  const age = Number.isFinite(at) && Number.isFinite(fan.ambient_verified_at) && fan.ambient_verified_at >= 0
    ? at - fan.ambient_verified_at : null;
  const state = age === null ? '采样时效未知' : age < 0 ? '采样时间异常' : age > 9 ? '采样已过期' : '采样有效';
  return `净化器本地温度 ${numeric(fan.ambient_c)} °C · 未校准环境参考，仅记录，尚未用于控制 · ${state} · 采样 ${historyTime(fan.ambient_verified_at)} · 来源 ${fan.ambient_source || '未知'}`;
}

function historyReadback(sample) {
  const fan = sample.fan || {};
  const age = Number.isFinite(sample.snapshot_at) && Number.isFinite(fan.verified_at)
    ? sample.snapshot_at - fan.verified_at : null;
  let state = age === null ? '读回时效未知' : age < 0 ? '读回时间异常' : age > 9 ? '读回已过期' : '已读回，未确认匹配';
  if (age !== null && age >= 0 && age <= 9 && fan.configured && fan.confirmed && !fan.fault
      && fan.execution !== 'fault' && fan.power === 'on' && fan.mode === 'favorite'
      && Number.isFinite(fan.level) && fan.level === sample.requested_level && Number.isFinite(fan.rpm) && fan.rpm > 0) {
    state = '记录时实际匹配';
  }
  return `${state}${age !== null && age >= 0 ? ` · 距快照 ${numeric(age)} 秒` : ''}`;
}

function trialDetails(decision) {
  const operation = decision?.execution_trial;
  const experiment = decision?.trial;
  const current = operation ? ` 当前执行 ${operation.from}→${operation.to} · ${operation.status} · ${operation.to < operation.from ? `计划下降 ${operation.planned_step || 1} 档` : '上调请求'} · 请求 ${numeric(operation.requested_at, 3)} · 原截止 ${numeric(operation.original_deadline, 3)} · 目的请求 ${numeric(operation.destination_requested_at, 3)}。` : '';
  const previous = experiment ? ` 下降试验 ${experiment.from}→${experiment.to} · ${experiment.outcome || experiment.status}。` : '';
  return current + previous;
}

function renderHistory(data) {
  const samples = data.samples || [];
  $('history-count').textContent = `显示 ${samples.length} 条，按日志写入顺序倒序，最多 200 条。`
    + (data.samples_truncated ? '所选范围还有更早记录，表格已截断；选择更早的结束时间可继续查看。' : '所选范围内未截断。');
  $('history-samples').innerHTML = samples.map(sample => {
    const fan = sample.fan || {};
    const manual = sample.control?.manual_level;
    const mode = manual === null ? '自动' : Number.isInteger(manual) && manual >= 0 && manual <= 16 ? `手动 ${manual} 档` : '未知（旧记录未记录）';
    const nodes = hosts.map(host => {
      const node = sample.nodes?.find(item => item.host === host);
      if (!node) return '<td>节点记录缺失</td>';
      const age = Number.isFinite(sample.snapshot_at) && Number.isFinite(node.sampled_at) ? sample.snapshot_at - node.sampled_at : null;
      const state = node.online === false ? '离线 / 缺失' : node.online !== true ? '在线状态未知'
        : age === null ? '采样时效未知' : age < 0 ? '采样时间异常' : age > 9 ? '采样已过期' : '记录时在线';
      return `<td>${numeric(node.cpu_temp)} / ${numeric(node.gpu_temp)} / ${numeric(node.board_temp)} °C<small>${state}</small><small>采样 ${escape(historyTime(node.sampled_at))}</small><small>${numeric(node.power_w)} W · ${escape(powerLabel(node))}</small></td>`;
    }).join('');
    const policies = Object.entries(sample.active_power_policy || {}).map(([host, policy]) => `${host} 整机 ${numeric(policy.threshold_w, 0)} W · ${policy.source} · 发布版本 ${policy.version}`).join('；');
    return `<tr><td>${escape(historyTime(sample.ts))}<small>快照 ${escape(historyTime(sample.snapshot_at))}</small></td><td>${mode}</td><td>${numeric(sample.requested_level, 0)}</td><td>${numeric(fan.level, 0)} 档<small>${escape(historyReadback(sample))}</small><small>${escape(fan.power ?? '电源未知')} / ${escape(fan.mode ?? '模式未知')}</small><small>${escape(fan.error || '')}</small><small>${escape(ambientReadback(fan, sample.snapshot_at))}</small></td><td>${numeric(fan.rpm, 0)} RPM</td><td>${escape(historyTime(fan.verified_at))}</td>${nodes}<td>${(sample.decision?.reasons || []).map(escape).join('<br>') || '未记录原因'}<small>当前场景：${escape(sample.decision?.scenario_detail || sample.decision?.scenario || '旧记录未标注场景')}</small>${trialDetails(sample.decision) ? `<small>${escape(trialDetails(sample.decision))}</small>` : ''}<small>${escape(policies || '固定保护；未记录生效学习参数')}</small></td></tr>`;
  }).join('') || '<tr><td colspan="11">所选保留时段没有记录。容量或保留时间可能已清理更早数据。</td></tr>';
  $('history-note').textContent = `${historyTime(data.from)} 至 ${historyTime(data.to)}。曲线为时间桶最高温，表格为原始快照。容量和保留时间可能缩短范围。`;
}

function clearHistory(message) {
  rows = [];
  draw();
  $('history-note').textContent = message;
  $('history-count').textContent = message;
  $('history-samples').innerHTML = `<tr><td colspan="11">${escape(message)}</td></tr>`;
}

async function history() {
  const generation = ++historyGeneration;
  const end = $('history-until').value;
  let path = `/api/history?hours=${encodeURIComponent($('hours').value)}`;
  if (end) {
    const until = new Date(end).getTime() / 1000;
    if (!Number.isFinite(until) || until < 0 || until > Date.now() / 1000) {
      clearHistory('请选择有效且不晚于现在的结束时间。');
      return;
    }
    path += `&until=${until}`;
  }
  clearHistory('正在读取所选时段。');
  try {
    const data = await api(path);
    if (generation !== historyGeneration) return;
    rows = data.rows;
    draw();
    renderHistory(data);
  } catch (error) {
    if (generation !== historyGeneration) return;
    clearHistory(error.status === 503 ? '历史读取超出预算，请缩小时间范围或选择更早的结束时间。' : '历史读取失败，实时监控继续运行。请重试。');
  }
}
$('hours').addEventListener('change', history);
$('history-until').addEventListener('input', () => {
  historyGeneration++;
  clearHistory('结束时间已修改，请点击查询。留空后查询可跟随实时。');
});
$('history-query').addEventListener('click', history);
document.querySelectorAll('[data-metric]').forEach(button => button.addEventListener('click', () => {
  metric = button.dataset.metric;
  document.querySelectorAll('[data-metric]').forEach(item => item.classList.toggle('selected', item === button));
  draw();
}));
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (settingsSaving) return;
  const proposed = {retention_days:Number($('retention').value), max_log_mb:Number($('capacity').value),
    manual_level:$('control-mode').value === 'automatic' ? null : Number($('control-mode').value)};
  settingsSaving = true;
  settingsControls.forEach(id => { $(id).disabled = true; });
  $('settings-result').textContent = '正在保存，设备执行仍以实际读回为准。';
  try {
    const result = await api('/api/settings', proposed);
    if (current) current.settings = result.settings;
    adoptSettings(result.settings);
    settingsDirty = settingsUnknown = false;
    $('settings-result').textContent = result.maintenance_error || '设置已保存。设备执行尚需受限控制与实际读回确认。';
  } catch (error) {
    settingsUnknown = !error.status || error.status === 503 || error.status === 409;
    if (settingsUnknown) settingsDirty = false;
    $('settings-result').textContent = settingsUnknown ? '保存结果未知，正在刷新服务器状态。请勿将当前草稿视为已执行。' : error.message;
  } finally {
    settingsSaving = false;
    settingsControls.forEach(id => { $(id).disabled = false; });
  }
  if (settingsUnknown) {
    try { render(await api('/api/status')); }
    catch { $('settings-result').textContent = '保存结果未知，状态刷新失败。恢复连接后核对服务器已保存设置。'; }
  }
});
['retention', 'capacity', 'control-mode'].forEach(id => $(id).addEventListener('input', () => {
  settingsDirty = true;
  $('settings-result').textContent = '草稿尚未保存。';
}));
$('settings-cancel').addEventListener('click', () => {
  if (!current || settingsSaving) return;
  adoptSettings(current.settings);
  settingsDirty = false;
  $('settings-result').textContent = '草稿已取消，恢复服务器已保存设置。';
});
let qrOwner = null;
let qrPhase = '';
let qrDeadline = 0;
let qrTimer = null;
let qrImageLoaded = false;
let qrFailures = 0;
let qrDevices = [];
const qrMessages = {preparing:'正在向米家获取二维码。', waiting:'请用米家 App 扫码，并在手机上确认登录。',
  fetching:'已登录，正在读取账号设备和家庭设备列表。', ready:'枚举完成，已显示本次返回的米家设备列表。请选择可绑定的固定净化器，再勾选确认。尚未绑定或控制。',
  bind_queued:'绑定等待中，仍可取消。', binding:'已开始固定身份验证与保存；请勿重复绑定，等待结果。',
  bound:'固定身份已验证，凭据已保存。电机执行仍须查看概览中的实际转速读回。',
  expired:'扫码会话已过期，临时凭据已清除。请重新开始。', canceled:'扫码已取消，未保存的临时凭据已清除。',
  failed:'连接失败，未完成绑定。请检查账号地区、联网及固定设备身份后重试。'};
const qrErrors = {target_missing:'枚举完成，但本次列表中未找到固定净化器。已查询账号设备和家庭设备，列表缺失不代表账号没有该设备。', target_mismatch:'固定净化器的身份或凭据不匹配。',
  credential_unavailable:'已找到固定净化器，但本次未返回控制凭据，无法绑定。共享设备可能不提供控制凭据。',
  device_conflict:'设备列表含有冲突记录，请重新扫码。',
  unsupported_protocol:'米家返回了未支持的协议或地址。已安全停止，不会放宽连接规则。',
  binding_failed:'设备身份验证或保存失败。请检查净化器在线状态。',
  cleanup_pending:'上次会话仍在清理，请稍后重新开始。',
  session_busy:'另一个页面已有扫码会话，请在原页面完成或等待它过期。',
  session_missing:'服务器已无此会话，可能已重启。请重新开始。'};

function qrControls() {
  const active = qrOwner !== null;
  $('qr-start').disabled = active || !$('qr-consent').checked;
  $('qr-consent').disabled = active;
  $('qr-cancel').disabled = !active || ['canceling', 'binding', 'bound'].includes(qrPhase);
  $('qr-devices').disabled = !active || qrPhase !== 'ready' || qrDevices.length === 0;
  $('qr-bind-consent').disabled = !active || qrPhase !== 'ready'
    || !qrDevices.some(device => device.did === $('qr-devices').value && device.selectable);
  $('qr-bind').disabled = $('qr-bind-consent').disabled || !$('qr-bind-consent').checked;
}

function qrDisplay(result) {
  if (qrPhase === 'canceling' && !['canceled', 'expired', 'failed', 'binding', 'bound'].includes(result.phase)) return;
  qrPhase = result.phase;
  const selected = $('qr-devices').value;
  qrDevices = qrPhase === 'ready' ? result.devices || [] : [];
  const placeholder = new Option('请选择设备，选择不会启动设备', '');
  const options = qrDevices.map(device => {
    const reason = device.selectable ? '固定目标，待绑定验证' : device.reason === 'credential_unavailable'
      ? qrErrors.credential_unavailable : '尚未验证此物理设备的控制范围，不支持绑定';
    const online = device.online === true ? '在线' : device.online === false ? '离线' : '在线状态未知';
    const option = new Option(`${device.name} · ${device.model || '型号未知'} · ${device.ip || 'IP 未知'} · DID ${device.did} · ${online} · ${reason}`, device.did);
    option.disabled = !device.selectable;
    return option;
  });
  $('qr-devices').replaceChildren(placeholder, ...options);
  $('qr-devices').value = qrDevices.some(device => device.did === selected && device.selectable) ? selected : '';
  if (!$('qr-devices').value) $('qr-bind-consent').checked = false;
  $('qr-result').textContent = (qrErrors[result.code] || qrMessages[qrPhase] || '连接状态未知。')
    + (['preparing', 'waiting', 'fetching', 'ready', 'bind_queued'].includes(qrPhase) ? ` 剩余 ${result.remaining_seconds} 秒。` : '');
  if (qrPhase !== 'waiting') $('qr-image').hidden = true;
  if (['bound', 'expired', 'canceled', 'failed'].includes(qrPhase)) {
    qrOwner = null;
    $('qr-bind-consent').checked = $('qr-consent').checked = false;
  }
  qrControls();
}

async function qrPoll(owner) {
  if (owner !== qrOwner) return;
  if (Date.now() >= qrDeadline + (qrPhase === 'binding' ? 45000 : 0)) {
    qrOwner = null;
    $('qr-image').hidden = true;
    $('qr-result').textContent = '本页已停止等待。临时会话会自动过期；若曾开始绑定，请核对概览的已配置状态，不要把等待超时当成绑定失败。';
    $('qr-bind-consent').checked = $('qr-consent').checked = false;
    qrControls();
    return;
  }
  try {
    const result = await api('/api/xiaomi/qr/status', {owner});
    if (owner !== qrOwner) return;
    qrFailures = 0;
    qrDisplay(result);
    if (qrOwner && qrPhase === 'waiting' && result.image_ready && !qrImageLoaded) {
      const response = await fetch('/api/xiaomi/qr/image', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({owner}), signal:AbortSignal.timeout(10000), cache:'no-store'});
      if (!response.ok) throw Error('image_unavailable');
      const bitmap = await createImageBitmap(await response.blob());
      try {
        if (owner === qrOwner && qrPhase === 'waiting') {
          $('qr-image').getContext('2d').drawImage(bitmap, 0, 0, 480, 480);
          $('qr-image').hidden = false;
          qrImageLoaded = true;
        }
      } finally { bitmap.close(); }
    }
  } catch (error) {
    if (owner !== qrOwner) return;
    qrFailures++;
    $('qr-image').hidden = true;
    qrImageLoaded = false;
    $('qr-bind-consent').checked = false;
    if (qrPhase !== 'canceling') qrPhase = 'unknown';
    qrControls();
    $('qr-result').textContent = qrErrors[error.message] || '连接暂时中断，结果未知；正在有限重试，不会自动重新登录或绑定。';
    if (error.status === 410) {
      qrDisplay({phase:'expired', code:'session_missing', remaining_seconds:0});
    }
  }
  if (owner === qrOwner) qrTimer = setTimeout(() => qrPoll(owner), qrFailures >= 3 ? 10000 : 2000);
}

$('qr-consent').addEventListener('change', qrControls);
$('qr-bind-consent').addEventListener('change', qrControls);
$('qr-devices').addEventListener('change', () => {
  $('qr-bind-consent').checked = false;
  qrControls();
});
$('qr-start').addEventListener('click', async () => {
  if (qrOwner || !$('qr-consent').checked) return;
  qrOwner = Array.from(crypto.getRandomValues(new Uint8Array(32)), byte => byte.toString(16).padStart(2, '0')).join('');
  const owner = qrOwner;
  qrDeadline = Date.now() + 180000;
  qrImageLoaded = false;
  qrFailures = 0;
  $('qr-bind-consent').checked = false;
  qrDisplay({phase:'preparing', remaining_seconds:180});
  try {
    const result = await api('/api/xiaomi/qr/start', {owner, consent:'community-qr-c4db715-v1'});
    if (owner === qrOwner) qrDisplay(result);
  }
  catch (error) {
    if (owner !== qrOwner) return;
    $('qr-result').textContent = qrErrors[error.message] || '启动结果未知，正在检查同一会话，不会重复启动。';
    if (error.status && error.status < 500) qrDisplay({phase:'failed', code:error.message});
  }
  if (owner === qrOwner) await qrPoll(owner);
});
$('qr-cancel').addEventListener('click', async () => {
  if (!qrOwner || ['canceling', 'binding', 'bound'].includes(qrPhase)) return;
  const owner = qrOwner;
  qrPhase = 'canceling';
  $('qr-bind-consent').checked = false;
  qrControls();
  try {
    const result = await api('/api/xiaomi/qr/cancel', {owner});
    if (owner === qrOwner) qrDisplay(result);
  } catch {
    if (owner !== qrOwner) return;
    qrPhase = 'unknown';
    $('qr-result').textContent = '取消结果未知。请等待状态刷新或会话自动过期；不要重新授权。';
    qrControls();
  }
});
$('qr-bind').addEventListener('click', async () => {
  const did = $('qr-devices').value;
  if (!qrOwner || qrPhase !== 'ready' || !$('qr-bind-consent').checked
      || !qrDevices.some(device => device.did === did && device.selectable)) return;
  const owner = qrOwner;
  qrPhase = 'bind_queued';
  $('qr-bind-consent').checked = false;
  qrControls();
  $('qr-result').textContent = '正在提交绑定，尚不能确认保存或电机执行。';
  try {
    const result = await api('/api/xiaomi/qr/bind', {owner, did, consent:'save-fixed-device-and-protected-control-v1'});
    if (owner === qrOwner) qrDisplay(result);
  } catch {
    if (owner !== qrOwner) return;
    $('qr-result').textContent = '绑定结果未知，请等待同一会话状态和概览实际读回；不会自动重复绑定。';
  }
});
window.addEventListener('pagehide', () => {
  clearTimeout(qrTimer);
  if (qrOwner && !['binding', 'bound'].includes(qrPhase)) fetch('/api/xiaomi/qr/cancel', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({owner:qrOwner}), keepalive:true}).catch(() => {});
  $('qr-image').hidden = true;
});
qrControls();
refresh(); history(); setInterval(() => { if (!$('history-until').value) history(); }, 30000);
