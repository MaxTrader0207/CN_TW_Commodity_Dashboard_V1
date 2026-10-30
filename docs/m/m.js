// 陸期 × 台股 多空儀表板 — 手機版
// 設計：底部分頁、只在打開分頁時才繪製內容、詳細頁全螢幕（手機返回鍵可關閉）。資料與電腦版共用 ../data/
'use strict';
const $ = s => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const cls = v => v == null ? 'flat' : v > 0 ? 'up' : v < 0 ? 'dn' : 'flat';
const pct = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + '%';
const sc = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(1);
const sg = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + 'σ';
const pp = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + '%';
const wr = v => v == null ? '—' : Math.round(v * 100) + '%';
const lots = v => v == null ? '—' : (v > 0 ? '+' : '') + Math.round(v).toLocaleString();
const fmtTime = t => t ? t.replace('T', ' ').slice(5, 16) : '—';
const CFG = window.DASH_CONFIG || {};
const W_URL = (CFG.triggerUrl || '').replace(/\/$/, '');
const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
                set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} } };
const getJSON = async p => { const r = await fetch(p + '?t=' + Date.now()); if (!r.ok) throw new Error(r.status); return r.json(); };

let D = null, BOARD = {}, CHIPS = {}, CAL = null, TRACK = null, HEAT = null, KL = null;
const drawn = {};   // 已繪製的分頁

function toast(msg) {
  const t = el('div', 'toast', msg); document.body.append(t);
  setTimeout(() => t.remove(), 2600);
}

// ── 主題 ──
const THEMES = [['', '自動'], ['light', '亮色'], ['dark', '暗色']];
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  store.set('theme', t || null);
  $('#theme').textContent = (THEMES.find(x => x[0] === (t || '')) || THEMES[0])[1];
  document.querySelectorAll('#theme-seg button').forEach(b => b.classList.toggle('on', b.dataset.t === (t || '')));
}
$('#theme').onclick = () => {
  const i = THEMES.findIndex(x => x[0] === (document.documentElement.dataset.theme || ''));
  applyTheme(THEMES[(i + 1) % 3][0]);
};
document.querySelectorAll('#theme-seg button').forEach(b => b.onclick = () => applyTheme(b.dataset.t));
applyTheme(document.documentElement.dataset.theme || '');

// ── 分頁 ──
let VIEW = 'home';
function show(v, push = true) {
  VIEW = v;
  document.querySelectorAll('.view').forEach(x => x.classList.toggle('on', x.id === 'v-' + v));
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  try { sessionStorage.setItem('mview', v); } catch (e) {}
  window.scrollTo(0, 0);
  if (D) draw(v);
}
document.querySelectorAll('#nav button').forEach(b => b.onclick = () => show(b.dataset.v));
function draw(v) {
  if (drawn[v]) return;
  drawn[v] = true;
  ({ home: drawHome, board: drawBoard, verify: drawVerify, codes: drawCodes, more: drawMore })[v]();
}

// ── 停擺偵測（與電腦版相同規則）──
function staleInfo(d) {
  if (!d.generated_at || d.demo) return null;
  const last = new Date(d.generated_at), now = new Date(), age = (now - last) / 36e5;
  if (age <= 30) return null;
  const tpe = t => new Date(t.getTime() + 8 * 36e5);
  for (let day = new Date(Date.UTC(tpe(last).getUTCFullYear(), tpe(last).getUTCMonth(), tpe(last).getUTCDate())); day <= tpe(now); day = new Date(day.getTime() + 864e5)) {
    if ([0, 6].includes(day.getUTCDay())) continue;
    for (const [h, m, name] of [[6, 17, '盤前版'], [17, 10, '收盤版']]) {
      const t = new Date(day.getTime() + (h * 60 + m) * 6e4 - 8 * 36e5);
      if (t > last && t.getTime() + 2 * 36e5 <= now.getTime()) return { age: Math.round(age), slot: `${tpe(t).toISOString().slice(5, 10).replace('-', '/')} ${name}` };
    }
  }
  return null;
}

