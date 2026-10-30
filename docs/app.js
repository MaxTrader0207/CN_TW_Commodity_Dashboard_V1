// 陸期 × 台股 多空儀表板 — 頁面程式（由 index.html 載入；放在獨立檔案以便用 CSP 禁止頁內腳本）
const $ = s => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const cls = v => v == null ? 'flat' : v > 0 ? 'up' : v < 0 ? 'dn' : 'flat';
const pct = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + '%';
const sc = v => (v > 0 ? '+' : '') + v.toFixed(1);
const tip = $('#tip');
function hoverTip(node, text) {
  node.addEventListener('mousemove', e => {
    tip.textContent = text; tip.style.display = 'block';
    const x = Math.min(e.clientX + 12, innerWidth - 270); tip.style.left = x + 'px'; tip.style.top = (e.clientY + 14) + 'px';
  });
  node.addEventListener('mouseleave', () => tip.style.display = 'none');
}

let BOARD = {};   // code → 商品／價差（含受惠股）
let CHIPS = {};   // 代號 → 籌碼（法人、量比、融資券）
let LATEST = null, KL = null;   // 目前顯示的資料、商品日K摘要（詳細面板用，第一次打開時才載入）
const lots = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toLocaleString();
function chipTip(code) {
  const c = CHIPS[code]; if (!c) return '';
  return `\n三大法人 ${lots(c.inst)} 張（外資 ${lots(c.f)}、投信 ${lots(c.t)}、自營 ${lots(c.d)}）` +
    `\n量比 ${c.vr ?? '—'}（今日量 ÷ 前 5 日均量）` +
    `\n融資 ${lots(c.mg)}（餘 ${c.mgb?.toLocaleString() ?? '—'}）　融券 ${lots(c.st)}（餘 ${c.stb?.toLocaleString() ?? '—'}）`;
}
function chipCells(tr, code) {
  const c = CHIPS[code] || {};
  const a = tr.insertCell(); a.className = 'n ch ' + cls(c.inst); a.textContent = c.inst == null ? '—' : lots(c.inst);
  const v = tr.insertCell(); v.className = 'n ch'; v.textContent = c.vr == null ? '—' : c.vr + 'x';
  if (c.vr >= 2) v.style.color = 'var(--accent)';
}

function benMap(sector) {
  // 這個類股的主要驅動商品 → 受惠／受壓個股（股票代號 → 角色與來源）
  const m = {};
  sector.drivers.filter(d => d.contrib !== 0).forEach(d => {
    (BOARD[d.code]?.stocks || []).forEach(x => { if (!m[x.code]) m[x.code] = { ...x, via: d.name }; });
  });
  return m;
}

function stockTable(rows, ben) {
  const t = el('table');
  if (!rows.length) { const tr = t.insertRow(); const td = tr.insertCell(); td.className = 'flat'; td.textContent = '無資料'; return t; }
  if (Object.keys(CHIPS).length) {
    const h = t.insertRow(); ['', '', '收盤', '漲跌', '法人(張)', '量比'].forEach(x => { const c = el('th', null, x); c.style.cssText = 'font-size:10.5px;color:var(--muted);font-weight:500;text-align:right'; h.append(c); });
  }
  rows.forEach(s => {
    const tr = t.insertRow();
    tr.insertCell().textContent = s.code;
    const nm = tr.insertCell(); nm.className = 'nm'; nm.textContent = s.name;
    const b = ben && ben[s.code];
    if (b) {
      const mk = el('span', 'mk ' + (b.role === '產品' ? 'p' : 'c'), b.role === '產品' ? '受惠' : '受壓');
      nm.append(mk); hoverTip(nm, `${b.via}：${b.why}`);
    }
    const c = tr.insertCell(); c.className = 'n'; c.textContent = s.close;
    const p = tr.insertCell(); p.className = 'n ' + cls(s.chg_pct); p.textContent = pct(s.chg_pct);
    if (Object.keys(CHIPS).length) { chipCells(tr, s.code); hoverTip(tr, `${s.code} ${s.name}${b ? `\n${b.via}：${b.why}` : ''}${chipTip(s.code)}`); }
  });
  return t;
}

function sectorCard(s, i, side) {
  const card = el('div', 'card');
  const h = el('div', 'card-h');
  h.append(el('span', 'rank', '#' + (i + 1)), el('span', 'sec', s.name));
  if (s.resonance) h.append(el('span', 'badge res', '共振'));
  const score = el('span', 'score ' + cls(s.score), sc(s.score)); h.append(score);
  card.append(h);
  const sub = el('div', 'sub');
  sub.textContent = `台股類股平均 ${pct(s.tw_avg_chg)}　漲 ${s.tw_up}／跌 ${s.tw_down}（${s.n} 檔）`;
  card.append(sub);

  const chips = el('div', 'chips');
  s.drivers.filter(d => d.contrib !== 0).slice(0, 6).forEach(d => {
    const c = el('span', 'chip' + (d.at_limit ? ' lim' : ''));
    const lim = d.at_limit === 'up' ? ' 漲停' : d.at_limit === 'down' ? ' 跌停' : '';
    c.append(document.createTextNode((d.kind === 'spread' ? '〔價差〕' : d.kind === 'intl' ? '〔國際〕' : '') + d.name + ' '));
    const zz = BOARD[d.code]?.z;
    const b = el('b', cls(d.chg), pct(d.chg) + lim + (zz != null && d.kind !== 'spread' ? ` ${zz > 0 ? '+' : ''}${zz}σ` : '')); c.append(b);
    c.append(document.createTextNode(' → '));
    c.append(el('b', cls(d.contrib), sc(d.contrib)));
    if (d.src === '新增') c.append(el('span', 'tag new', '新增'));
    c.classList.add('click'); c.onclick = () => openDetail(d.code);
    const ib = d.kind === 'intl' ? BOARD[d.code] : null;
    hoverTip(c, `${d.ex} ${d.code}｜${d.src || '手動'}權重 ${d.w > 0 ? '+' : ''}${d.w}｜${d.why}` +
      (ib?.settled_date ? `\n美國 ${ib.settled_date} 收盤漲跌（台股收盤後才發生，對今天是新資訊）` : ''));
    chips.append(c);
  });
  card.append(chips);

  const ben = benMap(s);
  const benRows = Object.values(ben).filter(x => x.name);
  if (benRows.length) {
    const box = el('div', 'ben');
    box.append(el('h4', null, '直接受惠／受壓個股'));
    const t = el('table');
    benRows.slice(0, 8).forEach(x => {
      const tr = t.insertRow();
      tr.insertCell().textContent = x.code;
      const nm = tr.insertCell(); nm.className = 'nm'; nm.textContent = x.name;
      nm.append(el('span', 'mk ' + (x.role === '產品' ? 'p' : 'c'), x.role === '產品' ? '受惠' : '受壓'));
      tr.insertCell().append(el('span', 'via', x.via));
      const v = tr.insertCell();
      if (x.verdict) v.append(el('span', 'tag ' + ({ '一致': 'ok', '隔日一致': 'lag', '相反': 'rev' }[x.verdict] || 'no'),
        '實測' + x.verdict + (x.oos_same ? '・樣本外確認' : '')));
      const p = tr.insertCell(); p.className = 'n ' + cls(x.chg_pct); p.textContent = pct(x.chg_pct);
      if (Object.keys(CHIPS).length) chipCells(tr, x.code);
      hoverTip(tr, `${x.why}${x.rho0 != null ? `\n歷史同日相關 ρ=${x.rho0 > 0 ? '+' : ''}${x.rho0}` : ''}` +
        (x.oos_same ? '\n前 60% 與後 40% 期間都顯著、方向一致' : '') + (x.oos_next ? '\n隔日關聯在測試期仍成立' : '') + chipTip(x.code));
    });
    box.append(t);
    card.append(box);
  }

  const lists = el('div', 'lists');
  const a = el('div'); a.append(el('h4', null, '前5強'), stockTable(s.strong, ben));
  const b = el('div'); b.append(el('h4', null, '前5弱'), stockTable(s.weak, ben));
  if (side === 'bear') lists.append(b, a); else lists.append(a, b);
  card.append(lists);
  return card;
}

// 停擺偵測（與 .github/scripts/watchdog.py 同規則）：超過 30 小時沒更新，且之後有週一~五 06:17／17:10 的排程（+2 小時）已經過了
function staleInfo(d) {
  if (!d.generated_at || d.demo) return null;
  const last = new Date(d.generated_at), now = new Date(), age = (now - last) / 36e5;
  if (age <= 30) return null;
  const tpe = t => new Date(t.getTime() + 8 * 36e5);   // 以 UTC 欄位表示台北時間
  let missed = null;
  for (let day = new Date(Date.UTC(tpe(last).getUTCFullYear(), tpe(last).getUTCMonth(), tpe(last).getUTCDate())); day <= tpe(now); day = new Date(day.getTime() + 864e5)) {
    if ([0, 6].includes(day.getUTCDay())) continue;
    for (const [h, m, name] of [[6, 17, '盤前版'], [17, 10, '日盤版']]) {
      const t = new Date(day.getTime() + (h * 60 + m) * 6e4 - 8 * 36e5);   // 轉回真實時間
      if (t > last && t.getTime() + 2 * 36e5 <= now.getTime()) { missed = `${tpe(t).toISOString().slice(5, 10).replace('-', '/')} ${name}`; break; }
    }
    if (missed) break;
  }
  return missed ? { age: Math.round(age), slot: missed } : null;
}

