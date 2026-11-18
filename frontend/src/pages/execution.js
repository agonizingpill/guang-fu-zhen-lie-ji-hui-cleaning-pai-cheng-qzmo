import {field, sync} from '../store.js';
import {request, identity} from '../api.js';
import {lead} from '../components/field.js';
import {safe, fixed, dataFrom, message, localTime} from '../utils.js';
import {EXECUTION_STATES, PERMIT_STATES, PERMIT_KINDS} from '../constants.js';

const predCache = {};
const crewCode = id => field.crews.find(c => c.id === id)?.code || '未知班组';
const blockOf = m => {
  const s = field.surveys.find(x => x.id === m.survey_id);
  return field.arrays.find(a => a.id === s?.block_id);
};
const missionSegments = id => field.segments.filter(s => s.mission_id === id);
const sum = (rows, key) => rows.reduce((acc, r) => acc + Number(r[key] || 0), 0);
const permitBadge = p => `<span class="badge ${safe(p.state)}">${safe(PERMIT_STATES[p.state] || p.state)}</span>`;
const today = () => new Date().toISOString().slice(0, 10);
const tomorrow = () => new Date(Date.now() + 86400000).toISOString().slice(0, 10);

function crewCapacityLine(crew) {
  const day = today();
  const reserved = field.permits
    .filter(p => p.crew_id === crew.id && p.work_date === day && ['signed', 'used'].includes(p.state))
    .reduce((a, p) => a + p.minutes, 0);
  const actual = field.segments
    .filter(s => s.crew_id === crew.id && s.work_date === day)
    .reduce((a, s) => a + s.actual_minutes, 0);
  const over = actual > crew.max_daily_minutes;
  return `<p class="${over ? 'rule' : 'note'}">${safe(crew.code)} 今日预留 ${reserved} / 实耗 ${actual} / 容量 ${crew.max_daily_minutes} 分钟${over ? ' · 实耗超额，已阻止新签' : ''}</p>`;
}

function predictionCard(mid) {
  const cacheKey = `${mid}:${missionSegments(mid).length}`;
  const ev = predCache[cacheKey];
  if (!ev) return '<p class="note">工时预测加载中…</p>';
  if (ev.manual_review) {
    return `<div class="rule"><b>转人工核定</b><br>${safe(ev.reason || '样本不足')}<br>已耗 ${ev.consumed_minutes} 分钟 · 已耗水 ${fixed(ev.consumed_water_liters, 1)} L</div>`;
  }
  return `<div class="evidence"><b>剩余工时预测（随机森林）</b><br>` +
    `预测总工时 ${ev.predicted_total_minutes} 分钟 · 剩余 ${ev.predicted_remaining_minutes} 分钟 · 已耗 ${ev.consumed_minutes} 分钟<br>` +
    `独立留出 ${ev.holdout_rows} 条，留出 MAE ${fixed(ev.holdout_mae_minutes, 1)} 分钟（${fixed(ev.holdout_mae_pct, 2)}%）；训练 MAE ${fixed(ev.train_mae_minutes, 1)} 分钟，训练 ${ev.train_rows} 条<br>` +
    `已完工样本 ${ev.completed_rows} 条 · 特征：${safe(ev.features.join(' / '))}</div>`;
}