// ── 多空 ──
let SIDE = 'bull';
document.querySelectorAll('#side-seg button').forEach(b => b.onclick = () => {
  SIDE = b.dataset.side;
  document.querySelectorAll('#side-seg button').forEach(x => x.classList.toggle('on', x === b));
  drawSectors();
});
function drawHome() {
  const notes = $('#notes'); notes.replaceChildren();
  const mk = D.market || {}, add = (txt) => { const b = el('div', 'banner warn'); b.append(el('b', null, '⚠ '), document.createTextNode(txt)); notes.append(b); };
  const st = staleInfo(D);
  if (st) add(`資料已 ${st.age} 小時沒有更新（應有的「${st.slot}」沒有跑），內容可能過時。`);
  if (mk.cn?.stale) add('大陸期貨今天沒有新報價（休市或尚未開盤），沿用前一次報價。');
  if (mk.tw_holiday) add(`今日台股休市（${mk.tw_holiday}）。`);
  if (mk.cn?.rolled?.length) add('主力合約今日換月：' + mk.cn.rolled.map(c => BOARD[c]?.name || c).join('、'));
  if (D.industry?.stale?.length) add(D.industry.stale.join('、') + '，新上市櫃股票可能沒有類股。');
  drawSectors();
  const bars = $('#bars'); bars.replaceChildren();
  const max = Math.max(1, ...D.sectors.map(s => Math.abs(s.score)));
  D.sectors.forEach(s => {
    const r = el('div', 'bar'); const tr = el('div', 'tr'); const i = el('i');
    const w = Math.abs(s.score) / max * 50;
    i.style.cssText = `width:${w}%;left:${s.score >= 0 ? 50 : 50 - w}%;background:var(--${s.score >= 0 ? 'up' : 'dn'})`;
    tr.append(i); r.append(el('div', null, s.name), tr, el('div', 'v ' + cls(s.score), sc(s.score)));
    bars.append(r);
  });
}
function benMap(sector) {
  const m = {};
  sector.drivers.filter(d => d.contrib !== 0).forEach(d => (BOARD[d.code]?.stocks || []).forEach(x => { if (!m[x.code]) m[x.code] = { ...x, via: d.name }; }));
  return m;
}
function drawSectors() {
  const box = $('#sectors'); box.replaceChildren();
  const by = Object.fromEntries(D.sectors.map(s => [s.name, s]));
  const names = D[SIDE] || [];
  if (!names.length) { box.append(el('div', 'card empty', SIDE === 'bull' ? '今日沒有正向助攻的類股' : '今日沒有負向壓力的類股')); return; }
  names.forEach((n, i) => {
    const s = by[n]; const c = el('div', 'card');
    c.style.borderLeft = `4px solid var(--${SIDE === 'bull' ? 'up' : 'dn'})`;
    const h = el('div', 'sc-h'); h.append(el('span', 'rk', '#' + (i + 1)), el('span', 'nm', s.name));
    if (s.resonance) h.append(el('span', 'badge', '共振'));
    h.append(el('span', 'scr ' + cls(s.score), sc(s.score)));
    c.append(h, el('div', 'sub', `類股平均 ${pct(s.tw_avg_chg)}　漲 ${s.tw_up}／跌 ${s.tw_down}（${s.n} 檔）`));
    const chips = el('div', 'chips');
    s.drivers.filter(d => d.contrib !== 0).slice(0, 8).forEach(d => {
      const b = el('button', 'chip' + (d.at_limit ? ' lim' : '')); b.type = 'button';
      b.append(document.createTextNode((d.kind === 'spread' ? '〔價差〕' : d.kind === 'intl' ? '〔國際〕' : '') + d.name));
      const sm = el('small'); sm.append(el('span', cls(d.chg), pct(d.chg)), document.createTextNode(' → '), el('span', cls(d.contrib), sc(d.contrib)));
      b.append(sm); b.onclick = () => openDetail(d.code); chips.append(b);
    });
    c.append(chips);
    const ben = Object.values(benMap(s)).filter(x => x.name);
    if (ben.length) {
      const det = el('details', 'more'); det.append(el('summary', null, `直接受惠／受壓個股（${ben.length}）`));
      const rows = el('div', 'rows');
      ben.slice(0, 10).forEach(x => {
        const r = el('div', 'row static'); const l = el('div', 'l');
        const t = el('div', 't', x.name); t.append(el('span', 'cd', x.code), el('span', 'tag ' + (x.role === '產品' ? 'p' : 'c'), x.role === '產品' ? '受惠' : '受壓'));
        const sub = `經由 ${x.via}` + (x.verdict ? `・實測${x.verdict}` : '') + (x.oos_same ? '・樣本外確認' : '');
        l.append(t, el('div', 's', sub));
        const rr = el('div', 'r'); rr.append(el('b', cls(x.chg_pct), pct(x.chg_pct)));
        const ch = CHIPS[x.code]; if (ch) rr.append(el('small', null, `法人 ${lots(ch.inst)}`));
        r.append(l, rr); rows.append(r);
      });
      det.append(rows); c.append(det);
    }
    const det2 = el('details', 'more'); det2.append(el('summary', null, '前 5 強／前 5 弱'));
    const sp = el('div', 'split');
    for (const [title, list] of [['前5強', s.strong], ['前5弱', s.weak]]) {
      const col = el('div'); col.append(el('h4', null, title));
      if (!list.length) col.append(el('div', 'flat', '無資料'));
      list.forEach(x => { const m = el('div', 'mini'); m.append(el('span', null, x.name), el('span', cls(x.chg_pct), pct(x.chg_pct))); col.append(m); });
      sp.append(col);
    }
    det2.append(sp); c.append(det2);
    box.append(c);
  });
}