function render(d) {
  $('#demo').style.display = d.demo ? 'block' : 'none';
  const mk = d.market || {}, notes = [];
  if (mk.cn?.stale) notes.push(`大陸期貨今天沒有新報價（休市或尚未開盤），以下漲跌沿用 ${fmtTime(mk.cn.newest_quote)} 的報價，這次訊號不計入命中率。`);
  if (mk.tw_holiday) notes.push(`今日台股休市（${mk.tw_holiday}），台股資料為 ${d.tw_date || '—'}。`);
  else if (mk.tw_open_today === false) notes.push(`今日台股休市，台股資料為 ${d.tw_date || '—'}。`);
  if (mk.cn?.rolled?.length) notes.push(`主力合約今日換月：${mk.cn.rolled.map(c => d.commodities.find(x => x.code === c)?.name || c).join('、')}（漲跌已用新合約的昨結算計算）。`);
  const st = staleInfo(d);
  if (st) notes.unshift(`⚠ 資料已 ${st.age} 小時沒有更新（最後更新 ${fmtTime(d.generated_at)}，之後應有的「${st.slot}」沒有跑），以下內容可能過時。可按右上角「立即更新」。`);
  if (d.industry?.stale?.length) notes.push(`⚠ ${d.industry.stale.join('、')}（來源抓取失敗，沿用舊快取），新上市櫃股票可能沒有類股。`);
  $('#mkt-note').textContent = notes.join('　');
  $('#mkt-note').style.display = notes.length ? 'block' : 'none';
  $('#m-session').textContent = d.premarket?.on ? '盤前版：陸期夜盤＋國際隔夜' : d.session_label;
  $('#m-time').textContent = '更新 ' + d.generated_at.replace('T', ' ').slice(0, 16);
  $('#m-tw').textContent = '台股資料日 ' + (d.tw_date || '—');
  $('#minlots').textContent = d.params.min_lots;
  $('#m-weights').textContent = '權重 ' + (d.weights_mode || '手動') +
    (d.calibration?.generated_at ? `（校準 ${d.calibration.generated_at.slice(0, 10)}）` : '');
  const byName = Object.fromEntries(d.sectors.map(s => [s.name, s]));
  BOARD = Object.fromEntries(d.commodities.map(c => [c.code, c]));
  LATEST = d;
  CHIPS = d.chips || {};

  for (const side of ['bull', 'bear']) {
    const box = $('#' + side); box.replaceChildren();
    if (!d[side].length) box.append(el('div', 'card empty', side === 'bull' ? '今日無正向助攻類股' : '今日無負向壓力類股'));
    d[side].forEach((n, i) => box.append(sectorCard(byName[n], i, side)));
  }

  const bars = $('#bars'); bars.replaceChildren();
  const max = Math.max(1, ...d.sectors.map(s => Math.abs(s.score)));
  d.sectors.forEach(s => {
    const name = el('div', null, s.name);
    const track = el('div', 'bar-track');
    const bar = el('div', 'bar');
    const w = Math.abs(s.score) / max * 50;
    bar.style.width = w + '%';
    bar.style.left = s.score >= 0 ? '50%' : (50 - w) + '%';
    bar.style.background = s.score >= 0 ? 'var(--up)' : 'var(--dn)';
    track.append(bar);
    hoverTip(track, s.drivers.slice(0, 4).map(x => `${x.name} ${pct(x.chg)} → ${sc(x.contrib)}`).join('　') || '無報價');
    bars.append(name, track, el('div', 'n ' + cls(s.score), sc(s.score)));
  });

  const board = $('#board'); board.replaceChildren();
  for (const ex of ['價差', '上期所', '大商所', '鄭商所', '廣期所', '國際']) {
    if (!d.commodities.some(c => c.ex === ex)) continue;
    board.append(el('div', 'ex-h', ex === '價差' ? '價差（加工利潤）：今日變化佔產品價格 %，括號為幾個標準差'
      : ex === '國際' ? (d.premarket?.on ? '國際隔夜（Yahoo，最後一根已收盤的美國日線；標「盤前計分」的已計入類股分數）'
                                        : '國際對照（Yahoo，美國期貨最新價 vs 前一交易日；日盤版只做交叉確認，不計分）')
      : ex + (ex === '上期所' ? '（含上海國際能源中心）　括號為今日漲跌是近 20 日波動度的幾倍（σ）' : '')));
    const g = el('div', 'board');
    d.commodities.filter(c => c.ex === ex).sort((a, b) => (b.chg ?? -99) - (a.chg ?? -99)).forEach(c => {
      if (c.kind === 'spread') return g.append(spreadTile(c));
      if (c.kind === 'intl') return g.append(intlTile(c));
      const t = el('div', 'tile');
      const strength = c.chg == null ? 0 : Math.min(1, c.z != null ? Math.abs(c.z) / 3 : Math.abs(c.chg) / c.limit);
      if (c.chg) t.style.background = `rgba(var(${c.chg > 0 ? '--up-rgb' : '--dn-rgb'}),${0.08 + strength * 0.4})`;
      if (c.at_limit) t.style.borderColor = 'var(--accent)';
      const t1 = el('div', 't1'); t1.append(el('b', null, c.name), el('span', null, c.code));
      const lim = c.at_limit === 'up' ? ' 漲停' : c.at_limit === 'down' ? ' 跌停' : '';
      if (c.rolled) t1.append(el('span', 'tag lag', '換月'));
      const limChanged = c.limit_default != null && c.limit_src && c.limit_src !== '預設' && Math.abs(c.limit - c.limit_default) >= 0.5;
      if (limChanged) t1.append(el('span', 'tag new', `停板${+c.limit.toFixed(1)}%`));
      if (c.intl?.length) {
        const ag = c.intl.filter(x => x.agree === true).length, dv = c.intl.filter(x => x.agree === false).length;
        if (ag || dv) t1.append(el('span', 'xb ' + (dv > ag ? 'dv' : 'ag'), dv > ag ? '國際背離' : '國際同向'));
      }
      const t2 = el('div', 't2 ' + cls(c.chg), pct(c.chg) + lim);
      if (c.z != null) t2.append(el('small', null, ` (${c.z > 0 ? '+' : ''}${c.z}σ)`));
      t.append(t1, t2,
        el('div', 't3', c.links.map(l => l.sector + (l.w > 0 ? '+' : '−')).join(' ') || '實測不顯著，不計分'));
      const sg = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(1) + 'σ';
      const sg1 = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(1);
      if (c.m5 != null || c.m20 != null) t.append(el('div', 't4', `5/20日 ${sg1(c.m5)}/${sg1(c.m20)}σ`));
      t.onclick = () => openDetail(c.code);
      hoverTip(t, (c.sigma ? `近 20 日波動度 ${c.sigma}%／日，今日 ${c.z > 0 ? '+' : ''}${c.z}σ（2σ 為訊號滿格）\n` : '') +
        (c.m5 != null ? `多日動能：5 日累計 ${sg(c.m5)}、20 日累計 ${sg(c.m20)}（累計漲跌 ÷ (σ×√天數)）${d.momentum?.adopt ? '，5 日動能已計入分數' : '，僅顯示、不計分'}\n` : '') +
        (c.limit ? `漲跌停 ±${+c.limit.toFixed(2)}%（${c.limit_src === '預設' || !c.limit_src ? '預設值' : c.limit_src + ' 公告'}${limChanged ? `；一般 ±${c.limit_default}%` : ''}）\n` : '') +
        c.links.map(l => `${l.sector}（${l.w > 0 ? '+' : ''}${l.w}）${l.why}`).join('\n') +
        (c.intl?.length ? '\n\n國際對照：' + c.intl.map(x => `${x.name} ${pct(x.chg)}`).join('、') : ''));
      g.append(t);
    });
    board.append(g);
  }

  renderCodes(d);

  const map = $('#map'); map.replaceChildren();
  const hr = map.insertRow(); ['交易所', '商品', '目前採用（權重）', '邏輯'].forEach(x => { const th = el('th', null, x); hr.append(th); });
  d.commodities.filter(c => c.kind !== 'intl').forEach(c => {
    const tr = map.insertRow();
    tr.insertCell().textContent = c.ex;
    tr.insertCell().textContent = `${c.name} ${c.code}`;
    const s = tr.insertCell();
    if (!c.links.length) s.append(el('span', 'flat', '（實測皆不顯著，暫不計分）'));
    c.links.forEach((l, i) => {
      if (i) s.append(document.createElement('br'));
      s.append(el('span', l.w > 0 ? 'up' : 'dn', `${l.sector} ${l.w > 0 ? '+' : ''}${l.w}`));
      if (l.src && l.src !== '手動') s.append(document.createTextNode(' '), el('span', 'tag ' + (l.src === '新增' ? 'new' : 'ok'), l.src));
    });
    const w = tr.insertCell();
    c.links.forEach((l, i) => { if (i) w.append(document.createElement('br')); w.append(document.createTextNode(l.why)); });
  });
}

// ── 訊號驗證 ──
const pp = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + '%';   // 超額報酬（百分點）
const wr = v => v == null ? '—' : Math.round(v * 100) + '%';
const SESS = { day: '日盤版', night: '夜盤版' };

