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
const modeOptions = [new Option('软件自动温控 · 固定规则平稳调节', 'automatic'),
  ...Array.from({length:17}, (_, level) => new Option(`手动最低档 ${level} 档${level === 0 ? ' · 仍在运转' : ''}`, String(level)))];
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
  const fresh = Number.isFinite(data.controller_age_seconds) && data.controller_age_seconds >= 0
    && data.controller_age_seconds <= 6 && Number.isFinite(data.server_time) && data.server_time >= 0
    && Number.isFinite(data.updated_at) && data.updated_at >= 0
    && data.server_time - data.updated_at >= 0 && data.server_time - data.updated_at <= 9;
  const online = data.nodes.filter(node => node.online && fresh).length;
  $('online-count').textContent = `${online} / 4 在线`;
  $('live-text').textContent = fresh ? '实时采集' : '采集异常';
  $('live-dot').style.background = fresh ? '#47dfb6' : '#ff8791';
  $('updated').textContent = data.updated_at ? `更新于 ${new Date(data.updated_at * 1000).toLocaleTimeString('zh-CN')}` : '等待采样';
  $('policy').textContent = data.settings.manual_level === null ? '已保存 · 软件自动温控' : `已保存 · 手动最低档 ${data.settings.manual_level} 档`;
  const enhanced80 = data.decision?.policy_spec?.enhanced_admission_c === 80;
  $('policy-detail').textContent = `当前场景：${data.decision?.scenario_detail || '旧记录未标注场景'}。有效请求包含温度与通信保护。保存设置后仍须等待受限执行和设备读回。` + trialDetails(data.decision) + (data.high_wind_gate ? ` 当前${enhanced80 ? '普通上限' : '可写上限'} ${data.high_wind_limit} 档。` + highWindDetails(data.decision) + (enhanced80 ? ' 普通上限不等于退出过程的本步指令或实际档位。故障保护例外须有独立证据，实际以正转速读回为准。' : ' 实际档位以受限执行和转速读回为准。撤权冻结未获资格的下降。') : '');
  if (data.settings.manual_level !== null) $('policy-detail').textContent += ` 所选最低档 ${data.settings.manual_level} 档是手动设置，温度与通信保护可提高需求，高风门控可限制有效请求；不保证固定在所选档位。`;
  $('policy-detail').textContent += controlDetails(data.decision, false);
  const memory = data.thermal_memory;
  if (memory && memory.state !== 'not_applicable') $('policy-detail').textContent += memoryDetails(memory);
  renderLearning(data.learning || {});
  const spec = data.decision?.policy_spec;
  $('policy-parameters').textContent = spec ? `策略 ${spec.version}。档位 ${spec.minimum_level} 至 ${spec.maximum_level}，普通权限上限 ${spec.ordinary_ceiling}。`
    + ` 工作目标 CPU ${spec.targets_c?.cpu}°C / GPU ${spec.targets_c?.gpu}°C / 其他 ${spec.targets_c?.board}°C，可信实温危急线 ${spec.critical_c}°C。`
    + ` 自动每次下降 ${spec.descent_step} 档，合格观察 ${spec.observe_seconds} 秒；空闲余量 ${spec.idle_margin_c}°C，稳定负载或慢回升余量 ${spec.loaded_margin_c}°C。`
    + ` 负载预备可比观察 ${spec.preparation_seconds} 秒；慢回升风险范围 ${spec.drift_horizon_seconds} 秒，常规慢回升斜率 ${spec.drift_rate_c_per_second}°C/s，累计额度 ${spec.warming_budget_c}°C。有界观察不代表允许执行；已减缓的小幅回温另验同源余量。`
    + (enhanced80 ? ' 普通增强须可信实温达到80°C。80°C不是器件安全上限。低于80°C且实际高于10档时，按电机约束逐档退出至10档，不等待普通180秒观察或负载可比窗口；可靠保存、真实反馈和故障保护仍有效。'
      : spec.high_exit ? ` 高档退出单独核验，先取本档趋势 ${spec.high_exit.trend_seconds} 秒，再积累 ${spec.high_exit.observation_seconds} 秒合格观察；距危急线的原始预测余量至少${spec.high_exit.margin_c}°C，退出链温升额度${spec.high_exit.warming_budget_c}°C。` : '')
    + ' 均为暂定工程值，尚非现场标定保证。' : '服务未返回当前策略规格，不能从旧页面推断已部署策略。';
  $('desired').textContent = data.requested_level;
  $('gear-bar').innerHTML = Array.from({length:16}, (_, index) => `<i class="${index < data.requested_level ? 'on' : ''}"></i>`).join('');
  const fanAt = Number.isFinite(data.controller_age_seconds) && data.controller_age_seconds >= 0
    && data.controller_age_seconds <= 6 ? data.server_time : null;
  const readback = fanReadback(data.fan, fanAt, data.requested_level);
  const fanFresh = readback.fresh;
  const fanConfirmed = readback.matched;
  $('actual').textContent = !data.fan.configured ? '待绑定' : readback.actual !== null ? `${readback.actual} 档` : '未确认';
  $('fan-setting').textContent = fanSetting(data.fan);
  $('rpm').textContent = fanFresh ? `${numeric(data.fan.rpm, 0)} RPM · ${data.fan.mode || '模式未知'}` : '尚无有效转速读回';
  $('readback-time').textContent = `读回于 ${historyTime(data.fan.verified_at)}${fanFresh ? '' : ' · 当前不可确认'}`;
  const ambientAt = Number.isFinite(data.controller_age_seconds) && data.controller_age_seconds >= 0
    && data.controller_age_seconds <= 6 ? data.server_time : null;
  $('ambient').textContent = ambientReadback(data.fan, ambientAt)
    + (Number.isFinite(ambientAt) ? '' : ' · 当前不可确认');
  $('fan-status').textContent = fanConfirmed ? '电源、模式、档位与转速已读回' : data.fan.error || (readback.actual !== null ? '实际档位已知，当前请求尚未确认' : '等待当前请求的设备读回');
  $('execution').textContent = data.fan.fault || data.fan.execution === 'fault' ? '故障锁定 · 只读观察' : data.fan.execution === 'pending' ? '最新请求待执行' : fanConfirmed ? '实际匹配' : '等待读回';
  $('pair-state').textContent = data.fan.configured ? '凭据已配置' : '需要连接';
  $('binding-state').textContent = !data.fan.configured ? '尚未配置本地执行凭据。' : fanConfirmed ? '本地凭据已配置，当前请求已获实际读回确认。现场散热标定仍待完成。' : '本地凭据已配置，当前执行尚未确认。' + (data.fan.error || '等待读回。');
  let alarm = '';
  if (!fresh) alarm = `采集循环异常。当前温度不可视为实时，保护请求 ${data.requested_level} 档。`;
  else if (['readback_hold', 'communication_hold'].includes(data.decision?.scenario)) alarm = fanConfirmed
    ? `通信恢复确认中，请求 ${data.requested_level} 档，暂停降档。`
    : `通信暂时中断，请求 ${data.requested_level} 档并暂停降档；实际风力${readback.actual !== null ? `为 ${readback.actual} 档` : '未知'}，正在重试。`;
  else if (online < 4) alarm = `部分节点采集失败，实际温度未知；当前保护请求 ${data.requested_level} 档。`;
  else if (data.decision?.scenario === 'protect' || data.nodes.some(node =>
           node.cpu_temp >= (data.decision?.limits?.cpu?.[1] ?? 75) - 2
           || node.gpu_temp >= (data.decision?.limits?.gpu?.[1] ?? 75) - 2
           || node.board_temp >= (data.decision?.limits?.board?.[1] ?? 75) - 2 || node.thermal_throttle))
    alarm = `当前保护请求 ${data.requested_level} 档。${data.fan.fault ? '执行预算故障锁定，保持只读。' : fanConfirmed ? '已匹配实际读回；RPM 不证明散热能力。' : '实际执行尚未确认，请查看具体保护原因。'}`;
  else if (data.nodes.some(node => node.cpu_temp >= (data.decision?.limits?.cpu?.[0] ?? 60)
           || node.gpu_temp >= (data.decision?.limits?.gpu?.[0] ?? 60)))
    alarm = `温度达到 60°C 准备区，需求至少 10 档。当前请求 ${data.requested_level}，实际 ${readback.actual ?? '未确认'} 档；持续缺口另行逐级评估。`;
  else if (data.storage_health?.phase === 'holding') alarm = `日志暂态中断，限时暂停下降；当前实际 ${readback.actual ?? '未知'} 档，温度保护继续有效。`;
  else if (memory && !['ready', 'not_applicable'].includes(memory.state)) alarm = memoryDetails(memory);
  else if (!fanConfirmed) alarm = data.fan.error || (readback.actual !== null ? `实际 ${readback.actual} 档，当前请求尚未确认匹配。温度记录正在运行。` : '风力尚未确认。温度记录正在运行。');
  else if (data.settings_error || data.storage_error) alarm = data.settings_error || data.storage_error;
  $('alarm').className = `alarm ${alarm ? 'bad' : 'ok'}`;
  $('alarm').textContent = alarm || '四台节点实时采集，散热设备读回正常。';
  const expanded = new Set([...$('nodes').querySelectorAll('details[open]')].map(item => item.dataset.host));
  $('nodes').innerHTML = hosts.map(host => {
    const node = data.nodes.find(item => item.host === host) || {sensors:[]};
    const valid = node.online && fresh;
    const slope = data.decision.rise_c_per_min?.[host];
    return `<article class="node ${valid ? '' : 'offline'}"><div class="node-head"><b>${host}</b><span>${valid ? '● 在线' : '● 数据缺失'}</span></div><div class="node-name">${escape(node.hostname || '等待节点响应')}</div><div class="temp-main"><b>${valid ? numeric(node.cpu_temp) : '—'}</b><span> °C</span><small>${node.cpu_proxy === false ? 'CPU / 平台保护最高温' : 'CPU 保护代理 · ACPI 最热区'}</small></div><div class="temp-sub"><span>GPU <b>${valid ? numeric(node.gpu_temp) : '—'}°</b></span><span>非GPU硬件最高 <b>${valid ? numeric(node.board_temp) : '—'}°</b></span></div><div class="node-load"><span>CPU ${valid ? numeric(node.cpu_load, 0) : '—'}%</span><span>GPU ${valid ? numeric(node.gpu_load, 0) : '—'}%</span><span>${valid ? numeric(node.power_w, 0) : '—'} W</span></div><small>${escape(powerLabel(node))}</small><div class="rise">${valid && slope >= 0.1 ? `↑ 最大升温趋势 ${numeric(slope)}°C / 分钟` : valid ? '当前未发现明显升温趋势' : `缺失数据，当前保护请求 ${data.requested_level} 档`}</div><details data-host="${host}" ${expanded.has(host) ? 'open' : ''}><summary>查看 ${node.sensors?.length || 0} 个传感器</summary><table><tbody>${(node.sensors || []).map(sensor => `<tr class="${sensor.calibrated ? '' : 'uncalibrated'}"><td>${escape(sensor.group)} / ${escape(sensor.label)}${sensor.calibrated ? '' : ' · 校准差异'}</td><td>${numeric(sensor.temp)}°C</td></tr>`).join('')}</tbody></table></details></article>`;
  }).join('');
  $('reasons').innerHTML = (data.decision.reasons || []).map(reason => `<li>${escape(reason)}</li>`).join('');
  $('log-size').textContent = `${numeric(data.log_bytes / 1048576, 2)} / ${data.settings.max_log_mb} MiB`;
  $('usage-fill').style.width = `${Math.min(100, data.log_bytes / (data.settings.max_log_mb * 1048576) * 100)}%`;
  $('storage-status').textContent = data.storage_error || `控制周期 ${numeric(data.control_period_seconds ?? data.period_seconds, 1)} 秒 · 记录周期 ${numeric(data.record_period_seconds ?? data.period_seconds, 1)} 秒 · 保留 ${data.settings.retention_days} 天 · 丢失记录 ${data.dropped_records}`;
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
    $('learning-candidate').textContent = '自动每次只降 1 档，先取得至少 180 秒完整合格观察；实际正转速确认后重新观察。空闲目标余量至少 5°C，非空闲至少 8°C；不因安全档吹凉或时间经过清除失败低档记忆。';
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