export function render() {
  const role = identity().role;
  const edit = role !== 'viewer';
  const admin = role === 'admin';
  const executable = field.missions.filter(m => m.status === 'dispatched');
  const m = executable.find(x => x.id === field.selectedMission) || executable[0];
  const pending = field.permits.filter(p => p.state === 'pending');
  return lead('MULTI-DAY EXECUTION', '山地清洗跨日分段，工时实耗按班次认账',
    '登记分段实耗训练剩余工时模型；暂停保留已耗水与工时并释放未耗，次日换班续作；容量不足保留待接单且不扣水，迟报实耗归原班次并重算，超额阻止新签。') +
    `<div class="twocol expansion-layout"><section class="surface">
      <h2>执行中的清洗任务</h2>
      ${executable.length ? executable.map(item => {
        const a = blockOf(item);
        return `<button data-mission="${item.id}" class="${item.id === m?.id ? 'selected' : ''}" style="width:100%;text-align:left;margin-bottom:8px">
          <span class="code">WASH-${String(item.id).padStart(3, '0')} · ${safe(a?.code || '')}</span>
          <h3>${safe(a?.name || '')} <span class="badge ${item.execution_state}">${safe(EXECUTION_STATES[item.execution_state] || item.execution_state)}</span></h3>
          计划 ${fixed(item.water_liters, 0)} L
        </button>`;
      }).join('') : '<div class="empty">没有执行中的任务<br>先在派工页签发许可并派工。</div>'}
      <h3>班组今日工时口径</h3>${field.crews.filter(c => c.active).map(crewCapacityLine).join('')}
      ${m ? `<div class="sectionline"></div><h2>分段实耗登记</h2>
        <form id="segment-form">
          <label>认账许可<select name="permit_id">${field.permits.filter(p => p.mission_id === m.id && ['used', 'paused'].includes(p.state)).map(p => `<option value="${p.id}">PERMIT-${p.id} ${safe(PERMIT_KINDS[p.kind] || p.kind)} · ${safe(crewCode(p.crew_id))} · ${permitBadge(p)}</option>`).join('')}</select></label>
          <label>分段单号<input name="segment_code" placeholder="SEG-1118-01" required></label>
          <label>作业日期<input name="work_date" type="date" value="${today()}" required></label>
          <label>实耗分钟<input name="actual_minutes" type="number" min="1" max="1440" value="90" required></label>
          <label>实耗水量 / L<input name="water_used_liters" type="number" min="0" step="0.1" value="120" required></label>
          <label>现场说明<input name="note" placeholder="天气、坡度、人员变化（选填）"></label>
          <button class="primary" ${edit ? '' : 'disabled'}>登记分段（迟报仍归原班次）</button>
        </form>
        <div class="rule">同一许可每日只认一段，累计用水不得超过任务计划；登记后按原班组原日期重算容量，超额自动阻止该班组同日新签。</div>
        <form id="pause-form" class="inline-form">
          <label>暂停原因<input name="reason" placeholder="收工跨日 / 阵风超限 / 水车待补" required ${m.execution_state === 'paused' ? 'disabled' : ''}></label>
          <button class="warm" ${edit && m.execution_state === 'active' ? '' : 'disabled'}>暂停并保留已耗</button>
        </form>` : ''}
      ${m && m.execution_state === 'paused' ? `<div class="sectionline"></div><h2>次日续作申请（可换班组）</h2>
        <form id="continuation-form">
          <label>承接班组<select name="crew_id">${field.crews.filter(c => c.active).map(c => `<option value="${c.id}">${safe(c.code)} · ${c.max_daily_minutes}分钟/天</option>`).join('')}</select></label>
          <label>续作日期<input name="work_date" type="date" value="${tomorrow()}" required></label>
          <label>现场阵风 m/s<input name="wind_mps" type="number" min="0" max="60" step="0.1" value="4" required></label>
          <label class="checkline"><input name="isolation" type="checkbox" required>续作前已完成电气隔离</label>
          <button class="primary" ${edit ? '' : 'disabled'}>申请续作（容量不足转待接单，不扣水）</button>
        </form>` : ''}
    </section><section>
      <h2>待接单与续作审批</h2>
      ${pending.length ? pending.map(p => {
        const ev = p.prediction_evidence || {};
        return `<article class="surface"><span class="code">PERMIT-${p.id} / WASH-${p.mission_id} ${permitBadge(p)}</span>
          <h3>${safe(crewCode(p.crew_id))} 承接 ${p.minutes} 分钟 · ${p.work_date}</h3>
          <p class="note">阵风 ${p.wind_mps} m/s · 隔离${p.isolation ? '已完成' : '未完成'}${ev.manual_review ? ' · <b>样本不足，人工核定余量</b>' : ` · 模型预测剩余 ${ev.predicted_remaining_minutes} 分钟（留出MAE ${fixed(ev.holdout_mae_minutes, 1)} 分钟）`}</p>
          <div class="actionbar">
            <button class="primary" data-approve="${p.id}" ${admin && p.isolation && p.wind_mps <= 10 ? '' : 'disabled'}>主管承接审批（占用容量）</button>
            <button data-reject="${p.id}" ${admin ? '' : 'disabled'}>驳回（保持待接单）</button>
          </div>
          <p class="note">待接单不扣水、不占工时；审批幂等，重试不会重复占额。</p></article>`;
      }).join('') : '<div class="empty">暂无待接单续作申请</div>'}
      <h2>已签发续作恢复作业</h2>
      ${field.permits.filter(p => p.kind === 'continuation' && ['signed', 'used'].includes(p.state)).map(p => `<article class="surface">
        <span class="code">PERMIT-${p.id} / WASH-${p.mission_id} ${permitBadge(p)}</span>
        <p>${safe(crewCode(p.crew_id))} · ${p.work_date} · ${p.minutes} 分钟 · 签发 ${safe(p.signed_by || '—')}</p>
        <button class="warm" data-resume="${p.id}" ${edit && p.state === 'signed' ? '' : 'disabled'}>续作派工恢复（不重复扣水）</button>
      </article>`).join('') || '<div class="empty">尚无已承接续作</div>'}
      ${m ? `<h2>WASH-${String(m.id).padStart(3, '0')} 分段台账与预测</h2>${predictionCard(m.id)}
        ${(() => { const rows = missionSegments(m.id); const mins = sum(rows, 'actual_minutes'); const water = sum(rows, 'water_used_liters');
          return `<div class="cols two"><div><span class="note">累计实耗工时</span><div class="reading">${mins}<small> 分钟</small></div></div>
          <div><span class="note">累计实耗水量 / 计划</span><div class="reading">${fixed(water, 1)}<small> / ${fixed(m.water_liters, 0)} L</small></div></div></div>`;
        })()}
        ${missionSegments(m.id).map(s => `<article class="surface"><span class="code">${safe(s.segment_code)} · ${s.work_date}</span>
          <h3>${safe(crewCode(s.crew_id))} ${permitBadge({state: field.permits.find(p => p.id === s.permit_id)?.state || 'used'})}</h3>
          <p>${s.actual_minutes} 分钟 · ${fixed(s.water_used_liters, 1)} L<br><span class="note">${safe(s.note || '无备注')} · 登记 ${safe(s.reporter)} · ${localTime(s.created_at)}</span></p></article>`).join('') || '<div class="empty">尚未登记分段实耗</div>'}` : ''}
    </section></div>`;
}