function kpiBox(k, v, s, vcls) {
  const b = el('div', 'kpi'); b.append(el('div', 'k', k), el('div', 'v ' + (vcls || ''), v));
  if (s) b.append(el('div', 's', s));
  return b;
}

function intlTile(c) {
  const t = el('div', 'tile intl');
  t.onclick = () => openDetail(c.code);
  const strength = c.chg == null ? 0 : Math.min(1, Math.abs(c.chg) / 4);
  if (c.chg) t.style.background = `rgba(var(${c.chg > 0 ? '--up-rgb' : '--dn-rgb'}),${0.08 + strength * 0.4})`;
  const t1 = el('div', 't1'); t1.append(el('b', null, c.name));
  const cn = (c.cn || []).map(x => BOARD[x]).filter(Boolean);
  if (c.links?.length) t1.append(el('span', 'tag ok', '盤前計分'));
  const t2 = el('div', 't2 ' + cls(c.chg), c.chg == null ? '—' : pct(c.chg));
  if (c.links?.length && c.z != null) t2.append(el('small', null, ` (${c.z > 0 ? '+' : ''}${c.z}σ)`));
  t.append(t1, t2,
    el('div', 't3', (c.links?.length ? `美國 ${c.settled_date?.slice(5)} 收盤　` : '') +
      (cn.length ? '陸期 ' + cn.map(x => `${x.name} ${pct(x.chg)}`).join(' ') : '')));
  hoverTip(t, `${c.symbol}　${c.bar_date ? '美國交易日 ' + c.bar_date : ''}${c.asof ? '（' + fmtTime(c.asof) + '）' : ''}\n` +
    (c.ref_links || []).map(l => `${l.sector}：${l.why}${l.status ? `｜實測 ${l.status}` : ''}`).join('\n') +
    (c.links?.length ? '\n盤前版：以已收盤的美國日線計入類股分數（實測權重）' : '\n日盤版只做交叉確認，不計入類股分數'));
  return t;
}

function spreadTile(c) {
  const t = el('div', 'tile spr');
  t.onclick = () => openDetail(c.code);
  const strength = c.chg == null ? 0 : Math.min(1, Math.abs(c.z || 0) / 3);
  if (c.chg) t.style.background = `rgba(var(${c.chg > 0 ? '--up-rgb' : '--dn-rgb'}),${0.08 + strength * 0.4})`;
  const t1 = el('div', 't1'); t1.append(el('b', null, c.name));
  const t2 = el('div', 't2 ' + cls(c.chg), c.chg == null ? '—' : pct(c.chg));
  if (c.z != null) t2.append(el('small', null, `(${c.z > 0 ? '+' : ''}${c.z}σ)`));
  t.append(t1, t2, el('div', 't3', c.links.length ? c.links.map(l => l.sector + (l.w > 0 ? '+' : '−')).join(' ') : '實測不顯著，不計分'));
  const legs = (c.legs || []).map(l => `${l.coef > 0 ? '+' : '−'}${Math.abs(l.coef) === 1 ? '' : Math.abs(l.coef)} ${BOARD[l.code]?.name || l.code}`).join(' ');
  hoverTip(t, `${c.name} = ${legs.replace(/^\+/, '')}${c.level != null ? `\n目前水準 ${c.level}` : ''}\n` +
    (c.manual || []).map(m => `${m.sector}：${m.w > 0 ? '利潤擴大有利' : '利潤擴大不利'}`).join('\n'));
  return t;
}

function renderTrack(t) {
  const box = $('#tk-kpis'); box.replaceChildren();
  const all = t.summary.all.next;
  $('#tk-hint').textContent = `自 ${t.since || '—'} 起累積；多空前 3 類股在訊號後下一個台股交易日的超額報酬（類股 − β × 加權指數）`;
  for (const [key, label] of [['all', '全部'], ['night', '夜盤版（盤前）'], ['day', '日盤版（收盤後）']]) {
    const m = key === 'all' ? all : t.summary[key].next;
    box.append(el('div', 'k', label + `　已開獎 ${m.n} 次`));
    const g = el('div', 'kpis'); g.style.marginBottom = '8px';
    g.append(kpiBox('多頭隔日', pp(m.bull_avg), '勝率 ' + wr(m.bull_win), cls(m.bull_avg)),
             kpiBox('空頭隔日', pp(m.bear_avg), '勝率 ' + wr(m.bear_win), cls(m.bear_avg)),
             kpiBox('多空價差', pp(m.spread_avg), '為正 ' + wr(m.spread_win), cls(m.spread_avg)));
    box.append(g);
  }
  const n = all.n;
  $('#tk-note').textContent = (n < 20 ? `目前只開獎 ${n} 次，樣本太少，數字只供觀察，累積 20 次以上再判斷。` : '') +
    (t.summary.excluded ? `另有 ${t.summary.excluded} 筆因排程延遲、在盤中才產生的訊號不計入。` : '') +
    '多頭勝率 = 超額報酬為正的比例；空頭勝率 = 為負的比例。';

  const tb = $('#tk-rows'); tb.replaceChildren();
  const h = tb.insertRow(); ['訊號時間', '版本', '多頭 3', '空頭 3', '開獎日', '多頭', '空頭', '價差'].forEach(x => h.append(el('th', null, x)));
  t.rows.forEach(r => {
    const tr = tb.insertRow();
    tr.insertCell().textContent = r.run_at.replace('T', ' ').slice(5, 16);
    const v = tr.insertCell(); v.textContent = SESS[r.session] || r.session;
    if (!r.valid) { v.append(document.createElement('br'), el('span', 'tag no', '不計')); hoverTip(v, r.invalid_reason); }
    tr.insertCell().textContent = r.bull.join('、') || '—';
    tr.insertCell().textContent = r.bear.join('、') || '—';
    const x = r.next;
    tr.insertCell().textContent = r.next_date ? r.next_date.slice(5) : '待開獎';
    for (const k of ['bull_avg', 'bear_avg', 'spread']) {
      const c = tr.insertCell(); c.className = 'n ' + cls(x?.[k]); c.textContent = x ? pp(x[k]) : '—';
    }
  });
}

function btRow(t, label, m) {
  const tr = t.insertRow();
  tr.insertCell().textContent = label;
  for (const [v, f] of [[m.days, x => x ?? '—'], [m.bull_avg, pp], [m.bull_win, wr], [m.bear_avg, pp], [m.bear_win, wr], [m.spread_avg, pp], [m.spread_t, x => x == null ? '—' : x.toFixed(1)]]) {
    const c = tr.insertCell(); c.className = 'n ' + (f === pp ? cls(v) : ''); c.textContent = f(v);
  }
}

let CAL = null, calFilter = 'all';
const STATUS_TAG = { '有效': 'ok', '隔日有效': 'lag', '方向相反': 'rev', '不顯著': 'no', '數據新增': 'new', '樣本不足': 'few' };

