/* 年线轮动策略看板 —— 浏览器端重算引擎
 * 本文件与 scripts/strategy.py 必须逐行等价：页面显示与 Bark 推送同源同算。
 * 配色约定：红 = 盈利/上涨，绿 = 亏损/下跌（A股习惯）。
 */
'use strict';

const MA_N = 250, VOL_N = 20, VOL_K = 1.3, TAKE_PROFIT = 0.80;
const TIERS = [0.96, 0.93, 0.90], COST = 0.0002;
const CASH_ANNUAL = 0.015, TRADING_DAYS = 243;
const LS = 'yearline.v1';

/* ---------------------------------------------------------------- 工具 */
const $ = id => document.getElementById(id);
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const pct = (x, d = 2) =>
  (Math.abs(x) < 5e-5 ? '' : x > 0 ? '+' : '−') + Math.abs(x * 100).toFixed(d) + '%';
const money = x => x.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
const yi = x => (x / 1e4).toFixed(0) + ' 亿';          // cyb_amt 单位是万元
const sign = x => (x > 1e-9 ? 'up' : x < -1e-9 ? 'down' : 'flat');

function sma(xs, n, i) {
  if (i < n - 1) return null;
  let s = 0;
  for (let k = i - n + 1; k <= i; k++) { if (xs[k] == null) return null; s += xs[k]; }
  return s / n;
}

/* ------------------------------------------------------------ 指标预计算 */
function indicators(d) {
  const n = d.dates.length, ma = [], mah = [], av = [];
  for (let i = 0; i < n; i++) {
    ma.push(sma(d.cyb_sig, MA_N, i));
    mah.push(sma(d.hl_sig, MA_N, i));
    if (i >= VOL_N) {
      let s = 0, ok = true;
      for (let k = i - VOL_N; k < i; k++) { if (d.cyb_amt[k] == null) { ok = false; break; } s += d.cyb_amt[k]; }
      av.push(ok ? s / VOL_N : null);
    } else av.push(null);
  }
  return { ma, mah, av };
}

const tiersAt = (d, ind, i) => {
  if (ind.mah[i] == null || d.hl_sig[i] == null) return 0;
  const r = d.hl_sig[i] / ind.mah[i];
  return TIERS.reduce((c, t) => c + (r <= t ? 1 : 0), 0);
};

function earliestStart(d, ind) {
  for (let i = 0; i < d.dates.length; i++)
    if (ind.ma[i] && ind.av[i] && ind.mah[i] && d.cyb50[i]) return d.dates[i];
  return d.dates[0];
}