function fanReadback(fan, at, requested) {
  const age = Number.isFinite(at) && at >= 0 && Number.isFinite(fan.verified_at) && fan.verified_at >= 0
    ? at - fan.verified_at : null;
  const fresh = age !== null && age >= 0 && age <= 9;
  const actual = fresh && fan.configured === true && fan.power === 'on' && fan.mode === 'favorite'
    && Number.isInteger(fan.level) && fan.level >= 0 && fan.level <= 16
    && Number.isFinite(fan.rpm) && fan.rpm > 0 && !fan.fault && fan.execution !== 'fault' ? fan.level : null;
  const matched = actual !== null && fan.confirmed === true && actual === requested
    && (fan.execution === 'matched' || fan.execution == null);
  return {age, fresh, actual, matched};
}

function fanSetting(fan) {
  const mode = {auto:'设备自动', favorite:'favorite 定档'}[fan.mode] || fan.mode || '模式未知';
  return `${fan.power ?? '电源未知'} / ${mode} · favorite 设置 ${fan.level ?? '未知'} · 设置值本身不等于实际档位`;
}

function historyReadback(readback) {
  const {age, actual, matched} = readback;
  let state = age === null ? '读回时效未知' : age < 0 ? '读回时间异常' : age > 9 ? '读回已过期' : '已读回，未确认匹配';
  if (actual !== null) state = matched ? '记录时实际匹配' : '记录时实际档位已知，未确认请求匹配';
  return `${state}${age !== null && age >= 0 ? ` · 距快照 ${numeric(age)} 秒` : ''}`;
}