function renderCalibration(c) {
  CAL = c;
  const bt = c.backtest;
  $('#bt-hint').textContent = `${bt.train[0]}～${bt.train[1]} 估計 β 與權重，${bt.test[0]}～${bt.test[1]} 測試（測試期資料完全沒參與）`;
  const t = $('#bt'); t.replaceChildren();
  const h = t.insertRow(); ['', '天數', '多頭', '勝率', '空頭', '勝率', '價差', 't 值'].forEach(x => h.append(el('th', null, x)));
  const sec = (txt) => { const r = t.insertRow(); const c0 = r.insertCell(); c0.colSpan = 8; c0.append(el('b', null, txt)); };
  for (const [lag, title] of [['lag1', '隔日（預測力）'], ['lag0', '同日（關聯是否存在）']]) {
    sec(title);
    for (const [k, label] of [['manual', '手動權重'], ['calibrated', '實測權重'], ['calibrated_spreads', '實測＋價差']]) {
      if (bt[k]) btRow(t, label + '・÷漲跌停', bt[k][lag]);
      if (bt[k + '_z']) btRow(t, label + '・z 分數', bt[k + '_z'][lag]);
    }
    for (const [k, label] of [['mom_1d', '動能・1 日'], ['mom_5d', '動能・5 日累計'], ['mom_20d', '動能・20 日累計'], ['mom_combo', '動能・1 日＋0.5×5 日']])
      if (bt[k]) btRow(t, label, bt[k][lag]);
  }
  const tp = $('#bt-pre'); tp.replaceChildren();
  if (bt.pre_intl) {
    const hh = tp.insertRow(); ['', '方向命中', '勝率', 't 值', '類股日數', '多空價差', 't 值'].forEach(x => hh.append(el('th', null, x)));
    for (const [k, label] of [['pre_intl', '僅國際隔夜'], ['pre_cn', '僅陸期前一日'], ['pre_combo', '國際＋陸期']]) {
      const m = bt[k]?.lag0; if (!m) continue;
      const tr = tp.insertRow(); tr.insertCell().textContent = label;
      for (const [v, f] of [[m.dir_avg, pp], [m.dir_win, wr], [m.dir_t, x => x == null ? '—' : x.toFixed(1)], [m.dir_n, x => x ?? '—'],
                            [m.spread_avg, pp], [m.spread_t, x => x == null ? '—' : x.toFixed(1)]]) {
        const c = tr.insertCell(); c.className = 'n ' + (f === pp ? cls(v) : ''); c.textContent = f(v);
      }
    }
    $('#bt-pre-note').textContent = '把美國 D 日、陸期 D 日的漲跌，對到 D 之後第一個台股交易日（08:10 時都已知）。方向命中 = 每個有分數的類股，' +
      '分數方向 × 當天超額報酬的平均；勝率 = 方向正確的比例。陸期夜盤無法從日K單獨取出，回測以「陸期前一日」代替，實際盤前版用的是夜盤漲跌。' +
      '關聯少的模型常只有單邊訊號，多空價差會是空白，以方向命中為準。';
  }
  const mo = c.momentum;
  $('#bt-note').textContent = (mo ? `「動能」列用實測＋價差權重、z 分數、不含漲跌停加成，比較 1 日、5 日、20 日累計漲跌與組合；` +
    `${mo.adopt ? '組合版通過門檻，即時分數已加入 5 日動能。' : '組合版未通過門檻（' + mo.rule + '），即時分數仍只用 1 日 z，5 日／20 日動能只顯示在商品看板。'}` : '') +
    '數字為選出類股的平均 β 調整超額報酬（類股 − β × 加權指數，已去除大盤漲跌的影響）。價差 t 值的絕對值大於 2 才算統計上顯著；' +
    '「同日」只代表陸期和台股類股會同步漲跌，「隔日」才是能拿來交易的預測力。';

  const counts = c.counts || {};
  $('#cal-summary').textContent = `關聯實測表（${c.period.start}～${c.period.end}，${c.period.tw_days} 個交易日）`;
  $('#cal-note').textContent = `依歷史資料重新檢驗每組「商品 → 類股」：` +
    Object.entries(counts).map(([k, v]) => `${k} ${v}`).join('、') +
    `。同日相關 ρ0 衡量陸期與類股（β 調整後）超額報酬是否同步；隔日 ρ1 衡量預測力。|t| ≥ 2 且前後兩段方向一致才採用，不顯著的權重歸 0。` +
    `「數據新增」是對照表外、門檻更嚴（|t| ≥ 4）的組合，只列為候選、不計分，確認產業邏輯後再加進對照表。一次檢驗上百組，約有 5% 的組合會純屬巧合而「顯著」。`;
  const f = $('#cal-filters'); f.replaceChildren();
  for (const k of ['all', '有效', '隔日有效', '方向相反', '不顯著', '數據新增', '樣本不足']) {
    if (k !== 'all' && !counts[k]) continue;
    const b = el('button', calFilter === k ? 'on' : '', k === 'all' ? '全部' : `${k} ${counts[k]}`);
    b.onclick = () => { calFilter = k; renderCalibration(CAL); };
    f.append(b);
  }
  const t2 = $('#cal'); t2.replaceChildren();
  const h2 = t2.insertRow(); ['商品', '類股', '手動', '同日 ρ0 (t)', '隔日 ρ1 (t)', 'n', '採用', '判定'].forEach(x => h2.append(el('th', null, x)));
  const rows = c.pairs.concat(c.discovered).filter(p => calFilter === 'all' || p.status === calFilter)
    .sort((a, b) => Math.abs(b.t0 || 0) - Math.abs(a.t0 || 0));
  const rt = (r, t) => r == null ? '—' : `${r > 0 ? '+' : ''}${r.toFixed(2)} (${t.toFixed(1)})`;
  rows.forEach(p => {
    const tr = t2.insertRow();
    tr.insertCell().textContent = `${p.name} ${p.code}`;
    tr.insertCell().textContent = p.sector;
    const m = tr.insertCell(); m.className = 'n'; m.textContent = p.manual_w == null ? '—' : (p.manual_w > 0 ? '+' : '') + p.manual_w;
    const a = tr.insertCell(); a.className = 'n ' + (Math.abs(p.t0 || 0) >= 2 ? cls(p.rho0) : 'flat'); a.textContent = rt(p.rho0, p.t0);
    const b = tr.insertCell(); b.className = 'n ' + (Math.abs(p.t1 || 0) >= 2 ? cls(p.rho1) : 'flat'); b.textContent = rt(p.rho1, p.t1);
    const n = tr.insertCell(); n.className = 'n'; n.textContent = p.n;
    const w = tr.insertCell(); const used = p.status === '數據新增' && !p.in_score ? 0 : p.cal_w; w.className = 'n ' + cls(used); w.textContent = used ? (used > 0 ? '+' : '') + used : '0';
    const s = tr.insertCell(); s.append(el('span', 'tag ' + (STATUS_TAG[p.status] || ''), p.status === '數據新增' && !p.in_score ? '候選（未計分）' : p.status));
    hoverTip(tr, p.why);
  });
}

function renderStocks(c) {
  if (!c.stocks) return;
  const names = Object.fromEntries(c.pairs.map(p => [p.code, p.name]));
  Object.entries(c.spreads || {}).forEach(([k, v]) => names[k] = v.name);
  const rows = [];
  Object.entries(c.stocks).forEach(([item, lst]) => lst.forEach(r => rows.push({ item, ...r })));
  const cnt = {}; rows.forEach(r => cnt[r.verdict] = (cnt[r.verdict] || 0) + 1);
  $('#stk-summary').textContent = `受惠／受壓個股實測（${rows.length} 組）`;
  $('#stk-note').textContent = '名單依產業公開資訊整理（產品＝商品漲有利，成本＝商品漲不利），再用 2 年日線檢驗：個股漲跌扣掉 β × 加權指數後，' +
    '與陸期（或價差）的相關是否符合角色。' + Object.entries(cnt).map(([k, v]) => `${k} ${v}`).join('、') +
    '。「測試期」為最後 40% 期間單獨計算：「樣本外確認」= 前 60% 與後 40% 都顯著且方向一致。「相反」代表市場實際反應與產業直覺不同。' +
    '目前多數個股的同日關聯在測試期仍成立，但隔日關聯大多在測試期消失，不宜當作隔日預測。';
  const t = $('#stk'); t.replaceChildren();
  const h = t.insertRow(); ['商品／價差', '個股', '角色', '同日 ρ0 (t)', '測試期同日', '隔日 ρ1 (t)', '測試期隔日', '判定'].forEach(x => h.append(el('th', null, x)));
  const order = { '一致': 0, '隔日一致': 1, '相反': 2, '不顯著': 3, '樣本不足': 4, '無資料': 5 };
  const rt = (r, tv) => r == null ? '—' : `${r > 0 ? '+' : ''}${r.toFixed(2)} (${tv.toFixed(1)})`;
  rows.sort((a, b) => (order[a.verdict] ?? 9) - (order[b.verdict] ?? 9) || Math.abs(b.t0 || 0) - Math.abs(a.t0 || 0)).forEach(r => {
    const tr = t.insertRow();
    tr.insertCell().textContent = names[r.item] || r.item;
    tr.insertCell().textContent = `${r.code} ${(r.why || '').split(/\s/)[0]}`;
    tr.insertCell().append(el('span', 'mk ' + (r.role === '產品' ? 'p' : 'c'), r.role));
    const a = tr.insertCell(); a.className = 'n ' + (Math.abs(r.t0 || 0) >= 2 ? cls(r.rho0) : 'flat'); a.textContent = rt(r.rho0, r.t0);
    const o = r.oos || {};
    const a2 = tr.insertCell(); a2.className = 'n ' + (Math.abs(o.t0_te || 0) >= 2 ? cls(o.rho0_te) : 'flat'); a2.textContent = rt(o.rho0_te, o.t0_te ?? 0);
    const b = tr.insertCell(); b.className = 'n ' + (Math.abs(r.t1 || 0) >= 2 ? cls(r.rho1) : 'flat'); b.textContent = rt(r.rho1, r.t1);
    const b2 = tr.insertCell(); b2.className = 'n ' + (Math.abs(o.t1_te || 0) >= 2 ? cls(o.rho1_te) : 'flat'); b2.textContent = rt(o.rho1_te, o.t1_te ?? 0);
    const vc = tr.insertCell(); vc.append(el('span', 'tag ' + ({ '一致': 'ok', '隔日一致': 'lag', '相反': 'rev' }[r.verdict] || 'no'), r.verdict));
    if (o.same_ok) vc.append(document.createTextNode(' '), el('span', 'tag ok', '樣本外確認'));
    hoverTip(tr, r.why);
  });
}