/* ------------------------------------------------------------------ 重放 */
function run(d, ind, startDate, capital) {
  const { ma, mah, av } = ind, n = d.dates.length;
  const usable = i => ma[i] && av[i] && mah[i] && d.cyb50[i];

  let s = -1;
  for (let i = 0; i < n; i++) if (d.dates[i] >= startDate && usable(i)) { s = i; break; }
  if (s < 0) for (let i = n - 1; i >= 0; i--) if (usable(i)) { s = i; break; }
  if (s < 0) throw new Error('数据不足，无法计算任何交易日');

  const ret = (a, i) => (a[i] && a[i - 1]) ? a[i] / a[i - 1] - 1 : 0;
  const rCash = Math.pow(1 + CASH_ANNUAL, 1 / TRADING_DAYS) - 1;

  let vCyb = 0, vHl = 0, vCash = capital;
  let state = 'HL', pend = null, entryPx = null, took = false, tier = 0;
  const curve = [], events = [];

  const rebalance = () => {
    const pool = vHl + vCash, tgt = pool * (tier / 3);
    if (vHl < tgt) { const x = Math.min(tgt - vHl, vCash); vCash -= x; vHl += x * (1 - COST); }
    else if (vHl > tgt) { const x = vHl - tgt; vHl -= x; vCash += x * (1 - COST); }
  };

  for (let i = s; i < n; i++) {
    vCyb *= 1 + ret(d.cyb50, i);
    vHl *= 1 + ret(d.hl_tr, i);
    vCash *= 1 + rCash;

    if (pend && i >= pend[1]) {
      if (pend[0] === 'CYB') {
        vCyb = (vHl * (1 - COST) + vCash) * (1 - COST); vHl = 0; vCash = 0;
        entryPx = d.cyb_sig[i]; took = false; tier = 0; state = 'CYB';
        events.push({ date: d.dates[i], action: '进场', detail: '全仓买入创业板50' });
      } else if (pend[0] === 'HL') {
        vCash += vCyb * (1 - COST); vCyb = 0; state = 'HL';
        tier = Math.max(tier, tiersAt(d, ind, i)); rebalance();
        events.push({ date: d.dates[i], action: '出场',
          detail: '清仓创业板50' + (tier ? `，同时补足红利 ${tier}/3 仓` : '，转入现金') });
      } else {
        const half = vCyb / 2; vCyb -= half; vCash += half * (1 - COST); took = true;
        tier = Math.max(tier, tiersAt(d, ind, i)); rebalance();
        events.push({ date: d.dates[i], action: '止盈', detail: '卖出一半创业板50（段内涨幅达 80%）' });
      }
      pend = null;
    }

    // 不限定 state === 'HL'：止盈后进入防守腿的那一半须立刻参与网格（与回测一致）
    if (vHl + vCash > 1e-9 && !pend) {
      const want = (tier > 0 && d.hl_sig[i] >= mah[i]) ? 0 : Math.max(tier, tiersAt(d, ind, i));
      if (want !== tier) {
        const old = tier; tier = want; rebalance();
        events.push({ date: d.dates[i], action: want === 0 ? '红利止盈' : '红利加仓',
          detail: want === 0 ? '红利涨回 MA250，三档全部清空'
                             : `红利买入至 ${want}/3 仓（原 ${old}/3）` });
      }
    }

    const eq = vCyb + vHl + vCash;
    curve.push({ date: d.dates[i], equity: eq, state, tier,
                 w_cyb: vCyb / eq, w_hl: vHl / eq, w_cash: vCash / eq });

    if (!pend) {
      const nxt = Math.min(i + 1, n - 1);
      if (state === 'HL') {
        if (d.cyb_sig[i] > ma[i] && av[i] && d.cyb_amt[i] >= VOL_K * av[i]) pend = ['CYB', nxt, i];
      } else if (d.cyb_sig[i] < ma[i]) pend = ['HL', nxt, i];
      else if (!took && entryPx && d.cyb_sig[i] / entryPx - 1 >= TAKE_PROFIT) pend = ['HALF', nxt, i];
    }
  }

  let peak = -Infinity, mdd = 0;
  for (const p of curve) { peak = Math.max(peak, p.equity); mdd = Math.min(mdd, p.equity / peak - 1); }
  const years = curve.length / TRADING_DAYS;
  const last = curve[curve.length - 1];

  return {
    curve, events, start: d.dates[s], days: curve.length, years, capital,
    equity: last.equity, totalReturn: last.equity / capital - 1,
    cagr: years > 0.08 ? Math.pow(last.equity / capital, 1 / years) - 1 : null,
    maxDrawdown: mdd, state: last.state, tier: last.tier,
    weights: { cyb: last.w_cyb, hl: last.w_hl, cash: last.w_cash },
    // execDate 为 null 表示信号刚发生在最后一个已知交易日，执行日尚未产生
    pending: pend ? { action: pend[0], execDate: pend[1] > pend[2] ? d.dates[pend[1]] : null } : null,
    entryPx, took
  };
}