function highWindDetails(decision) {
  const high = decision?.high_wind;
  if (!high || typeof high !== 'object') return '高风证据未知，旧记录不补推断。';
  const labels = {normal:'普通需求', observing:'实际10档观察中', empirical:'有界实测增强', recovering:'安全退出或精确恢复', critical:'当前实温危急保护', expired:'实测周期已到期', degraded:'证据不足，暂停下降', source_fault:'温度来源异常诊断'};
  const exceptions = {SourceFaultProtection:'温度来源故障保护例外', FailedDescentRestoration:'真实降档失败的精确恢复例外'};
  const exception = high.exception ? (typeof high.exception === 'string' && Object.hasOwn(exceptions, high.exception)
    ? exceptions[high.exception] : '保护例外类型未知，不能据此确认高风权限') : '';
  const enhanced80 = decision?.policy_spec?.enhanced_admission_c === 80;
  const source = Array.isArray(high.source) ? high.source.map(value => String(value).slice(0, 160)).join(' / ') : '来源未知';
  const admission = enhanced80
    ? ' 普通增强门槛为可信实温80°C。75°C风险告警、工作目标不足、功率与预测均不能单独授予高档；80°C不是器件安全上限。'
    : '工作60/65°C、危急75°C均为暂定工程值。未经标定的预测不单独授予高风增强权限。';
  const state = enhanced80 && high.reason === 'critical' ? '可信实温达到普通增强准入门槛' : labels[high.reason] || '高风状态未知';
  return (exception ? `${exception}，不是普通增强，实际方向以本步指令和读回为准。` : '')
    + `${state}。${String(high.detail || '').slice(0, 240)} 来源 ${source}，实温 ${numeric(high.actual_c)}°C，观察 ${numeric(high.observation_seconds, 0)}秒。` + admission;
}