// ── 看板 ──
const INE = new Set(['SC', 'LU', 'NR', 'BC', 'EC']);
const groupOf = c => c.kind === 'spread' ? '價差' : c.kind === 'intl' ? '國際' : (c.ex === '上期所' && INE.has(c.code) ? '能源中心' : c.ex);
const GROUPS = ['全部', '上期所', '能源中心', '大商所', '鄭商所', '廣期所', '價差', '國際'];
let GRP = '全部', SORT = 'chg';
function drawBoard() {
  const seg = $('#ex-seg'); seg.replaceChildren();
  GROUPS.forEach(g => {
    const n = g === '全部' ? D.commodities.length : D.commodities.filter(c => groupOf(c) === g).length;
    if (!n) return;
    const b = el('button', g === GRP ? 'on' : '', `${g} ${n}`); b.type = 'button';
    b.onclick = () => { GRP = g; drawn.board = false; draw('board'); }; seg.append(b);
  });
  document.querySelectorAll('#sort-seg button').forEach(b => {
    b.classList.toggle('on', b.dataset.sort === SORT);
    b.onclick = () => { SORT = b.dataset.sort; drawn.board = false; draw('board'); };
  });
  const key = c => SORT === 'chg' ? c.chg : SORT === 'z' ? c.z : c.m5;
  const list = D.commodities.filter(c => GRP === '全部' || groupOf(c) === GRP)
    .sort((a, b) => (key(b) ?? -99) - (key(a) ?? -99));
  const box = $('#board'); box.replaceChildren();
  list.forEach(c => {
    const r = el('div', 'row'); const l = el('div', 'l');
    const t = el('div', 't', c.name); t.append(el('span', 'cd', c.code));
    if (c.at_limit) t.append(el('span', 'tag new', c.at_limit === 'up' ? '漲停' : '跌停'));
    if (c.rolled) t.append(el('span', 'tag lag', '換月'));
    const secs = [...new Set([...(c.links || []), ...(c.ref_links || [])].map(x => x.sector))];
    l.append(t, el('div', 's', `${groupOf(c)}・${secs.join('、') || '實測不顯著，不計分'}`));
    const rr = el('div', 'r'); rr.append(el('b', cls(c.chg), pct(c.chg)));
    rr.append(el('small', null, SORT === 'm5' ? `5日 ${sg(c.m5)}` : sg(c.z)));
    if (c.chg) r.style.background = `linear-gradient(90deg, transparent 55%, rgba(var(--${c.chg > 0 ? 'up' : 'dn'}-rgb),${Math.min(.25, .05 + Math.abs(c.z ?? c.chg / 3) * .07)}))`;
    r.append(l, rr); r.onclick = () => openDetail(c.code); box.append(r);
  });
  if (!list.length) box.append(el('div', 'empty', '沒有資料'));
}