function renderIntl(c) {
  const it = c.intl; if (!it) return;
  $('#intl-note').textContent = '美國 D 日的漲跌，對到 D 之後第一個台股交易日的類股超額報酬（台股開盤前就已知，所以這裡的相關就是預測力）。' +
    '「陸期跟隨」= 同一天的陸期是否跟著前一晚的國際價格走。Yahoo 連續月未做換月修正，單日超過 ±15% 的換約跳空已剔除。';
  const t = $('#intl'); t.replaceChildren();
  const h = t.insertRow(); ['國際商品', '類股', '手動', '隔夜→台股 ρ (t)', 'n', '判定', '陸期跟隨 ρ (t)'].forEach(x => h.append(el('th', null, x)));
  const rt = (r, tv) => r == null ? '—' : `${r > 0 ? '+' : ''}${r.toFixed(2)} (${(tv ?? 0).toFixed(1)})`;
  it.pairs.slice().sort((a, b) => Math.abs(b.t0 || 0) - Math.abs(a.t0 || 0)).forEach(p => {
    const tr = t.insertRow();
    tr.insertCell().textContent = p.name;
    tr.insertCell().textContent = p.sector;
    const m = tr.insertCell(); m.className = 'n'; m.textContent = (p.manual_w > 0 ? '+' : '') + p.manual_w;
    const a = tr.insertCell(); a.className = 'n ' + (Math.abs(p.t0 || 0) >= 2 ? cls(p.rho0) : 'flat'); a.textContent = rt(p.rho0, p.t0);
    const n = tr.insertCell(); n.className = 'n'; n.textContent = p.n;
    tr.insertCell().append(el('span', 'tag ' + (STATUS_TAG[p.status] || ''), p.status));
    const f = (it.info[p.code]?.follow || []).map(x => `${BOARD[x.cn]?.name || x.cn} ${rt(x.rho, x.t)}`).join('、');
    tr.insertCell().textContent = f || '—';
    hoverTip(tr, p.why);
  });
}

function renderCoverage(c, latest) {
  const all = latest?.all_sectors; if (!all) return;
  const st = {};
  const get = s => st[s] ??= { manual: 0, same: [], next: [], intl: [], items: new Set() };
  c.pairs.forEach(p => {
    const x = get(p.sector);
    if (p.manual_w) { x.manual++; x.items.add(p.name); }
    if (p.status === '有效') x.same.push(p.name);
    if (p.status === '隔日有效') x.next.push(p.name);
  });
  (c.intl?.pairs || []).forEach(p => { if (p.cal_w) get(p.sector).intl.push(p.name); });
  const ELEC = ['半導體', '電腦週邊', '光電', '通信網路', '電子零組件', '電子通路', '資訊服務', '其他電子'];
  const rows = all.map(s => ({ s, ...get(s), elec: ELEC.includes(s) }));
  const lvl = r => r.same.length || r.next.length ? 0 : r.intl.length ? 1 : r.manual ? 2 : 3;
  rows.sort((a, b) => lvl(a) - lvl(b) || (b.same.length + b.next.length) - (a.same.length + a.next.length) || b.manual - a.manual);
  const nCov = rows.filter(r => lvl(r) <= 1).length;
  $('#cov-summary').textContent = `類股覆蓋範圍（${all.length} 類中 ${nCov} 類有實測有效的商品）`;
  const ecov = rows.filter(r => r.elec && lvl(r) <= 1).length;
  $('#cov-note').textContent = `陸期多為原物料，主要覆蓋傳產類股；電子類 ${ELEC.length} 類中只有 ${ecov} 類有實測有效的商品可對應（對照表中的電子關聯：銅→銅箔基板、銀／矽→太陽能、錫→焊料、黃金→貴金屬回收），` +
    '半導體、電腦週邊、通信網路等的股價主要由終端需求與美股科技股決定，不在本儀表板能解釋的範圍。「有效」= 同日關聯顯著且前後兩段一致；國際 = 美盤隔夜對次一台股交易日有預測力。';
  const t = $('#cov'); t.replaceChildren();
  const h = t.insertRow(); ['類股', '對照表商品', '同日有效', '隔日有效', '國際隔夜', '覆蓋'].forEach(x => h.append(el('th', null, x)));
  const LV = [['ok', '有效覆蓋'], ['lag', '僅國際'], ['no', '僅對照表（實測不顯著）'], ['few', '無對應商品']];
  rows.forEach(r => {
    const tr = t.insertRow();
    tr.insertCell().textContent = r.s + (r.elec ? '（電子）' : '');
    const m = tr.insertCell(); m.className = 'n'; m.textContent = r.manual || '—';
    if (r.items.size) hoverTip(m, [...r.items].join('、'));
    for (const arr of [r.same, r.next, r.intl]) {
      const c2 = tr.insertCell(); c2.textContent = arr.length ? arr.join('、') : '—'; c2.className = arr.length ? 'lst' : 'flat';
    }
    const [k, v] = LV[lvl(r)]; tr.insertCell().append(el('span', 'tag ' + k, v));
  });
}

let HEAT = null, heatMode = 's';
function renderHeat(h) {
  HEAT = h;
  const days = (h.days || []).filter(r => r.s && Object.keys(r.s).length);
  const box = $('#heat'); box.replaceChildren();
  if (!days.length) { box.append(el('div', 'empty', '尚無分數歷史，下次校準後產生。')); return; }
  const m = $('#heat-mode'); m.replaceChildren();
  for (const [k, label] of [['s', '陸期分數'], ['a', '實際超額報酬'], ['x', '方向是否一致']]) {
    const b = el('button', heatMode === k ? 'on' : '', label); b.onclick = () => { heatMode = k; renderHeat(HEAT); }; m.append(b);
  }
  const secs = {};
  days.forEach(r => Object.entries(r.s).forEach(([k, v]) => { secs[k] = (secs[k] || 0) + Math.abs(v); }));
  const names = Object.keys(secs).filter(k => secs[k] / days.length >= 1).sort((a, b) => secs[b] - secs[a]);
  box.style.gridTemplateColumns = `max-content repeat(${days.length}, minmax(6px, 1fr))`;
  const col = (v, full) => { const a = Math.min(1, Math.abs(v) / full); return v === 0 ? 'var(--panel2)' : `rgba(var(${v > 0 ? '--up-rgb' : '--dn-rgb'}),${(0.12 + a * 0.88).toFixed(2)})`; };
  // 日期列：每月第一個交易日標記
  box.append(el('div', 'hn', ''));
  let prevM = '';
  days.forEach(r => { const mo = r.d.slice(5, 7); box.append(el('div', 'hd', mo !== prevM ? r.d.slice(5).replace('-', '/') : '')); prevM = mo; });
  let agree = 0, tot = 0;
  names.forEach(n => {
    box.append(el('div', 'hn', n));
    days.forEach(r => {
      const sv = r.s[n] ?? 0, av = r.a?.[n];
      const cell = el('div', 'hc');
      if (heatMode === 's') cell.style.background = col(sv, 60);
      else if (heatMode === 'a') { if (av != null) cell.style.background = col(av, 2); }
      else if (av != null && Math.abs(sv) >= 10) { cell.style.background = (sv > 0) === (av > 0) ? 'var(--accent)' : 'var(--cell-off)'; }
      if (av != null && Math.abs(sv) >= 10) { tot++; if ((sv > 0) === (av > 0)) agree++; }
      cell.dataset.t = `${r.d}　${n}\n陸期分數 ${sc(sv)}${av != null ? `\n實際超額報酬 ${pp(av)}` : '\n實際：待類股指數更新'}\n${r.src === '即時' ? '當天日盤版實際分數' : '回填（全期實測權重重算）'}`;
      box.append(cell);
    });
  });
  const lg = $('#heat-legend'); lg.replaceChildren();
  const sw = c => { const e = el('span', 'sw'); e.style.background = c; return e; };
  if (heatMode === 'x') lg.append(sw('var(--accent)'), document.createTextNode('同向'), sw('var(--cell-off)'), document.createTextNode('反向'),
    document.createTextNode('　（只標 |分數| ≥ 10 的格子）'));
  else lg.append(sw(col(-1, 1)), document.createTextNode(heatMode === 's' ? '空頭 −60' : '−2%'), sw(col(-0.3, 1)), sw('var(--panel2)'), sw(col(0.3, 1)),
    sw(col(1, 1)), document.createTextNode(heatMode === 's' ? '+60 多頭' : '+2%'));
  const nLive = days.filter(r => r.src === '即時').length;
  $('#heat-note').textContent = `最近 ${days.length} 個台股交易日（${days[0].d}～${days.at(-1).d}），列出平均 |分數| ≥ 1 的 ${names.length} 個類股，依分數強度排序。` +
    `|分數| ≥ 10 的 ${tot} 格中，陸期方向與當天類股 β 調整超額報酬同向 ${tot ? Math.round(agree / tot * 100) : '—'}%。` +
    `回填天數以本次全期實測權重重算（樣本內，會高估一致率）${nLive ? `；其中 ${nLive} 天是當天日盤版的實際分數` : ''}。`;
}
document.addEventListener('mousemove', e => {
  const c = e.target.closest?.('.hc'); if (!c || !c.dataset.t) return;
  tip.textContent = c.dataset.t; tip.style.display = 'block';
  tip.style.left = Math.min(e.clientX + 12, innerWidth - 270) + 'px'; tip.style.top = (e.clientY + 14) + 'px';
});
document.addEventListener('mouseout', e => { if (e.target.closest?.('.hc')) tip.style.display = 'none'; });