/* -------------------------------------------------------------- 下一步 */
function nextTriggers(d, ind, res) {
  const i = d.dates.length - 1, { ma, mah, av } = ind;
  const cyb = d.cyb_sig[i], hl = d.hl_sig[i], amt = d.cyb_amt[i];
  const out = [];

  if (res.state === 'CYB') {
    const buf = cyb / ma[i] - 1;
    out.push({ label: '创业板出场', action: `清仓${NAME.cyb}，转入${NAME.hl}腿`,
      cond: `收盘跌破 MA250 ${ma[i].toFixed(2)}`,
      headline: `距出场线尚有 <b>${(buf * 100).toFixed(2)}%</b> 缓冲`,
      short: `尚有 ${(buf * 100).toFixed(2)}% 缓冲`,
      progress: 1 - clamp(buf / 0.20, 0, 1),
      marks: [`出场线 ${ma[i].toFixed(2)}`, `现价 ${cyb.toFixed(2)}`, ''] });

    if (!res.took && res.entryPx) {
      const tgt = res.entryPx * (1 + TAKE_PROFIT), g = cyb / res.entryPx - 1;
      out.push({ label: '创业板止盈一半', action: `卖出一半${NAME.cyb}`,
        cond: `本段涨幅达 +80%，即指数涨到 ${tgt.toFixed(2)}`,
        headline: `本段已涨 <b>${(g * 100).toFixed(1)}%</b> ／ 需 +80%`,
        short: `本段已涨 ${(g * 100).toFixed(1)}%`,
        progress: clamp(g / TAKE_PROFIT, 0, 1),
        marks: [`进场 ${res.entryPx.toFixed(2)}`, `现价 ${cyb.toFixed(2)}`, tgt.toFixed(2)] });
    }
  } else {
    // 进场需同时满足「站上均线」和「放量」，取更难的那个作为距离
    const ratio = av[i] ? amt / av[i] : null;
    const priceProg = clamp(cyb / ma[i], 0, 1);
    const volProg = ratio ? clamp(ratio / VOL_K, 0, 1) : 0;
    const volBinding = volProg <= priceProg;
    out.push({ label: '创业板进场', action: `全仓买入${NAME.cyb}`,
      cond: cyb > ma[i]
        ? `已站上 MA250 ${ma[i].toFixed(2)}，只差放量`
        : `需站上 MA250 ${ma[i].toFixed(2)}，且成交额达 1.30×`,
      headline: volBinding
        ? `成交额 <b>${(ratio || 0).toFixed(2)}×</b> ／ 需 1.30×`
        : `再涨 <b>${((ma[i] / cyb - 1) * 100).toFixed(2)}%</b> 站上均线`,
      short: volBinding ? `量能 ${(ratio || 0).toFixed(2)}×／1.30×`
                        : `再涨 ${((ma[i] / cyb - 1) * 100).toFixed(2)}%`,
      progress: Math.min(priceProg, volProg),
      marks: [`当前 ${yi(amt)}`, '', av[i] ? `需 ${yi(VOL_K * av[i])}` : ''] });

    if (res.tier < 3 && mah[i]) {
      const t = TIERS[res.tier], price = mah[i] * t, need = price / hl - 1;
      out.push({ label: `红利第 ${res.tier + 1} 档买入`, action: `买入 1/3 仓${NAME.hl}`,
        cond: `跌破 ${price.toFixed(2)}（MA250 −${Math.round((1 - t) * 100)}%）`,
        headline: `再跌 <b>${Math.abs(need * 100).toFixed(2)}%</b>`,
        short: `再跌 ${Math.abs(need * 100).toFixed(2)}%`,
        progress: clamp((mah[i] - hl) / (mah[i] - price), 0, 1),
        marks: [`MA250 ${mah[i].toFixed(2)}`, `现价 ${hl.toFixed(2)}`, price.toFixed(2)] });
    }
    if (res.tier > 0 && mah[i]) {
      const need = mah[i] / hl - 1;
      out.push({ label: '红利止盈', action: `清空全部${NAME.hl}仓位，回到现金`,
        cond: `涨回 MA250 ${mah[i].toFixed(2)}`,
        headline: `再涨 <b>${(need * 100).toFixed(2)}%</b>`,
        short: `再涨 ${(need * 100).toFixed(2)}%`,
        progress: clamp(1 - need / 0.10, 0, 1),
        marks: [`现价 ${hl.toFixed(2)}`, '', mah[i].toFixed(2)] });
    }
  }

  // 按接近程度排序，最近的排第一 —— 页面和推送都以此为准
  out.sort((a, b) => b.progress - a.progress);
  return out;
}

/* ------------------------------------------------------------------ 渲染 */
const NAME = { cyb: '创业板50', hl: '红利' };
const ACT_TITLE = { CYB: '全仓买入创业板50', HL: '清仓创业板50，转入红利腿', HALF: '卖出一半创业板50' };

function positionText(res) {
  const w = res.weights;
  if (res.state === 'CYB') {
    const p = [`${NAME.cyb} ${Math.round(w.cyb * 100)}%`];
    if (w.hl > 0.005) p.push(`${NAME.hl} ${Math.round(w.hl * 100)}%`);
    if (w.cash > 0.005) p.push(`现金 ${Math.round(w.cash * 100)}%`);
    return p.join(' ／ ');
  }
  if (res.tier > 0) return `${NAME.hl} ${res.tier}/3 仓（${Math.round(w.hl * 100)}%）／ 现金 ${Math.round(w.cash * 100)}%`;
  return '空仓（现金 100%）';
}