// ── 驗證 ──
async function drawVerify() {
  try { TRACK ??= await getJSON('../data/track.json'); } catch (e) { $('#track').replaceChildren(el('div', 'empty', '尚無追蹤資料')); }
  if (TRACK) {
    const m = TRACK.summary.all.next, box = $('#track'); box.replaceChildren();
    const k = el('div', 'kpis');
    const kp = (t, v, s, c) => { const b = el('div', 'kpi'); b.append(el('div', 'k', t), el('div', 'v ' + (c || ''), v), el('div', 's', s)); k.append(b); };
    kp('多頭隔日', pp(m.bull_avg), '勝率 ' + wr(m.bull_win), cls(m.bull_avg));
    kp('空頭隔日', pp(m.bear_avg), '勝率 ' + wr(m.bear_win), cls(m.bear_avg));
    kp('多空價差', pp(m.spread_avg), '為正 ' + wr(m.spread_win), cls(m.spread_avg));
    box.append(k, el('p', 'note', `自 ${TRACK.since || '—'} 起，已開獎 ${m.n} 次${m.n < 20 ? '，樣本還太少，僅供觀察' : ''}。數字為類股扣掉大盤影響後的超額報酬。`));
    const rows = $('#tk-rows'); rows.replaceChildren();
    TRACK.rows.slice(0, 12).forEach(r => {
      const x = el('div', 'row static'); const l = el('div', 'l');
      const t = el('div', 't', `${r.run_at.replace('T', ' ').slice(5, 16)} ${r.session === 'night' ? '盤前' : '收盤'}`);
      if (!r.valid) t.append(el('span', 'tag no', '不計'));
      l.append(t, el('div', 's', `多：${r.bull.join('、') || '—'}｜空：${r.bear.join('、') || '—'}`));
      const rr = el('div', 'r'); rr.append(el('b', cls(r.next?.spread), r.next ? pp(r.next.spread) : '待開獎'), el('small', null, r.next_date ? r.next_date.slice(5) : ''));
      x.append(l, rr); rows.append(x);
    });
  }
  try { CAL ??= await getJSON('../data/calibration.json'); } catch (e) { $('#bt').replaceChildren(el('div', 'empty', '尚未校準')); }
  if (CAL) {
    const bt = CAL.backtest, box = $('#bt'); box.replaceChildren();
    const k = el('div', 'kpis two');
    const add = (t, m) => { const b = el('div', 'kpi'); b.append(el('div', 'k', t), el('div', 'v ' + cls(m?.spread_avg), pp(m?.spread_avg)), el('div', 's', `t 值 ${m?.spread_t ?? '—'}・${m?.days ?? 0} 天`)); k.append(b); };
    add('同日 多空價差', bt.calibrated_spreads_z?.lag0);
    add('隔日 多空價差', bt.calibrated_spreads_z?.lag1);
    if (bt.pre_combo) add('盤前版（國際＋陸期）', bt.pre_combo.lag0);
    if (bt.pre_intl) add('盤前版（僅國際）', bt.pre_intl.lag0);
    box.append(k, el('p', 'note', `${bt.test[0]}～${bt.test[1]} 樣本外測試（實測權重＋價差、z 分數）。t 值絕對值大於 2 才算統計上顯著；「同日」代表同步，「隔日」才是預測力。`));
  }
  try { HEAT ??= await getJSON('../data/score_history.json'); drawHeat(); } catch (e) { $('#heat-note').textContent = '尚無分數歷史'; }
}
let HMODE = 's';
document.querySelectorAll('#heat-seg button').forEach(b => b.onclick = () => {
  HMODE = b.dataset.m; document.querySelectorAll('#heat-seg button').forEach(x => x.classList.toggle('on', x === b)); drawHeat();
});
function drawHeat() {
  if (!HEAT) return;
  const days = (HEAT.days || []).filter(r => r.s && Object.keys(r.s).length).slice(-80);
  const tot = {}; days.forEach(r => Object.entries(r.s).forEach(([k, v]) => tot[k] = (tot[k] || 0) + Math.abs(v)));
  const names = Object.keys(tot).filter(k => tot[k] / days.length >= 1).sort((a, b) => tot[b] - tot[a]);
  const cv = $('#heat'), dpr = Math.min(3, devicePixelRatio || 1);
  const LW = 64, CW = 7, CH = 18, TOP = 16, W = LW + days.length * CW, H = TOP + names.length * CH;
  cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + 'px'; cv.style.height = H + 'px';
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const up = css('--up-rgb'), dn = css('--dn-rgb'), off = css('--panel2'), fg = css('--fg'), mut = css('--muted'), acc = css('--accent'), co = css('--cell-off');
  g.font = '12px sans-serif'; g.textBaseline = 'middle';
  let pm = '';
  days.forEach((r, j) => { const m = r.d.slice(5, 7); if (m !== pm) { g.fillStyle = mut; g.font = '10px monospace'; g.fillText(r.d.slice(5).replace('-', '/'), LW + j * CW, 8); pm = m; } });
  g.font = '12px sans-serif';
  names.forEach((n, i) => {
    g.fillStyle = fg; g.fillText(n, 0, TOP + i * CH + CH / 2);
    days.forEach((r, j) => {
      const s = r.s[n] ?? 0, a = r.a?.[n];
      let col = off;
      const tint = (v, full) => `rgba(${v > 0 ? up : dn},${(0.15 + Math.min(1, Math.abs(v) / full) * 0.85).toFixed(2)})`;
      if (HMODE === 's' && s) col = tint(s, 60);
      else if (HMODE === 'a' && a != null && a !== 0) col = tint(a, 2);
      else if (HMODE === 'x' && a != null && Math.abs(s) >= 10) col = (s > 0) === (a > 0) ? acc : co;
      g.fillStyle = col; g.fillRect(LW + j * CW, TOP + i * CH + 2, CW - 1, CH - 4);
    });
  });
  cv.onclick = e => {
    const b = cv.getBoundingClientRect(), x = e.clientX - b.left, y = e.clientY - b.top;
    const j = Math.floor((x - LW) / CW), i = Math.floor((y - TOP) / CH);
    if (j < 0 || i < 0 || j >= days.length || i >= names.length) return;
    const r = days[j], n = names[i];
    $('#heat-info').textContent = `${r.d} ${n}　分數 ${sc(r.s[n] ?? 0)}　實際 ${r.a?.[n] != null ? pp(r.a[n]) : '待更新'}`;
  };
  requestAnimationFrame(() => { const w = cv.parentElement; w.scrollLeft = w.scrollWidth; });   // 預設捲到最新日期
  let agree = 0, tot2 = 0;
  days.forEach(r => names.forEach(n => { const s = r.s[n] ?? 0, a = r.a?.[n]; if (a != null && Math.abs(s) >= 10) { tot2++; if ((s > 0) === (a > 0)) agree++; } }));
  $('#heat-note').textContent = `最近 ${days.length} 個交易日，左右滑動看更早的日子。紅＝偏多、綠＝偏空。|分數|≥10 的格子中，方向與實際同向 ${tot2 ? Math.round(agree / tot2 * 100) : '—'}%（回填部分為樣本內，會高估）。`;
}