// ── 商品詳細面板 ──
const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs) => { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const sg2 = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + 'σ';
const rtx = (r, t) => r == null ? '—' : `${r > 0 ? '+' : ''}${r.toFixed(2)} (${(t ?? 0).toFixed(1)})`;

function priceChart(rows, c) {
  // rows: [[日期, 收盤, 漲跌%, z]]；上半部收盤線、下半部每日 z 柱（觸及漲跌停加框）
  const W = innerWidth < 600 ? 380 : 700, H = 230, P = { l: innerWidth < 600 ? 44 : 52, r: 8, t: 8 }, PH = 150, ZT = 170, ZH = 50;
  const n = rows.length, xs = i => P.l + (W - P.l - P.r) * (n === 1 ? 0.5 : i / (n - 1));
  const cl = rows.map(r => r[1]).filter(v => v != null);
  const lo = Math.min(...cl), hi = Math.max(...cl), span = hi - lo || 1;
  const ys = v => P.t + PH * (1 - (v - lo) / span);
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': `${c.name} 近 ${n} 日收盤與每日 z 分數` });
  for (const v of [lo, lo + span / 2, hi]) {
    svg.append(sv('line', { x1: P.l, x2: W - P.r, y1: ys(v), y2: ys(v), stroke: 'var(--line)', 'stroke-dasharray': '2 3' }));
    const t = sv('text', { x: P.l - 4, y: ys(v) + 3, 'text-anchor': 'end' }); t.textContent = +v.toPrecision(5); svg.append(t);
  }
  let dpath = '';
  rows.forEach((r, i) => { if (r[1] != null) dpath += (dpath ? 'L' : 'M') + xs(i).toFixed(1) + ' ' + ys(r[1]).toFixed(1); });
  svg.append(sv('path', { d: dpath, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 1.6 }));
  // z 柱：±3σ 滿高
  const z0 = ZT + ZH / 2, bw = Math.max(1.5, (W - P.l - P.r) / n * 0.7);
  svg.append(sv('line', { x1: P.l, x2: W - P.r, y1: z0, y2: z0, stroke: 'var(--line)' }));
  for (const zz of [2, -2]) svg.append(sv('line', { x1: P.l, x2: W - P.r, y1: z0 - zz / 3 * ZH / 2, y2: z0 - zz / 3 * ZH / 2, stroke: 'var(--line)', 'stroke-dasharray': '2 3' }));
  const zl = sv('text', { x: P.l - 4, y: z0 + 3, 'text-anchor': 'end' }); zl.textContent = 'z'; svg.append(zl);
  const z2 = sv('text', { x: P.l - 4, y: z0 - ZH / 3 + 3, 'text-anchor': 'end' }); z2.textContent = '+2σ'; svg.append(z2);
  rows.forEach((r, i) => {
    if (r[3] == null) return;
    const h = Math.min(1, Math.abs(r[3]) / 3) * ZH / 2;
    const lim = (c.kind || 'commodity') === 'commodity' && r[2] != null && c.limit && Math.abs(r[2]) >= c.limit - 0.1;
    svg.append(sv('rect', { x: xs(i) - bw / 2, y: r[3] > 0 ? z0 - h : z0, width: bw, height: Math.max(h, 0.5),
      fill: r[3] > 0 ? 'var(--up)' : 'var(--dn)', opacity: Math.abs(r[3]) >= 2 ? 1 : 0.55, stroke: lim ? 'var(--accent)' : 'none' }));
  });
  // 日期標籤（首、中、尾）
  for (const i of [0, Math.floor((n - 1) / 2), n - 1]) {
    const t = sv('text', { x: xs(i), y: H - 2, 'text-anchor': i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle' });
    t.textContent = rows[i][0].slice(5).replace('-', '/'); svg.append(t);
  }
  // 十字線
  const cross = sv('line', { y1: P.t, y2: ZT + ZH, stroke: 'var(--muted)', 'stroke-width': 0.8, visibility: 'hidden' });
  const dot = sv('circle', { r: 3, fill: 'var(--accent)', visibility: 'hidden' });
  svg.append(cross, dot);
  const info = el('div', 'chart-tip', '移動滑鼠或手指看每日數字');
  const pick = ev => {
    const b = svg.getBoundingClientRect(), x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - b.left;
    const i = Math.max(0, Math.min(n - 1, Math.round(((x / b.width * W) - P.l) / (W - P.l - P.r) * (n - 1))));
    const r = rows[i];
    cross.setAttribute('x1', xs(i)); cross.setAttribute('x2', xs(i)); cross.setAttribute('visibility', 'visible');
    if (r[1] != null) { dot.setAttribute('cx', xs(i)); dot.setAttribute('cy', ys(r[1])); dot.setAttribute('visibility', 'visible'); }
    info.textContent = `${r[0]}　收盤 ${r[1] ?? '—'}　漲跌 ${pct(r[2])}　${sg2(r[3])}`;
  };
  svg.addEventListener('mousemove', pick); svg.addEventListener('touchmove', pick, { passive: true }); svg.addEventListener('touchstart', pick, { passive: true });
  const wrap = el('div'); wrap.append(svg, info);
  return wrap;
}

function vtable(head, rows) {
  const box = el('div'); box.style.overflowX = 'auto';
  const t = el('table', 'vt'); const h = t.insertRow(); head.forEach(x => h.append(el('th', null, x)));
  rows.forEach(r => { const tr = t.insertRow(); r.forEach(cell => { const td = tr.insertCell(); if (cell instanceof Node) td.append(cell); else if (cell && typeof cell === 'object') { td.textContent = cell.t; if (cell.c) td.className = cell.c; } else td.textContent = cell ?? '—'; }); });
  box.append(t); return box;
}

async function openDetail(code) {
  const c = BOARD[code]; if (!c) return;
  tip.style.display = 'none';
  const body = $('#d-body'); body.replaceChildren();
  const kind = c.kind || 'commodity';
  // 標頭
  const h = el('div', 'dh');
  h.append(el('span', 'nm', c.name), el('span', 'rank', `${c.ex}${kind === 'commodity' ? '・' + c.code : kind === 'intl' ? '・' + (c.symbol || '') : ''}`));
  const big = el('span', 'big ' + cls(c.chg), c.chg == null ? '—' : pct(c.chg) + (c.at_limit === 'up' ? ' 漲停' : c.at_limit === 'down' ? ' 跌停' : ''));
  h.append(big);
  if (c.rolled) h.append(el('span', 'tag lag', '今日換月'));
  h.querySelector('.nm').id = 'd-title';
  body.append(h);
  const ks = el('div', 'dstats');
  const kp = (k, v, sub, vc) => ks.append(kpiBox(k, v, sub, vc));
  if (kind === 'commodity') {
    kp('最新價', c.last ?? '—', `昨結算 ${c.base ?? '—'}${c.quote_time ? '・' + c.quote_time.replace(/(\d\d)(\d\d)(\d\d)/, '$1:$2') : ''}`);
    kp('今日 z', sg2(c.z), c.sigma ? `20 日波動度 ${c.sigma}%` : '', cls(c.z));
    kp('5 日動能', sg2(c.m5), LATEST?.momentum?.adopt ? '已計入分數' : '僅參考', cls(c.m5));
    kp('20 日動能', sg2(c.m20), '累計 ÷ (σ×√天數)', cls(c.m20));
    kp('漲跌停', c.limit ? `±${+c.limit.toFixed(2)}%` : '—', c.limit_src && c.limit_src !== '預設' ? c.limit_src : '預設值');
    kp('持倉量', c.oi ? Math.round(c.oi).toLocaleString() : '—', c.quote_date || '');
  } else if (kind === 'spread') {
    kp('今日變化', pct(c.chg), '佔產品價格', cls(c.chg));
    kp('今日 z', sg2(c.z), c.sigma ? `20 日波動度 ${c.sigma}%` : '', cls(c.z));
    kp('目前水準', c.level ?? '—', '價差絕對值');
  } else {
    kp('最新漲跌', pct(c.chg), c.bar_date ? `美國 ${c.bar_date}` : '', cls(c.chg));
    if (c.settled_date) kp('已收盤日線', pct(c.chg_settled), `美國 ${c.settled_date}`, cls(c.chg_settled));
    kp('z', sg2(c.z), c.sigma ? `20 日波動度 ${c.sigma}%` : '', cls(c.z));
  }
  body.append(ks);
  if (kind === 'spread' && c.legs?.length)
    body.append(el('p', 'note', '組成：' + c.legs.map(l => `${l.coef > 0 ? '+' : '−'}${Math.abs(l.coef) === 1 ? '' : Math.abs(l.coef) + '×'}${BOARD[l.code]?.name || l.code}`).join(' ').replace(/^\+/, '')));

  // 走勢圖（陸期、價差、國際都有）
  {
    body.append(el('h3', null, kind === 'spread' ? '近 120 個交易日：價差水準（上，Σ 係數 × 各腿收盤）與每日變化 z 分數（下）'
      : kind === 'intl' ? '近 120 個美國交易日：收盤（上，最後一根可能仍在交易中）與每日 z 分數（下）'
      : '近 120 個交易日：收盤（上）與每日 z 分數（下，金框 = 觸及漲跌停）'));
    const slot = el('div', 'empty', '載入中…'); body.append(slot);
    try {
      KL ??= await (await fetch('data/klines.json?t=' + Date.now())).json();
      const rows = (KL.data[code] || []).slice();
      if (kind === 'commodity' && c.quote_date && c.last != null && (!rows.length || rows.at(-1)[0] < c.quote_date)) rows.push([c.quote_date, c.last, c.chg, c.z]);
      const qd = kind === 'spread' ? BOARD[c.legs?.[0]?.code]?.quote_date : null;
      if (qd && c.level != null && (!rows.length || rows.at(-1)[0] < qd)) rows.push([qd, c.level, c.chg, c.z]);
      slot.replaceWith(rows.length > 1 ? priceChart(rows, c) : el('div', 'empty', '這個項目還沒有歷史資料（下次每日更新後出現）'));
      if (rows.length) {
        body.append(el('h3', null, '最近 10 天'));
        body.append(vtable(['日期', kind === 'spread' ? '價差' : '收盤', kind === 'spread' ? '變化' : '漲跌', 'z'], rows.slice(-10).reverse().map(r =>
          [r[0], r[1] ?? '—', { t: pct(r[2]), c: 'n ' + cls(r[2]) }, { t: sg2(r[3]), c: 'n ' + (Math.abs(r[3] ?? 0) >= 2 ? cls(r[3]) : 'flat') }])));
      }
    } catch (e) { slot.textContent = '日K資料尚未產生（下次每日更新後出現）'; }
  }

  // 類股關聯
  body.append(el('h3', null, kind === 'intl' ? '對應類股（美盤隔夜 → 次一台股交易日實測）' : '對應類股：權重與歷史實測'));
  const used = Object.fromEntries((c.links || []).map(l => [l.sector, l]));
  const manual = Object.fromEntries((c.manual || []).map(l => [l.sector, l.w]));
  const calRows = kind === 'intl' ? (CAL?.intl?.pairs || []).filter(p => p.code === code)
                                  : (CAL?.pairs || []).concat(CAL?.discovered || []).filter(p => p.code === code);
  const secs = [...new Set([...Object.keys(used), ...Object.keys(manual), ...calRows.map(p => p.sector), ...(c.ref_links || []).map(l => l.sector)])];
  const contrib = s => LATEST?.sectors?.find(x => x.name === s)?.drivers?.find(d => d.code === code)?.contrib;
  body.append(vtable(['類股', '手動', '採用', '同日 ρ (t)', '隔日 ρ (t)', '判定', '今日貢獻'], secs.map(s => {
    const p = calRows.find(x => x.sector === s) || {};
    const u = used[s]?.w, ct = contrib(s);
    const st = el('span', 'tag ' + (STATUS_TAG[p.status] || 'no'), p.status || '—');
    return [s, manual[s] != null ? (manual[s] > 0 ? '+' : '') + manual[s] : (p.manual_w != null ? (p.manual_w > 0 ? '+' : '') + p.manual_w : '—'),
      { t: u != null ? (u > 0 ? '+' : '') + u : '0', c: 'n ' + cls(u) }, { t: rtx(p.rho0, p.t0), c: 'n' }, { t: rtx(p.rho1, p.t1), c: 'n' }, st,
      { t: ct != null ? sc(ct) : '—', c: 'n ' + cls(ct) }];
  })));
  const why = [...(c.links || []), ...(c.ref_links || [])].filter((l, i, a) => l.why && a.findIndex(x => x.sector === l.sector) === i);
  if (why.length) body.append(el('p', 'note', why.map(l => `${l.sector}：${l.why}`).join('；')));
  if (!CAL) body.append(el('p', 'note', '實測數字載入中或尚未校準。'));

  // 受惠／受壓個股
  if (c.stocks?.length) {
    body.append(el('h3', null, '直接受惠／受壓個股（今日）'));
    body.append(vtable(['個股', '角色', '今日', '實測', '原因'], c.stocks.map(x => {
      const v = el('span'); if (x.verdict) v.append(el('span', 'tag ' + ({ '一致': 'ok', '隔日一致': 'lag', '相反': 'rev' }[x.verdict] || 'no'), x.verdict + (x.oos_same ? '・樣本外確認' : '')));
      return [`${x.code} ${x.name || ''}`, el('span', 'mk ' + (x.role === '產品' ? 'p' : 'c'), x.role === '產品' ? '受惠' : '受壓'),
        { t: pct(x.chg_pct), c: 'n ' + cls(x.chg_pct) }, v, x.why];
    })));
  }

  // 國際對照／對應陸期
  if (kind === 'commodity') {
    const intl = Object.values(BOARD).filter(x => x.kind === 'intl' && (x.cn || []).includes(code));
    if (intl.length) {
      body.append(el('h3', null, '國際對照'));
      body.append(vtable(['國際商品', '最新漲跌', '已收盤日線', '陸期跟隨 ρ (t)'], intl.map(x => {
        const f = (CAL?.intl?.info?.[x.code]?.follow || []).find(y => y.cn === code);
        return [x.name, { t: pct(x.chg), c: 'n ' + cls(x.chg) }, { t: x.settled_date ? `${pct(x.chg_settled)}（${x.settled_date.slice(5)}）` : '—', c: 'n ' + cls(x.chg_settled) },
          { t: f ? rtx(f.rho, f.t) : '—', c: 'n' }];
      })));
      body.append(el('p', 'note', '「陸期跟隨」= 美國前一晚漲跌與當天陸期漲跌的相關，越高代表陸期主要是跟著國際價格走。'));
    }
  } else if (kind === 'intl' && c.cn?.length) {
    body.append(el('h3', null, '對應陸期'));
    body.append(vtable(['陸期', '今日漲跌', 'z', '跟隨 ρ (t)'], c.cn.map(k => {
      const x = BOARD[k] || {}; const f = (CAL?.intl?.info?.[code]?.follow || []).find(y => y.cn === k);
      return [x.name || k, { t: pct(x.chg), c: 'n ' + cls(x.chg) }, { t: sg2(x.z), c: 'n' }, { t: f ? rtx(f.rho, f.t) : '—', c: 'n' }];
    })));
  }
  // 換月紀錄
  const rolls = (CAL?.rolls?.[code] || []).slice(-3).reverse();
  if (rolls.length) body.append(el('p', 'note', '最近換月：' + rolls.map(r => `${r.date} ${r.from}→${r.to}`).join('、')));

  $('#modal').classList.add('on'); document.body.style.overflow = 'hidden';
  $('#sheet').scrollTop = 0; $('#modal').scrollTop = 0; $('#d-close').focus();
  try { history.replaceState(null, '', '#' + code); } catch (e) {}
}
function closeDetail() {
  $('#modal').classList.remove('on'); document.body.style.overflow = '';
  try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
}
$('#d-close').onclick = closeDetail;
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeDetail(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('#modal').classList.contains('on')) closeDetail(); });

// ── 商品代碼清單：依交易市場 → 交易所分組 ──
const INE_CODES = new Set(['SC', 'LU', 'NR', 'BC', 'EC']);
const US_EX = { 'BZ=F': 'NYMEX', 'CL=F': 'NYMEX', 'HO=F': 'NYMEX', 'RB=F': 'NYMEX', 'HG=F': 'COMEX', 'ALI=F': 'COMEX',
  'GC=F': 'COMEX', 'SI=F': 'COMEX', 'ZS=F': 'CBOT', 'ZM=F': 'CBOT', 'ZL=F': 'CBOT', 'ZC=F': 'CBOT', 'CT=F': 'ICE US', 'SB=F': 'ICE US' };
const CODE_GROUPS = [
  { mkt: '中國大陸期貨', note: '人民幣計價，日盤＋夜盤；漲跌以昨結算為基準', ex: [
    ['上期所', '上海期貨交易所（SHFE）'], ['能源中心', '上海國際能源交易中心（INE）'], ['大商所', '大連商品交易所（DCE）'],
    ['鄭商所', '鄭州商品交易所（ZCE）'], ['廣期所', '廣州期貨交易所（GFEX）']] },
  { mkt: '美國期貨（國際對照）', note: '美元計價；台股開盤前已收盤，盤前版計分用', ex: [
    ['NYMEX', '紐約商業交易所（能源）'], ['COMEX', '紐約商品交易所（金屬）'], ['CBOT', '芝加哥期貨交易所（農產品）'], ['ICE US', '洲際交易所（軟性商品）']] },
  { mkt: '價差（陸期組合）', note: '由多個陸期商品組成的加工利潤，非交易所掛牌商品', ex: [['價差', '加工利潤／裂解價差']] },
];
function codeExchange(c) {
  if (c.kind === 'intl') return US_EX[c.symbol] || 'NYMEX';
  if (c.kind === 'spread') return '價差';
  return c.ex === '上期所' && INE_CODES.has(c.code) ? '能源中心' : c.ex;
}
function renderCodes(d) {
  const q = ($('#code-q').value || '').trim().toLowerCase();
  const box = $('#code-list'); box.replaceChildren();
  let shown = 0;
  for (const g of CODE_GROUPS) {
    const grid = el('div', 'ex-grid'); let n = 0;
    for (const [ex, full] of g.ex) {
      const items = d.commodities.filter(c => codeExchange(c) === ex).filter(c => {
        if (!q) return true;
        const secs = [...(c.links || []), ...(c.manual || []), ...(c.ref_links || [])].map(l => l.sector).join(' ');
        return `${c.code} ${c.name} ${c.symbol || ''} ${secs} ${ex} ${full}`.toLowerCase().includes(q);
      }).sort((a, b) => a.code.localeCompare(b.code));
      if (!items.length) continue;
      n += items.length;
      const card = el('div', 'ex-card');
      const h = el('h4', null, ex); h.append(el('span', null, `${full}・${items.length} 項`));
      card.append(h);
      const t = el('table');
      items.forEach(c => {
        const tr = t.insertRow(); tr.className = 'row';
        const cd = tr.insertCell(); cd.className = 'cd';
        const a = el('a', null, c.code); a.href = '#' + c.code; a.onclick = e => { e.preventDefault(); openDetail(c.code); }; cd.append(a);
        tr.insertCell().textContent = c.name;
        const sym = tr.insertCell(); sym.className = 'sym';
        sym.textContent = c.kind === 'intl' ? c.symbol : c.kind === 'spread' ? (c.legs || []).map(l => l.code).join('/') : `nf_${c.code}0`;
        const secs = [...new Set([...(c.links || []), ...(c.manual || []), ...(c.ref_links || [])].map(l => l.sector))];
        const sc_ = tr.insertCell(); sc_.className = 'sec'; sc_.textContent = secs.join('、') || '—';
        const p = tr.insertCell(); p.className = 'n ' + cls(c.chg); p.textContent = pct(c.chg);
        tr.onclick = () => openDetail(c.code);
        hoverTip(tr, `${c.name}（${c.code}）\n${full}\n對應類股：${secs.join('、') || '—'}`);
      });
      card.append(t); grid.append(card);
    }
    if (!n) continue;
    shown += n;
    const mh = el('div', 'mkt-h', g.mkt); mh.append(el('small', null, `${n} 項・${g.note}`));
    box.append(mh, grid);
  }
  if (!shown) box.append(el('div', 'empty', '找不到符合的商品'));
  $('#code-cnt').textContent = q ? `找到 ${shown} 項` : `共 ${shown} 項`;
}
$('#code-q').addEventListener('input', () => LATEST && renderCodes(LATEST));
window.addEventListener('hashchange', () => {
  const hc = decodeURIComponent(location.hash.slice(1));
  if (hc && BOARD[hc]) openDetail(hc);
});

async function loadVerify() {
  try {
    const t = await (await fetch('data/track.json?t=' + Date.now())).json();
    renderTrack(t);
  } catch (e) { $('#tk-kpis').replaceChildren(el('div', 'empty', '尚無追蹤資料，下次更新後開始累積。')); }
  try { renderHeat(await (await fetch('data/score_history.json?t=' + Date.now())).json()); }
  catch (e) { $('#heat').replaceChildren(el('div', 'empty', '尚無分數歷史，下次校準後產生。')); }
  try {
    const c = await (await fetch('data/calibration.json?t=' + Date.now())).json();
    renderCalibration(c);
    renderStocks(c);
    renderIntl(c);
    try { renderCoverage(c, await (await fetch('data/latest.json?t=' + Date.now())).json()); } catch (e) {}
  } catch (e) {
    $('#bt-hint').textContent = '尚未完成第一次校準（每週六自動執行）。';
    $('#cal-summary').textContent = '關聯實測表（尚未校準）';
  }
}

const ST_LABEL = { ok: '正常', warn: '注意', fail: '失敗' };
function fmtTime(t) { return t ? t.replace('T', ' ').slice(0, 16) : '—'; }

function renderSources(st) {
  const list = $('#src-list'); list.replaceChildren();
  const srcs = st.sources || [];
  const bad = srcs.filter(x => x.status === 'fail').length, warn = srcs.filter(x => x.status === 'warn').length;
  const okAll = st.ok && !bad && !warn;
  $('#src-summary').textContent = `資料來源狀態　${okAll ? '● 全部正常' : st.ok ? `● ${bad} 失敗／${warn} 注意` : '● 最近一次更新失敗'}`;
  $('#src-summary').className = okAll ? 'dn' : st.ok && !bad ? '' : 'up';
  if (!okAll) $('#src-box').open = true;

  const head = $('#src-head'); head.replaceChildren();
  const add = (k, v) => { const sp = el('span'); sp.append(document.createTextNode(k + ' ')); sp.append(el('b', null, v)); head.append(sp); };
  add('最近執行', fmtTime(st.run_at));
  add('結果', st.ok ? '成功' : '失敗');
  if (st.error) add('錯誤', st.error);
  if (st.run_url) { const a = el('a', null, '查看執行紀錄 ↗'); a.href = st.run_url; a.target = '_blank'; a.rel = 'noopener'; a.style.color = 'var(--muted)'; head.append(a); }

  srcs.forEach(x => {
    const li = el('li');
    li.append(el('span', 'sdot s-' + x.status));
    li.append(el('span', 'src-name', x.name));
    const stat = el('span', 'src-stat');
    stat.append(el('span', x.status === 'ok' ? 'dn' : x.status === 'warn' ? '' : 'up', ST_LABEL[x.status] || x.status));
    stat.append(document.createTextNode(x.rows != null ? `　${x.rows.toLocaleString()} 筆` : ''));
    li.append(stat);
    const sub = el('span', 'src-sub');
    sub.textContent = `用途：${x.used_for}　資料日：${x.data_date || '—'}　抓取：${fmtTime(x.fetched_at)}${x.note ? '　｜ ' + x.note : ''}`;
    li.append(sub);
    list.append(li);
  });
  if (!srcs.length) list.append(el('li', 'empty', '尚無來源紀錄，下次更新後會出現。'));
}

async function loadStatus() {
  try {
    const r = await fetch('data/status.json?t=' + Date.now());
    if (!r.ok) throw 0;
    const st = await r.json(); renderSources(st); return st;
  } catch (e) { return null; }
}

// ── 明暗模式：自動（跟隨系統）→ 亮 → 暗 ──
const THEMES = [['', '自動', 'auto'], ['light', '亮色', 'light'], ['dark', '暗色', 'dark']];
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  const cur = THEMES.find(x => x[0] === (t || '')) || THEMES[0];
  $('#theme').replaceChildren(el('span', 'ico ' + cur[2]), document.createTextNode(cur[1]));
  $('#theme').title = '明暗模式：' + cur[1] + (t ? '' : '（跟隨裝置設定）') + '，點一下切換';
  try { if (t) localStorage.setItem('theme', t); else localStorage.removeItem('theme'); } catch (e) {}
}
applyTheme(document.documentElement.dataset.theme || '');
$('#theme').onclick = () => {
  const i = THEMES.findIndex(x => x[0] === (document.documentElement.dataset.theme || ''));
  applyTheme(THEMES[(i + 1) % THEMES.length][0]);
};