function renderAlert(d, res) {
  const box = $('alert'), lastDate = d.dates[d.dates.length - 1];
  const todays = res.events.filter(e => e.date === lastDate);
  let key = null, title = '', body = '', pillText = '需要操作';

  if (res.pending) {
    key = `ack:${lastDate}:pend:${res.pending.action}`;
    title = ACT_TITLE[res.pending.action];
    body = `触发于 ${lastDate} 收盘，应于 <b>${res.pending.execDate || '下一个交易日'}</b> 收盘执行。`;
  } else if (todays.length) {
    key = `ack:${lastDate}:done:${todays.map(e => e.action).join('+')}`;
    pillText = '今日已执行';
    title = todays.map(e => e.action).join('／');
    body = todays.map(e => '· ' + e.detail).join('<br />');
  }

  if (!key || localStorage.getItem(key) === '1') { box.hidden = true; return; }
  $('alert-pill').textContent = pillText;
  $('alert-title').textContent = title;
  $('alert-body').innerHTML = body;
  const btn = $('alert-ack');
  btn.onclick = () => { localStorage.setItem(key, '1'); box.hidden = true; };
  box.hidden = false;
}

function renderTracks(list) {
  if (!list.length) { $('tracks').innerHTML = ''; return; }
  const [lead, ...rest] = list;
  const p = clamp(lead.progress ?? 0, 0, 1);
  const hot = p >= 0.85;
  const marks = (lead.marks || ['', '', '']).map(m => `<span>${m}</span>`).join('');

  let html = `<div class="lead${hot ? ' hot' : ''}">
    <div class="eyebrow">${hot ? '即将触发' : '最接近触发'}</div>
    <div class="act">${lead.action}</div>
    <div class="gap">${lead.headline}</div>
    <div class="rail${hot ? ' near' : ''}"><div class="fill" style="width:${(p * 100).toFixed(1)}%"></div></div>
    <div class="mark">${marks}</div>
    <div class="cond">触发条件：${lead.cond}</div>
  </div>`;

  if (rest.length) {
    html += `<div class="rest"><div class="hd">其它触发点</div>` + rest.map(t => {
      const q = clamp(t.progress ?? 0, 0, 1);
      return `<div class="item">
        <span class="n">${t.label} → ${t.action}</span>
        <span class="minirail"><i style="width:${(q * 100).toFixed(1)}%"></i></span>
        <span class="g">${t.short}</span>
      </div>`;
    }).join('') + `</div>`;
  }
  $('tracks').innerHTML = html;
}

function renderStats(res) {
  const items = [
    { k: '当前净值', v: '¥' + money(res.equity), c: sign(res.totalReturn) },
    { k: '总收益', v: pct(res.totalReturn), c: sign(res.totalReturn) },
    { k: '年化', v: res.cagr == null ? '—' : pct(res.cagr), c: res.cagr == null ? 'flat' : sign(res.cagr) },
    { k: '最大回撤', v: pct(res.maxDrawdown), c: res.maxDrawdown < -1e-9 ? 'down' : 'flat' }
  ];
  $('stats').innerHTML = items.map(i =>
    `<div class="stat"><div class="k">${i.k}</div><div class="v ${i.c}">${i.v}</div></div>`).join('');
  $('range-note').textContent = `${res.start} 起 · ${res.days} 个交易日 · 本金 ¥${money(res.capital)}`;
}

function renderHoldings(d, ind, res) {
  const i = d.dates.length - 1, w = res.weights, rows = [];
  rows.push(`<div class="row"><span class="l"><b>${NAME.cyb}</b></span>
    <span class="${w.cyb > 0.005 ? 'chip on' : 'chip'}">${(w.cyb * 100).toFixed(0)}%</span></div>`);
  rows.push(`<div class="row"><span class="l"><b>${NAME.hl}</b></span>
    <span class="${w.hl > 0.005 ? 'chip on' : 'chip'}">${(w.hl * 100).toFixed(0)}%</span></div>`);
  rows.push(`<div class="row"><span class="l"><b>现金</b>（货基／逆回购）</span>
    <span class="${w.cash > 0.005 ? 'chip on' : 'chip'}">${(w.cash * 100).toFixed(0)}%</span></div>`);
  if (ind.mah[i]) {
    TIERS.forEach((t, k) => {
      const hit = k < res.tier;
      rows.push(`<div class="row"><span class="l">红利第 ${k + 1} 档 −${Math.round((1 - t) * 100)}% ·
        ${(ind.mah[i] * t).toFixed(2)}</span>
        <span class="chip${hit ? ' on' : ''}">${hit ? '已持有' : '未触发'}</span></div>`);
    });
  }
  rows.push(`<div class="row"><span class="l">创业板 MA250（出场线）</span>
    <span>${ind.ma[i].toFixed(2)}</span></div>`);
  $('holdings').innerHTML = rows.join('');
}