// ── 代碼 ──
const EX_FULL = { '上期所': '上海期貨交易所', '能源中心': '上海國際能源交易中心', '大商所': '大連商品交易所', '鄭商所': '鄭州商品交易所', '廣期所': '廣州期貨交易所', '價差': '加工利潤／裂解價差', '國際': '美國期貨（NYMEX／COMEX／CBOT／ICE）' };
$('#q').addEventListener('input', () => { drawn.codes = false; draw('codes'); });
function drawCodes() {
  const q = $('#q').value.trim().toLowerCase(), box = $('#codes'); box.replaceChildren();
  let n = 0;
  for (const g of GROUPS.slice(1)) {
    const items = D.commodities.filter(c => groupOf(c) === g).filter(c => {
      if (!q) return true;
      const secs = [...(c.links || []), ...(c.manual || []), ...(c.ref_links || [])].map(x => x.sector).join(' ');
      return `${c.code} ${c.name} ${c.symbol || ''} ${secs} ${g} ${EX_FULL[g]}`.toLowerCase().includes(q);
    }).sort((a, b) => a.code.localeCompare(b.code));
    if (!items.length) continue;
    n += items.length;
    box.append(el('div', 'grp-h', `${g}　${EX_FULL[g]}・${items.length} 項`));
    const card = el('div', 'card'); const rows = el('div', 'rows');
    items.forEach(c => {
      const r = el('div', 'row'); const l = el('div', 'l');
      const t = el('div', 't'); t.append(el('b', 'mono', c.code), document.createTextNode(' ' + c.name));
      const secs = [...new Set([...(c.links || []), ...(c.manual || []), ...(c.ref_links || [])].map(x => x.sector))];
      l.append(t, el('div', 's', (c.kind === 'intl' ? c.symbol + '・' : '') + (secs.join('、') || '—')));
      const rr = el('div', 'r'); rr.append(el('b', cls(c.chg), pct(c.chg)));
      r.append(l, rr); r.onclick = () => openDetail(c.code); rows.append(r);
    });
    card.append(rows); box.append(card);
  }
  if (!n) box.append(el('div', 'empty', '找不到符合的商品'));
}