function trialDetails(decision) {
  const operation = decision?.execution_trial;
  const experiment = decision?.trial;
  const current = operation ? ` 当前执行 ${operation.from}→${operation.to} · ${operation.status} · ${operation.to < operation.from ? `计划下降 ${operation.planned_step || 1} 档` : '上调请求'} · 请求 ${numeric(operation.requested_at, 3)} · 原截止 ${numeric(operation.original_deadline, 3)} · 目的请求 ${numeric(operation.destination_requested_at, 3)}。` : '';
  const transition = experiment?.transition;
  const outcome = {failed: '试降后升温，恢复原安全档', invalidated: '证据中断，取消本次评估'};
  const witness = transition?.witness;
  const source = Array.isArray(witness?.source) ? witness.source.join(' / ') : '';
  const cause = transition ? ` · 时间 ${numeric(transition.at, 3)} · ${transition.cause || ''}${source ? ` · 来源 ${source}` : ''}${Number.isFinite(witness?.actual_c) ? ` · 实温 ${numeric(witness.actual_c)}°C` : ''}` : '';
  const previous = experiment ? ` 历史下降 ${experiment.from}→${experiment.to} · ${outcome[experiment.outcome] || experiment.outcome || experiment.status}${cause}。` : '';
  const operating = decision?.operating ? ` 需求 ${decision.demand} · 下一允许档 ${decision.level} · ${decision.operating.phase} · 已观察 ${numeric(decision.operating.observe_credit)} 秒。` : '';
  const floor = decision?.operating?.failed_floor;
  const blocked = floor ? ` 暂停重试 ${floor.failed_level} 档及以下 · 失败时间 ${numeric(floor.failed_at, 3)}。` : '';
  return operating + current + previous + blocked;
}