function renderEvents(res) {
  const ev = res.events.slice(-8).reverse();
  $('events').innerHTML = ev.length
    ? ev.map(e => `<div class="row"><span class="l">${e.date} · <b>${e.action}</b></span>
        <span>${e.detail}</span></div>`).join('')
    : '<div class="empty">启动以来还没有触发过任何操作。</div>';
}

/* ------------------------------------------------------------------ 设置 */
function loadPrefs() { try { return JSON.parse(localStorage.getItem(LS)) || {}; } catch { return {}; } }
function savePrefs(p) { localStorage.setItem(LS, JSON.stringify(p)); }

/* -------------------------------------------------------------------- 主 */
(async function main() {
  let cfg, daily;
  try {
    const bust = '?t=' + Date.now();
    [cfg, daily] = await Promise.all([
      fetch('config.json' + bust).then(r => r.json()),
      fetch('data/daily.json' + bust).then(r => r.json())
    ]);
  } catch (e) {
    $('boot').textContent = '数据载入失败：' + e.message;
    return;
  }
  if (cfg.names) Object.assign(NAME, cfg.names);
  ACT_TITLE.CYB = `全仓买入${NAME.cyb}`;
  ACT_TITLE.HL = `清仓${NAME.cyb}，转入${NAME.hl}腿`;
  ACT_TITLE.HALF = `卖出一半${NAME.cyb}`;

  const ind = indicators(daily);
  const minStart = earliestStart(daily, ind);
  const maxStart = daily.dates[daily.dates.length - 1];
  const defaults = {
    capital: Number(cfg.initial_capital) || 100000,
    start: cfg.start_date || minStart
  };

  function draw() {
    const p = loadPrefs();
    const capital = Number(p.capital) || defaults.capital;
    let start = p.start || defaults.start;
    if (start < minStart) start = minStart;
    let res;
    try { res = run(daily, ind, start, capital); }
    catch (e) { $('boot').hidden = false; $('boot').textContent = '计算失败：' + e.message; return; }

    renderAlert(daily, res);
    renderTracks(nextTriggers(daily, ind, res));
    renderStats(res);
    renderHoldings(daily, ind, res);
    renderEvents(res);
    $('pos-chip').textContent = positionText(res);

    const stale = (Date.now() - new Date(daily.updated + 'T16:00:00+08:00').getTime()) / 86400000;
    const foot = document.querySelector('.foot');
    foot.classList.toggle('stale', stale > 10);
    $('data-note').textContent = `数据截至 ${daily.updated}` + (stale > 10 ? '（已停滞，请检查抓取任务）' : '');

    $('cfg-capital').value = capital;
    $('cfg-start').value = start;
    $('cfg-start').min = minStart;
    $('cfg-start').max = maxStart;
    $('boot').hidden = true;
    $('app').hidden = false;
  }

  $('cfg-toggle').onclick = () => {
    const box = $('cfg'), open = box.hidden;
    box.hidden = !open;
    $('cfg-toggle').setAttribute('aria-expanded', String(open));
  };
  $('cfg-apply').onclick = () => {
    const cap = Number($('cfg-capital').value), st = $('cfg-start').value;
    const hint = $('cfg-hint');
    if (!(cap > 0)) { hint.textContent = '初始资金需大于 0'; hint.className = 'hint err'; return; }
    if (st < minStart) { hint.textContent = `启动时间最早只能到 ${minStart}（此前数据不足以计算信号）`; hint.className = 'hint err'; return; }
    if (st > maxStart) { hint.textContent = `启动时间不能晚于最新数据日 ${maxStart}`; hint.className = 'hint err'; return; }
    savePrefs({ capital: cap, start: st });
    hint.textContent = '已重新计算。'; hint.className = 'hint';
    draw();
  };
  $('cfg-reset').onclick = () => {
    localStorage.removeItem(LS);
    $('cfg-hint').textContent = '已恢复为 config.json 的设定。';
    $('cfg-hint').className = 'hint';
    draw();
  };

  draw();
})();