// ── 更多：資料來源、管理者 ──
async function drawMore() {
  setAdminUI();
  try {
    const st = await getJSON('../data/status.json'), box = $('#srcs'); box.replaceChildren();
    const bad = st.sources.filter(x => x.status !== 'ok').length;
    $('#src-h').textContent = `資料來源狀態（${bad ? bad + ' 項需注意' : '全部正常'}）`;
    st.sources.forEach(x => {
      const r = el('div', 'src'); const l = el('div');
      l.append(el('div', null, x.name), el('div', 'note', `${x.data_date || ''} ${x.rows != null ? x.rows + ' 筆' : ''} ${x.note || ''}`.trim()));
      l.lastChild.style.margin = '0';
      r.append(el('span', 'dot ' + x.status), l); box.append(r);
    });
  } catch (e) { $('#srcs').replaceChildren(el('div', 'empty', '讀取失敗')); }
}
const getKey = () => store.get('adminKey') || '';
function setAdminUI() {
  const on = !!getKey();
  $('#admin-login').classList.toggle('hidden', on);
  $('#admin-in').classList.toggle('hidden', !on);
}
$('#admin-go').onclick = async () => {
  const key = $('#admin-key').value.trim(); if (!key || !W_URL) return;
  $('#admin-msg').textContent = '驗證中…';
  try {
    const r = await fetch(W_URL + '/verify', { method: 'POST', headers: { 'X-Admin-Key': key } });
    const j = await r.json(); if (!j.ok) throw new Error(j.error || r.status);
    store.set('adminKey', key); $('#admin-key').value = ''; $('#admin-msg').textContent = ''; setAdminUI();
  } catch (e) { $('#admin-msg').textContent = '登入失敗：' + e.message; }
};
$('#admin-out').onclick = () => { store.set('adminKey', null); setAdminUI(); toast('已登出'); };
$('#refresh').onclick = async () => {
  const btn = $('#refresh'); btn.disabled = true; $('#upd').textContent = '觸發中…';
  try {
    const r = await fetch(W_URL + '/trigger', { method: 'POST', headers: { 'X-Admin-Key': getKey() } });
    const j = await r.json();
    if (r.status === 401) { store.set('adminKey', null); setAdminUI(); }
    if (!j.ok) throw new Error(j.error || r.status);
    $('#upd').textContent = '已觸發，約 2–4 分鐘後資料更新，屆時重新整理即可。';
  } catch (e) { $('#upd').textContent = '觸發失敗：' + e.message; btn.disabled = false; }
};
$('#to-desktop').onclick = () => { store.set('preferDesktop', '1'); location.href = '../' + location.hash; };