function memoryDetails(memory) {
  const labels = {ready:'热记忆已可靠保存', pending:'热记忆等待可靠保存，暂停下降',
    failed:'热记忆保存失败，暂停下降', invalid:'热记忆缺失或损坏，保留故障证据并暂停下降'};
  return ` ${labels[memory.state] || '热记忆状态未知，不能确认下降资格'}。${memory.descent_allowed === true ? '' : '保持和有权限的保护上调仍可执行。'}`;
}

function controlDetails(decision, includeMemory = true) {
  const status = decision?.control_status;
  if (!status || typeof status !== 'object') return '';
  const source = Array.isArray(status.blocking_source) ? status.blocking_source.join(' / ') : status.blocking_source;
  const labels = {hold:'暂停下降', observing:'合格观察', critical:'危急保护', recovering:'恢复安全端点',
    preparing:'负载预备', settled:'稳定负载', stable_loaded:'稳定负载', idle:'空闲', waiting_execution:'等待执行',
    measured_critical:'可信实温达到危急线', attributed_descent_failure:'已归因的试降热失败',
    memory_capacity:'热记忆容量边界', unresolved_descent:'重启前下降尚未核实',
    failed_descent_floor:'失败低档尚未康复', warming_budget_exhausted:'累计温升额度已耗尽',
    stationary_envelope:'已验证的安全往返波动',
    bounded_drift_observation:'回温预测余量充足，暂仅观察',
    historical_expansion_observation:'历史波幅已回落，仍等待稳定证据',
    high_exit_warmup:'高档退出趋势覆盖尚未完成',
    high_exit_observation:'高档退出安全证据取样中', high_exit_ready:'高档退出观察已满足',
    high_exit_margin:'距危急线的原始预测余量不足', high_exit_trend:'高档退出趋势超界',
    high_exit_warming_budget:'高档退出链温升额度已耗尽', high_exit_recent_jump:'新跳温待重新取证',
    high_exit_source:'高档退出来源证据不完整', high_exit_context:'高档退出工况不可比',
    high_exit_endpoint:'高档退出端点未确认', high_exit_power:'功率保护阻止高档退出',
    high_exit_restoration:'高档试降后实测触发精确恢复',
    high_exit_forecast_restoration:'高档试降后预测风险触发预防恢复',
    high_exit_timeout:'高档退出原执行期限已到',
    ceiling_exit_pending:'80°C门槛退出等待本步读回',
    ceiling_exit_ready:'80°C门槛相邻退出已具备条件',
    ceiling_exit_source:'来源不完整或已过期，暂停门槛退出',
    ceiling_exit_endpoint:'等待真实正转速高档端点',
    ceiling_exit_motor_wait:'等待电机降档间隔',
    ceiling_exit_memory:'当前退出意图等待可靠保存',
    ceiling_exit_timeout:'门槛退出原执行截止已到',
    ceiling_safety_conflict:'门槛退出与已证实的保护限制冲突',
    ceiling_exit_execution_timeout:'本步原执行截止已到，等待读回协调',
    ceiling_exit_timeout_reconciliation:'执行超时后等待新的物理核验',
    ceiling_exit_fresh_endpoint:'等待新鲜正转速端点',
    ceiling_exit_endpoint_protection:'设备反馈保护阻止普通退出',
    ceiling_exit_endpoint_reconciliation:'实际端点尚未核实',
    ceiling_exit_endpoint_changed:'实际端点改变，本步重新核实',
    ceiling_exit_identity_changed:'设备身份改变，撤销旧执行归因',
    ceiling_exit_revoked_inflight:'权限已撤销，等待在途动作读回',
    ceiling_exit_source_fault:'温度来源异常，核验独立保护',
    source_fault_protection:'当前温度来源故障保护',
    failed_descent_restoration:'真实降档热失败，恢复精确安全端点',
    ceiling_exit_measured80:'实温达到80°C，重新评估普通增强需求',
    ceiling_exit_source_evidence:'退出所需完整来源证据不足',
    ceiling_exit_memory_conflict:'热记忆证据冲突，暂停普通退出',
    ceiling_exit_failed_descent_restoration:'真实降档失败，核验精确恢复',
    ceiling_exit_complete:'已读回普通门槛退出的目的端点',
    ceiling_exit_current_rapid_risk:'同一来源接近危急线且快速升温，短时暂停普通退出',
    ceiling_exit_hardware_alarm:'硬件告警，暂停退出并核验保护',
    ceiling_exit_unresolved_execution:'先核实未决物理动作',
    ceiling_exit_safety_conflict:'历史失败限制阻止本步退出，等待符合条件的重新核验',
    ceiling_exit_requalifying:'历史失败限制保留，重新核验本步退出',
    ceiling_requalification_collecting:'正在采集本端点完整60秒温度窗口',
    ceiling_requalification_material_recovery:'失败来源的负载或功率尚未满足改善条件',
    ceiling_requalification_context_changed:'工况范围或空闲状态改变，重新取证',
    ceiling_requalification_fresh_evidence:'等待完整新鲜的温度、身份及风机证据',
    ceiling_requalification_advancing_evidence:'等待温度来源和真实风机读回共同推进',
    ceiling_requalification_persistent_heat:'持续升温证据尚未解除，暂停本步退出',
    ceiling_requalification_temperature_window:'温度波幅或趋势未通过完整60秒核验',
    ceiling_requalification_temperature_slope:'完整60秒温度趋势仍超出退出范围',
    ceiling_requalification_temperature_range:'完整60秒温度波幅超过2°C',
    ceiling_requalification_temperature_median:'末段温度中位数较首段上升超过0.3°C',
    ceiling_requalification_ready:'本步重新取证合格，仍须核验电机和持久意图',
    ceiling_exit_motor_budget:'电机预算限制，暂停本步退出',
    ceiling_exit_motor_interval:'等待电机降档间隔',
    ceiling_exit_advancing_endpoint:'等待新的端点反馈',
    growing_return_cycles:'重复波动幅度仍在增加',
    returned_cycle_observation:'处于既有波动范围，尚未满足执行条件',
    returned_cycle_bound:'已回落的周期波动，仍按单步条件评估',
    bounded_deceleration:'小幅回温已减缓且预测余量充足',
    raw_projection_margin:'原始预测余量不足',
    persistent_heat:'持续升温尚未证明风险有界',
    coverage_or_genuine_rise:'覆盖不足或升温风险未解除',
    expanding_return_range:'波动范围扩大，尚未证明回落',
    drift_rate_or_projected_margin:'回温速率或预测余量未满足下降条件',
    new_workload:'新负载周期', pending_intent:'当前下降意图待确认', incomplete_safe_evidence:'安全证据不完整',
    insufficient_margin:'预测余量不足', safe_observation:'安全证据取样中', storage_hold:'存储暂停', protection:'保护状态',
    complete_safe_observation:'完成本档安全观察', fresh_safe_temperature:'取得新鲜安全温度',
    safe_endpoint_readback:'确认原安全端点的真实读回', verify_restrictive_memory:'核实持久限制和未完成下降',
    same_source_ready:'等待约束来源恢复可执行资格；不只等待计时结束',
    lower_same_host_workload_60s:'失败来源负载及功率改善后取证60秒',
    safe_comparable_plateau_180s:'同档可比安全新平衡180秒', comparable_safe_workload_60s:'可比安全负载60秒',
    persisted_snapshot_and_readback:'当前快照可靠保存并确认真实目的档',
    fresh_complete_safe_evidence:'重新取得完整新鲜安全证据', safe_projected_margin:'同源预测余量满足要求',
    high_exit_complete_trend_60s:'先取得本档完整60秒趋势',
    high_exit_complete_observation_180s:'完成本档180秒合格退出观察',
    high_exit_safe_critical_margin:'逐来源距75°C危急线的预测余量至少5°C',
    high_exit_comparable_context:'重新取得同源可比工况',
    high_exit_fresh_endpoint:'取得新鲜正转速端点',
    reliable_storage_and_fresh_evidence:'存储恢复且安全证据新鲜'};
  const label = value => labels[value] || value || '未知';
  return ` 闭环状态 ${label(status.phase)} · ${label(status.primary_reason)}。`
    + (status.release_basis === 'high_exit' ? ' 高档逐级退出，每次仅下降一档，至少到10档后再按普通规则寻找更低档位。' : '')
    + (status.release_basis === 'ceiling80' && !decision?.high_wind?.exception ? ' 低于80°C的普通增强权限已撤销，按真实电机约束逐档退出至10档。此过程不积累普通试降信用。' : '')
    + (status.primary_reason === 'ceiling_exit_requalifying' ? ' 观察时长达到60秒仍须温度趋势、波幅和可比工况全部合格，计时结束不等于获准降档。' : '')
    + (source ? ` 约束来源 ${String(source).slice(0, 240)}。` : '')
    + (Number.isFinite(status.margin_c) ? ` ${status.margin_reference === 'critical75'
      ? '距75°C危急线的预测余量' : '同源预测余量'} ${numeric(status.margin_c)}°C。` : '')
    + (status.release_basis === 'high_exit' && Number.isFinite(status.measured_margin_c)
      ? ` 实温余量 ${numeric(status.measured_margin_c)}°C。` : '')
    + (status.release_basis === 'high_exit' && Number.isFinite(status.trend_rate_c_per_second)
      ? ` 趋势 ${numeric(status.trend_rate_c_per_second, 3)}°C/s。` : '')
    + (status.release_basis === 'high_exit' && Number.isFinite(status.chain_warming_c)
        && Number.isFinite(status.warming_budget_c)
      ? ` 退出链累计温升 ${numeric(status.chain_warming_c)} / ${numeric(status.warming_budget_c)}°C。` : '')
    + (Number.isFinite(status.observe_seconds) && Number.isFinite(status.required_seconds)
      ? ` 合格观察 ${numeric(status.observe_seconds, 0)} / ${numeric(status.required_seconds, 0)} 秒。` : '')
    + (status.next_condition ? ` 下一步条件 ${String(label(status.next_condition)).slice(0, 300)}。` : '')
    + (status.workload_phase ? ` 负载阶段 ${label(status.workload_phase)}。` : '')
    + (status.release_basis === 'high_exit' && status.ordinary_reason
      ? ` 普通低档搜索仍受 ${String(label(status.ordinary_reason)).slice(0, 160)} 约束，不借用退出观察。` : '')
    + (includeMemory && decision.thermal_memory ? memoryDetails(decision.thermal_memory) : '');
}