// ── 立即更新 ──
const CFG = window.DASH_CONFIG || {};
let polling = null;
function setUpd(t) { $('#upd').textContent = t; }
async function waitForNewRun(prevRunAt) {
  const started = Date.now();
  clearInterval(polling);
  polling = setInterval(async () => {
    const mins = Math.floor((Date.now() - started) / 60000);
    let runTxt = '';
    if (CFG.triggerUrl) {
      try {
        const s = await (await fetch(CFG.triggerUrl.replace(/\/$/, '') + '/status')).json();
        if (s.run) runTxt = { queued: '排隊中', in_progress: '執行中', completed: '收尾中' }[s.run.status] || s.run.status;
      } catch (e) {}
    }
    setUpd(`更新${runTxt || '中'}… ${mins} 分`);
    const st = await loadStatus();
    if (st && st.run_at !== prevRunAt) {
      clearInterval(polling); $('#refresh').disabled = false;
      setUpd(st.ok ? '✓ 已更新 ' + fmtTime(st.run_at) : '✗ 更新失敗，見資料來源狀態');
      $('#hist').value = ''; load(''); loadVerify();
    } else if (Date.now() - started > 12 * 60000) {
      clearInterval(polling); $('#refresh').disabled = false; setUpd('等待逾時，請稍後重新整理');
    }
  }, 15000);
}