// ── 詳細頁 ──
const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs) => { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
function chart(rows, c) {
  const W = 360, H = 220, L = 46, R = 6, T = 8, PH = 140, ZT = 160, ZH = 44;
  const n = rows.length, xs = i => L + (W - L - R) * (n === 1 ? .5 : i / (n - 1));
  const cl = rows.map(r => r[1]).filter(v => v != null), lo = Math.min(...cl), hi = Math.max(...cl), span = hi - lo || 1;
  const ys = v => T + PH * (1 - (v - lo) / span);
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': `${c.name} 走勢` });
  for (const v of [lo, lo + span / 2, hi]) {
    svg.append(sv('line', { x1: L, x2: W - R, y1: ys(v), y2: ys(v), stroke: 'var(--line)', 'stroke-dasharray': '2 3' }));
    const t = sv('text', { x: L - 4, y: ys(v) + 3, 'text-anchor': 'end' }); t.textContent = +v.toPrecision(5); svg.append(t);
  }
  let d = ''; rows.forEach((r, i) => { if (r[1] != null) d += (d ? 'L' : 'M') + xs(i).toFixed(1) + ' ' + ys(r[1]).toFixed(1); });
  svg.append(sv('path', { d, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 1.8 }));
  const z0 = ZT + ZH / 2, bw = Math.max(1.4, (W - L - R) / n * .7);
  svg.append(sv('line', { x1: L, x2: W - R, y1: z0, y2: z0, stroke: 'var(--line)' }));
  rows.forEach((r, i) => {
    if (r[3] == null) return;
    const h = Math.min(1, Math.abs(r[3]) / 3) * ZH / 2;
    svg.append(sv('rect', { x: xs(i) - bw / 2, y: r[3] > 0 ? z0 - h : z0, width: bw, height: Math.max(h, .5), fill: r[3] > 0 ? 'var(--up)' : 'var(--dn)', opacity: Math.abs(r[3]) >= 2 ? 1 : .55 }));
  });
  const zl = sv('text', { x: L - 4, y: z0 + 3, 'text-anchor': 'end' }); zl.textContent = 'z'; svg.append(zl);
  for (const i of [0, n - 1]) { const t = sv('text', { x: xs(i), y: H - 2, 'text-anchor': i ? 'end' : 'start' }); t.textContent = rows[i][0].slice(5).replace('-', '/'); svg.append(t); }
  const cross = sv('line', { y1: T, y2: ZT + ZH, stroke: 'var(--muted)', 'stroke-width': .8, visibility: 'hidden' }); svg.append(cross);
  const info = el('div', 'chart-tip', '手指在圖上滑動看每日數字');
  const pick = ev => {
    const b = svg.getBoundingClientRect(), x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - b.left;
    const i = Math.max(0, Math.min(n - 1, Math.round(((x / b.width * W) - L) / (W - L - R) * (n - 1)))), r = rows[i];
    cross.setAttribute('x1', xs(i)); cross.setAttribute('x2', xs(i)); cross.setAttribute('visibility', 'visible');
    info.textContent = `${r[0]}　${r[1] ?? '—'}　${pct(r[2])}　${sg(r[3])}`;
  };
  svg.addEventListener('touchstart', pick, { passive: true }); svg.addEventListener('touchmove', pick, { passive: true }); svg.addEventListener('mousemove', pick);
  const w = el('div'); w.append(svg, info); return w;
}
const STATUS_TAG = { '有效': 'ok', '隔日有效': 'lag', '方向相反': 'rev', '不顯著': 'no', '數據新增': 'new', '樣本不足': 'no' };
async function openDetail(code, push = true) {
  const c = BOARD[code]; if (!c) return;
  const kind = c.kind || 'commodity';
  $('#d-title').textContent = `${c.name}　${c.code}`;
  const body = $('#d-body'); body.replaceChildren();
  const head = el('div'); head.append(el('div', 'big ' + cls(c.chg), pct(c.chg) + (c.at_limit === 'up' ? ' 漲停' : c.at_limit === 'down' ? ' 跌停' : '')));
  head.append(el('div', 'note', `${groupOf(c)}${kind === 'intl' ? '・' + c.symbol : ''}${c.rolled ? '・今日換月' : ''}`));
  body.append(head);
  const k = el('div', 'kpis two'), kp = (t, v, s, cc) => { const b = el('div', 'kpi'); b.append(el('div', 'k', t), el('div', 'v ' + (cc || ''), v), el('div', 's', s || '')); k.append(b); };
  if (kind === 'commodity') {
    kp('最新價', String(c.last ?? '—'), `昨結算 ${c.base ?? '—'}`);
    kp('今日 z', sg(c.z), c.sigma ? `波動度 ${c.sigma}%` : '', cls(c.z));
    kp('5 日動能', sg(c.m5), '', cls(c.m5)); kp('20 日動能', sg(c.m20), '', cls(c.m20));
    kp('漲跌停', c.limit ? `±${+c.limit.toFixed(2)}%` : '—', c.limit_src && c.limit_src !== '預設' ? c.limit_src : '預設值');
    kp('持倉量', c.oi ? Math.round(c.oi).toLocaleString() : '—', c.quote_date || '');
  } else if (kind === 'spread') {
    kp('今日變化', pct(c.chg), '佔產品價格', cls(c.chg)); kp('z', sg(c.z), '', cls(c.z));
    kp('價差水準', String(c.level ?? '—'), (c.legs || []).map(l => BOARD[l.code]?.name || l.code).join('／'));
  } else {
    kp('最新漲跌', pct(c.chg), c.bar_date ? '美國 ' + c.bar_date : '', cls(c.chg));
    if (c.settled_date) kp('已收盤', pct(c.chg_settled), '美國 ' + c.settled_date, cls(c.chg_settled));
  }
  body.append(k);
  body.append(el('h2', null, '近 120 日走勢'));
  const slot = el('div', 'empty', '載入中…'); body.append(slot);
  const secH = el('h2', null, '對應類股'); const secBox = el('div', 'card'); const secRows = el('div', 'rows'); secBox.append(secRows);
  body.append(secH, secBox);
  // 類股（實測需要校準檔，背景載入）
  const fillSectors = () => {
    secRows.replaceChildren();
    const used = Object.fromEntries((c.links || []).map(l => [l.sector, l.w]));
    const cal = kind === 'intl' ? (CAL?.intl?.pairs || []).filter(p => p.code === code) : (CAL?.pairs || []).filter(p => p.code === code);
    const secs = [...new Set([...Object.keys(used), ...(c.manual || []).map(m => m.sector), ...cal.map(p => p.sector), ...(c.ref_links || []).map(l => l.sector)])];
    if (!secs.length) secRows.append(el('div', 'empty', '沒有對應類股'));
    secs.forEach(s => {
      const p = cal.find(x => x.sector === s) || {}, ct = D.sectors.find(x => x.name === s)?.drivers?.find(x => x.code === code)?.contrib;
      const r = el('div', 'row static'), l = el('div', 'l');
      const t = el('div', 't', s); if (p.status) t.append(el('span', 'tag ' + (STATUS_TAG[p.status] || 'no'), p.status));
      l.append(t, el('div', 's', `採用權重 ${used[s] != null ? (used[s] > 0 ? '+' : '') + used[s] : '0'}・同日 ρ ${p.rho0 != null ? p.rho0.toFixed(2) : '—'}（t ${p.t0 != null ? p.t0.toFixed(1) : '—'}）`));
      const rr = el('div', 'r'); rr.append(el('b', cls(ct), ct != null ? sc(ct) : '—'), el('small', null, '今日貢獻'));
      r.append(l, rr); secRows.append(r);
    });
  };
  fillSectors();
  if (!CAL) getJSON('../data/calibration.json').then(j => { CAL = j; fillSectors(); }).catch(() => {});
  if (c.stocks?.length) {
    body.append(el('h2', null, '受惠／受壓個股'));
    const card = el('div', 'card'), rows = el('div', 'rows'); card.append(rows);
    c.stocks.forEach(x => {
      const r = el('div', 'row static'), l = el('div', 'l');
      const t = el('div', 't', x.name || x.code); t.append(el('span', 'cd', x.code), el('span', 'tag ' + (x.role === '產品' ? 'p' : 'c'), x.role === '產品' ? '受惠' : '受壓'));
      l.append(t, el('div', 's', (x.why || '') + (x.verdict ? `・實測${x.verdict}` : '')));
      const rr = el('div', 'r'); rr.append(el('b', cls(x.chg_pct), pct(x.chg_pct)));
      r.append(l, rr); rows.append(r);
    });
    body.append(card);
  }
  // 開啟
  $('#sheet').classList.add('on'); $('#sheet').scrollTop = 0; document.body.style.overflow = 'hidden';
  if (push) { try { history.pushState({ detail: code }, '', '#' + code); } catch (e) {} }
  // 走勢（最後載入，不擋畫面）
  try {
    KL ??= await getJSON('../data/klines.json');
    const rows = (KL.data[code] || []).slice();
    if (kind === 'commodity' && c.quote_date && c.last != null && (!rows.length || rows.at(-1)[0] < c.quote_date)) rows.push([c.quote_date, c.last, c.chg, c.z]);
    slot.replaceWith(rows.length > 1 ? chart(rows, c) : el('div', 'empty', '沒有歷史資料'));
  } catch (e) { slot.textContent = '走勢資料尚未產生'; }
}
function closeDetail() {
  $('#sheet').classList.remove('on'); document.body.style.overflow = '';
}
$('#d-back').onclick = () => { if (history.state?.detail) history.back(); else { closeDetail(); try { history.replaceState(null, '', location.pathname); } catch (e) {} } };
window.addEventListener('popstate', e => {
  if (e.state?.detail) openDetail(e.state.detail, false); else closeDetail();
});

// ── 啟動 ──
(async () => {
  document.documentElement.style.setProperty('--top-h', $('#top').offsetHeight + 'px');
  try {
    D = await getJSON('../data/latest.json');
  } catch (e) { $('#meta').textContent = '資料載入失敗，請稍後重新整理'; return; }
  BOARD = Object.fromEntries(D.commodities.map(c => [c.code, c]));
  CHIPS = D.chips || {};
  $('#meta').textContent = `${D.premarket?.on ? '盤前版' : '收盤版'}・更新 ${fmtTime(D.generated_at)}・台股 ${D.tw_date || '—'}`;
  let v = 'home'; try { v = sessionStorage.getItem('mview') || 'home'; } catch (e) {}
  show(v, false);
  const hc = decodeURIComponent(location.hash.slice(1));
  if (hc && BOARD[hc]) { try { history.replaceState(null, '', location.pathname); } catch (e) {} openDetail(hc); }
})();