export function bind(rerender) {
  const role = identity().role;
  const edit = role !== 'viewer';
  const m = field.missions.find(x => x.id === field.selectedMission)
    || field.missions.find(x => x.status === 'dispatched');
  async function act(path, body, method = 'POST', okText = '现场执行已更新') {
    try {
      const out = await request(path, method, body);
      await sync();
      if (out?.warning) message(out.warning); else message(okText);
      rerender();
      return out;
    } catch (e) { message(e.message); }
  }
  document.querySelectorAll('[data-mission]').forEach(b => b.onclick = async () => {
    field.selectedMission = Number(b.dataset.mission);
    rerender();
    try {
      const mid = field.selectedMission;
      predCache[`${mid}:${missionSegments(mid).length}`] =
        await request(`/missions/${mid}/duration-prediction`);
      rerender();
    } catch (e) { message(e.message); }
  });
  if (m) {
    const cacheKey = `${m.id}:${missionSegments(m.id).length}`;
    if (predCache[cacheKey] === undefined) {
      request(`/missions/${m.id}/duration-prediction`)
        .then(ev => { predCache[cacheKey] = ev; rerender(); })
        .catch(() => {});
    }
  }
  const segForm = document.querySelector('#segment-form');
  if (segForm) segForm.onsubmit = e => {
    e.preventDefault();
    const b = dataFrom(e.target);
    b.permit_id = Number(b.permit_id);
    b.actual_minutes = Number(b.actual_minutes);
    b.water_used_liters = Number(b.water_used_liters);
    act('/segments', b, 'POST', '分段实耗已登记，容量已重算');
  };
  const pauseForm = document.querySelector('#pause-form');
  if (pauseForm) pauseForm.onsubmit = e => {
    e.preventDefault();
    if (!m) return;
    act(`/missions/${m.id}/pause`, dataFrom(e.target), 'POST', '已暂停：已耗保留，未耗工时释放');
  };
  const contForm = document.querySelector('#continuation-form');
  if (contForm) contForm.onsubmit = e => {
    e.preventDefault();
    const b = dataFrom(e.target);
    b.crew_id = Number(b.crew_id);
    b.wind_mps = Number(b.wind_mps);
    b.isolation = e.target.elements.isolation.checked;
    act('/continuations', b, 'POST', '续作申请已提交').then(out => {
      if (out && out.capacity_available === false) message('班组容量不足，已保留待接单且不扣水');
    });
  };
  document.querySelectorAll('[data-approve]').forEach(b => b.onclick = () =>
    act(`/continuations/${b.dataset.approve}/approve`, undefined, 'POST', '已承接，容量已占用'));
  document.querySelectorAll('[data-reject]').forEach(b => b.onclick = () =>
    act(`/continuations/${b.dataset.reject}/reject`, undefined, 'POST', '已驳回，未扣水未占额'));
  document.querySelectorAll('[data-resume]').forEach(b => b.onclick = () =>
    act(`/continuations/${b.dataset.resume}/resume`, undefined, 'POST', '续作已恢复，沿用已扣水量'));
}