function renderHistory(data) {
  const samples = data.samples || [];
  $('history-count').textContent = `显示 ${samples.length} 条，按日志写入顺序倒序，最多 200 条。`
    + (data.samples_truncated ? '所选范围还有更早记录，表格已截断；选择更早的结束时间可继续查看。' : '所选范围内未截断。');
  $('history-samples').innerHTML = samples.map(sample => {
    const fan = sample.fan || {};
    const readback = fanReadback(fan, sample.snapshot_at, sample.requested_level);
    const manual = sample.control?.manual_level;
    const mode = manual === null ? '软件自动温控' : Number.isInteger(manual) && manual >= 0 && manual <= 16 ? `手动最低档 ${manual} 档` : '未知（旧记录未记录）';
    const nodes = hosts.map(host => {
      const node = sample.nodes?.find(item => item.host === host);
      if (!node) return '<td>节点记录缺失</td>';
      const age = Number.isFinite(sample.snapshot_at) && Number.isFinite(node.sampled_at) ? sample.snapshot_at - node.sampled_at : null;
      const state = node.online === false ? '离线 / 缺失' : node.online !== true ? '在线状态未知'
        : age === null ? '采样时效未知' : age < 0 ? '采样时间异常' : age > 9 ? '采样已过期' : '记录时在线';
      return `<td>${numeric(node.cpu_temp)} / ${numeric(node.gpu_temp)} / ${numeric(node.board_temp)} °C<small>${state}</small><small>采样 ${escape(historyTime(node.sampled_at))}</small><small>${numeric(node.power_w)} W · ${escape(powerLabel(node))}</small></td>`;
    }).join('');
    const policies = Object.entries(sample.active_power_policy || {}).map(([host, policy]) => `${host} 整机 ${numeric(policy.threshold_w, 0)} W · ${policy.source} · 发布版本 ${policy.version}`).join('；');
    return `<tr><td>${escape(historyTime(sample.ts))}<small>快照 ${escape(historyTime(sample.snapshot_at))}</small></td><td>${mode}</td><td>${numeric(sample.requested_level, 0)}</td><td>${readback.actual !== null ? `${readback.actual} 档` : '实际档位未知'}<small>${escape(historyReadback(readback))}</small><small>${escape(fanSetting(fan))}</small><small>${escape(fan.error || '')}</small><small>${escape(ambientReadback(fan, sample.snapshot_at))}</small></td><td>${numeric(fan.rpm, 0)} RPM</td><td>${escape(historyTime(fan.verified_at))}</td>${nodes}<td>${(sample.decision?.reasons || []).map(escape).join('<br>') || '未记录原因'}<small>当前场景：${escape(sample.decision?.scenario_detail || sample.decision?.scenario || '旧记录未标注场景')}</small><small>${escape(highWindDetails(sample.decision))}</small><small>${escape(controlDetails(sample.decision))}</small>${trialDetails(sample.decision) ? `<small>${escape(trialDetails(sample.decision))}</small>` : ''}<small>${escape(policies || '固定保护；未记录生效学习参数')}</small></td></tr>`;
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