$('#refresh').onclick = async () => {
  const btn = $('#refresh'); btn.disabled = true;
  const prev = (await loadStatus())?.run_at;
  if (CFG.triggerUrl) {
    setUpd('觸發中…');
    try {
      const r = await fetch(CFG.triggerUrl.replace(/\/$/, '') + '/trigger', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || r.status);
      setUpd('已觸發，約 1–2 分鐘完成…');
      waitForNewRun(prev);
    } catch (e) { btn.disabled = false; setUpd('觸發失敗：' + e.message); }
  } else {
    window.open(CFG.actionsUrl, '_blank', 'noopener');
    setUpd('請在 GitHub 按 Run workflow，完成後自動載入…');
    waitForNewRun(prev);
  }
};

async function load(key) {
  const url = key ? `data/history/${key}.json` : 'data/latest.json';
  try {
    const r = await fetch(url + '?t=' + Date.now());
    if (!r.ok) throw new Error(r.status);
    render(await r.json());
  } catch (e) {
    $('#bull').replaceChildren(el('div', 'card empty', '尚無資料：請到 GitHub Actions 手動執行一次「Daily update」。'));
  }
}

(async () => {
  try {
    const idx = await (await fetch('data/history/index.json?t=' + Date.now())).json();
    const sel = $('#hist');
    sel.append(new Option('最新', ''));
    idx.slice().reverse().forEach(k => {
      const [d, s] = k.split('_'); sel.append(new Option(`${d} ${s === 'night' ? '夜盤' : '日盤'}`, k));
    });
    sel.onchange = () => load(sel.value);
  } catch (e) { $('#hist').style.display = 'none'; }
  await load('');
  loadStatus();
  await loadVerify();
  const hc = decodeURIComponent(location.hash.slice(1));
  if (hc && BOARD[hc]) openDetail(hc);
})();
