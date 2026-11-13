// 電腦版與手機版共用：首頁的狀態燈、預測卡、實盤停損提示。
// 兩個頁面各自負責版面與其他功能（看板、詳細頁、驗證），這裡只放「畫出同一份資料」的部分，避免寫兩次。
const UI = (() => {
  const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
  const cls = v => v == null ? 'flat' : v > 0 ? 'up' : v < 0 ? 'dn' : 'flat';
  const pct = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2) + '%';
  const sc = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(1);
  const tval = t => t == null ? '—' : (t > 0 ? '+' : '') + t;
  const mmdd = d => d ? d.slice(5).replace('-', '/') : '—';
  const hit = v => v == null ? '—' : Math.round(v * 100) + '%';
  const txt = s => document.createTextNode(s);
  const STATE = { 1: ['偏多', 'up'], '-1': ['偏空', 'dn'], 0: ['中性', 'flat'] };
  const GATE_LABEL = { pre: '盤前狀態燈', day_next: '收盤狀態燈（隔日）', day_same: '收盤狀態燈（同日）', cards_cn: '陸期隔日預測卡', cards_intl: '國際盤前預測卡' };

  // ── 版本定位 ──
  function headline(d) {
    const night = d.session === 'night';
    const target = d.focus?.target;
    return night
      ? { title: '今日盤前', when: `預測 ${mmdd(target)} 台股`, lead: '開盤前可以用的版本：依昨夜美國期貨收盤與陸期夜盤，看哪些類股今天比平常更偏多或偏空。' }
      : { title: '今日回顧', when: `${mmdd(d.tw_date)} 台股已收盤`, lead: '收盤後的對照：陸期今天的走勢，和台股類股實際表現是否一致。這一頁解釋今天；對明天的看法只看下方的預測卡。' };
  }

  // ── 實盤停損提示 ──
  function gateNote(g, key) {
    if (!g) return null;
    const label = GATE_LABEL[key] || key;
    const box = el('div', 'u-gate' + (g.state === 'demoted' ? ' demoted' : ''));
    const rule = `滿 ${g.need} 次後，命中低於 ${hit(g.min_hit)} 且平均為負會自動降級`;
    if (g.state === 'demoted') {
      box.append(el('b', null, `⚠ ${label}已降級為「研究中」`),
        txt(`：實盤最近 ${g.n} 次方向只對 ${hit(g.win)}、平均 ${pct(g.avg)}。請不要依這一塊做決定；之後回升會自動恢復。`));
    } else if (g.state === 'collecting') {
      box.append(el('b', null, `${label}實盤 ${g.total}/${g.need} 次`),
        txt(g.total ? `：方向對 ${hit(g.win)}、平均 ${pct(g.avg)}（${rule}）` : `：改版後才開始記錄（${rule}）`));
    } else if (g.state === 'ok') {
      box.append(el('b', null, `${label}實盤達標`), txt(`：最近 ${g.n} 次方向對 ${hit(g.win)}、平均 ${pct(g.avg)}`));
    } else {
      box.append(el('b', null, `${label}實盤`), txt(g.total ? `：${g.total} 次，方向對 ${hit(g.win)}、平均 ${pct(g.avg)}（同日只是回顧，不做停損判定）` : '：改版後才開始記錄'));
    }
    return box;
  }

  // ── 狀態燈 ──
  function meter(z) {
    const m = el('div', 'u-meter');
    const lim = 2.5, zz = Math.max(-lim, Math.min(lim, z || 0));
    for (const t of [-1, 1]) { const th = el('i', 'th'); th.style.left = (50 + t / lim * 50) + '%'; m.append(th); }
    const fill = el('i'); const w = Math.abs(zz) / lim * 50;
    fill.style.left = zz >= 0 ? '50%' : (50 - w) + '%'; fill.style.width = w + '%';
    fill.style.background = Math.abs(zz) >= 1 ? `var(--${zz > 0 ? 'up' : 'dn'})` : 'var(--muted)';
    fill.style.opacity = Math.abs(zz) >= 1 ? 1 : .55;
    m.append(fill, el('i', 'mid'));
    return m;
  }
  function evidence(s, night) {
    const st = s.light?.stats, box = el('div', 'u-ev');
    const one = (lab, m) => m?.n ? `${lab}方向對 ${hit(m.hit)}、平均 ${pct(m.avg)}（${m.n} 天）` : null;
    const parts = night ? [one('當天', st?.lag0)] : [one('同日', st?.lag0), one('隔日', st?.lag1)];
    const ok = parts.filter(Boolean);
    if (ok.length) { box.append(el('b', null, '這個類股過去亮燈時（樣本外）：'), txt(ok.join('；') + '。')); }
    else box.append(txt('這個類股的樣本外亮燈紀錄還不夠，請參考上方的整體數字。'));
    if (s.light?.weak_oos) box.append(txt(' '), el('span', 'u-pill warn', '樣本外不到五成'));
    return box;
  }
  function sensRow(x, board) {
    const r = el('div', 'u-row'), l = el('div', 'l');
    const t = el('div', 't', x.name || x.code); t.append(el('span', null, x.code));
    l.append(t, el('div', 's', `經由 ${board[x.item]?.name || x.item}・同日 t ${tval(x.t0)}・隔日 t ${tval(x.t1)}` + (x.why ? `・${x.why}` : '')));
    const tags = el('div', 'u-tags');
    if (x.oos_same) tags.append(el('span', 'u-pill ok', '樣本外確認'));
    if (x.oos_next) tags.append(el('span', 'u-pill a', '隔日仍成立'));
    if (tags.childElementCount) l.append(tags);
    const rr = el('div', 'r ' + cls(x.chg_pct), pct(x.chg_pct)); rr.append(el('small', null, '最新收盤'));
    r.append(l, rr);
    return r;
  }
  function lightCard(s, d, opts = {}) {
    const night = d.session === 'night', L = s.light || {}, board = opts.board || {};
    const [label, c] = STATE[L.state ?? 0];
    if (L.pre_off) {   // 每週校準實測：這個類股在開盤前沒有預測力 → 盤前不亮燈，只留參考分數
      const card = el('div', 'u-light dim u-off');
      card.dataset.sector = s.name;
      const h = el('div', 'u-lh');
      h.append(el('span', 'u-name', s.name), el('span', 'u-state', '盤前不亮燈'), el('span', 'u-score flat', sc(s.score)));
      card.append(h, el('div', 'u-sub', '開盤前沒有預測力（實測）：這個類股只對得到陸期商品，夜盤走勢對台股當天的方向沒有參考價值。請等收盤後看「今日回顧」。'));
      return card;
    }
    const card = el('div', `u-light s-${c}` + (L.weak_oos && L.state ? ' dim' : ''));
    card.dataset.sector = s.name;
    const h = el('div', 'u-lh');
    h.append(el('span', 'u-name', s.name), el('span', 'u-state ' + (L.state ? c : ''), label));
    if (L.strong) h.append(el('span', 'u-pill strong', '強'));
    const lv = s.confidence?.level;
    h.append(el('span', 'u-pill' + (lv === 'A' ? ' a' : ''), lv === 'A' ? '可預測' : '同步'));
    h.append(el('span', 'u-score ' + cls(s.score), sc(s.score)));
    card.append(h, meter(L.z));
    const lean = L.z == null ? '' : L.state ? '' : L.z > 0.15 ? '，略偏多' : L.z < -0.15 ? '，略偏空' : '';
    card.append(el('div', 'u-sub', L.z == null ? '還沒有足夠的歷史分數可以比較' :
      `訊號強度是平常的 ${Math.abs(L.z).toFixed(1)} 倍${L.state ? '' : `（滿 ${d.focus?.lights?.z ?? 1} 倍才亮燈${lean}）`}`));
    if (!night && s.tw_avg_chg != null) {
      const a = el('div', 'u-actual');
      a.append(txt('類股今天平均 '), el('b', cls(s.tw_avg_chg), pct(s.tw_avg_chg)), txt(`（漲 ${s.tw_up}／跌 ${s.tw_down}）`));
      if (L.state && s.tw_avg_chg) a.append(txt(' '), el('span', 'u-pill ' + ((s.tw_avg_chg > 0) === (L.state > 0) ? 'ok' : 'no'), (s.tw_avg_chg > 0) === (L.state > 0) ? '與陸期同向' : '與陸期反向'));
      card.append(a);
    }
    const drv = el('div', 'u-drv');
    (s.drivers || []).filter(x => x.contrib).slice(0, 5).forEach(x => {
      const b = el('button', 'u-chip'); b.type = 'button';
      b.append(txt(x.name + ' '), el('span', cls(x.chg), pct(x.chg)), el('small', null, sc(x.contrib)));
      if (opts.openDetail) b.onclick = () => opts.openDetail(x.code);
      drv.append(b);
    });
    if (drv.childElementCount) card.append(drv);
    card.append(evidence(s, night));
    const sens = s.sensitive || [];
    if (sens.length) {
      const det = el('details', 'u-more'); det.append(el('summary', null, `最敏感個股（${sens.length}）`));
      sens.forEach(x => det.append(sensRow(x, board)));
      card.append(det);
    }
    return card;
  }
  // 整塊：標題、定位說明、整體證據、停損提示、亮燈的在前
  function lightsBlock(box, d, opts = {}) {
    box.replaceChildren();
    const night = d.session === 'night', f = d.focus || {};
    const head = el('div', 'u-head'); head.append(el('h2', null, '類股狀態燈'), el('span', 'u-when', '只列通過實測檢驗的類股'));
    box.append(head);
    const by = Object.fromEntries((d.sectors || []).map(s => [s.name, s]));
    const list = (f.sectors || []).map(n => by[n]).filter(s => s?.light);
    if (!list.length) { box.append(el('div', 'u-empty', '目前沒有類股通過實測檢驗（下次校準後更新）。')); return { lit: 0, total: 0 }; }
    const P = f.lights?.pooled;
    if (P?.lit) {
      const p = el('p', 'u-lead');
      const key = P.lit.lag0, off = P.unlit?.lag0;
      p.append(el('b', null, `亮燈的意思：`), txt(`這個類股今天的陸期訊號，比它自己平常的幅度大（${f.lights.z} 倍以上）。樣本外（${f.lights.test_from || '—'} 起）全部類股合計，` +
        `亮燈時${night ? '當天' : '同日'}方向對 ${hit(key.hit)}、平均 ${pct(key.avg)}；沒亮時 ${hit(off?.hit)}、${pct(off?.avg)}。` +
        (night ? '' : `隔日：亮燈 ${hit(P.lit.lag1?.hit)}、${pct(P.lit.lag1?.avg)}，優勢不明顯。`)));
      box.append(p);
    }
    const PS = night && f.lights?.pre_scope;
    if (PS) {
      const r = a => !a ? '—' : a[0] === a[1] ? hit(a[0]) : `${hit(a[0])}～${hit(a[1])}`;
      const p = el('p', 'u-lead');
      p.append(el('b', null, `開盤前只有 ${(PS.pre_sectors || []).join('、')} 會亮燈：`),
        txt(`用實際的陸期夜盤資料檢驗（至少 ${PS.days} 個交易日、3 個切點），有國際商品可對應的類股，當天方向對 ${r(PS.intl_hit)}；` +
          `只靠陸期夜盤的類股 ${r(PS.cn_hit)}，跟丟銅板差不多，所以盤前不亮燈。每週重新檢驗，證據改變會自動調整。`));
      box.append(p);
    }
    const g = d.gates || {};
    for (const k of night ? ['pre'] : ['day_same', 'day_next']) { const n = gateNote(g[k], k); if (n) box.append(n); }
    const lit = list.filter(s => s.light.state);
    if (!lit.length) box.append(el('div', 'u-gate', `今天沒有類股亮燈：所有類股的訊號都在平常的幅度內，${night ? '盤前沒有特別偏多或偏空的族群。' : '陸期今天對台股類股沒有特別明顯的方向。'}`));
    const grid = el('div', 'u-grid');
    const demoted = g[night ? 'pre' : 'day_next']?.state === 'demoted' && night;
    const offs = list.filter(s => s.light.pre_off);
    list.filter(s => !s.light.pre_off).forEach(s => { const c = lightCard(s, d, opts); if (demoted) c.classList.add('dim'); grid.append(c); });
    box.append(grid);
    if (offs.length) {
      const det = el('details', 'u-others'); det.append(el('summary', null, `盤前不亮燈的類股（${offs.length}，開盤前沒有預測力，收盤後才看）`));
      const g2 = el('div', 'u-grid'); g2.style.marginTop = '10px';
      offs.forEach(s => g2.append(lightCard(s, d, opts)));
      det.append(g2); box.append(det);
    }
    if (!night && f.resonance?.on && f.resonance.res?.n) {
      box.append(el('p', 'u-note', `「與陸期同向」之後的隔日：方向延續 ${hit(f.resonance.res.hit)}（反向時 ${hit(f.resonance.non.hit)}），差距很小，只供參考。`));
    }
    const others = (d.sectors || []).filter(s => !s.light && Math.abs(s.score) >= 1);
    if (others.length) {
      const det = el('details', 'u-others'); det.append(el('summary', null, `其他類股（${others.length}，沒有實測支持，分數僅供參考）`));
      det.append(el('p', 'u-note', others.map(s => `${s.name} ${sc(s.score)}`).join('、')));
      box.append(det);
    }
    return { lit: lit.length, total: list.length - offs.length, off: offs.length };
  }

  // ── 預測卡 ──
  function predCard(cd, d, opts = {}) {
    const intl = cd.kind === 'intl', dir = cd.direction;
    const gate = (d.gates || {})['cards_' + (cd.kind || 'cn')];
    const c = el('div', 'u-pred' + (gate?.state === 'demoted' ? ' dim' : ''));
    c.dataset.kind = cd.kind || 'cn';
    const h = el('div', 'u-lh');
    h.append(el('span', 'u-name', `${intl ? '盤前預測' : '隔日預測'}：${cd.sector}`), el('span', 'u-pill a', intl ? '國際隔夜' : '陸期收盤'));
    if (cd.tier) h.append(el('span', 'u-pill' + (cd.tier === '強' ? ' strong' : ''), cd.tier + '訊號'));
    h.append(el('span', 'u-dir ' + (dir > 0 ? 'up' : dir < 0 ? 'dn' : 'flat'), dir > 0 ? '偏多' : dir < 0 ? '偏空' : '不明確'));
    c.append(h, el('div', 'u-sub', `依${intl ? '美國' : '陸期'} ${mmdd(cd.signal_date)} ${intl ? '收盤' : '日盤收盤'} → 台股 ${mmdd(cd.target)}${d.run_date === cd.target ? '（今天）' : '（下一交易日）'}`));
    const sum = el('div', 'u-items');
    cd.items.forEach(x => {
      const ch = el('span', 'u-item' + (x.status !== '有效' ? ' obs' : ''));
      ch.append(txt(x.name + ' '), el('span', cls(x.ret), pct(x.ret)), txt(' '),
        x.strong ? el('b', x.dir > 0 ? 'up' : 'dn', (x.dir > 0 ? '偏多' : '偏空') + (x.tier ? '・' + x.tier : '')) : el('span', 'flat', '不明確'));
      sum.append(ch);
    });
    const det = el('details', 'u-more');
    det.append(el('summary', null, dir ? '命中率與明細' : '今日訊號不明確（各商品 |z| < 0.5）・明細'));
    if (dir) c.append(sum); else det.append(sum);
    cd.items.forEach(x => {
      const r = el('div', 'u-row'), l = el('div', 'l');
      const t = el('div', 't', x.name); t.append(el('span', null, `${pct(x.ret)}${x.z == null ? '' : `（${x.z > 0 ? '+' : ''}${x.z}σ）`}`));
      l.append(t, el('div', 's', (x.tier_n ? `${x.tier}訊號歷史：方向對 ${hit(x.tier_hit)}、平均 ${pct(x.tier_avg)}（${x.tier_n} 天）・` : '') +
        `全部 ${hit(x.hit)}（${x.n} 次）・樣本外 ${hit(x.hit_test)}` + (x.night != null ? `・夜盤目前 ${pct(x.night)}（未回測，僅參考）` : '')));
      if (x.status !== '有效') { const tg = el('div', 'u-tags'); tg.append(el('span', 'u-pill no', '觀察（不影響方向）')); l.append(tg); }
      r.append(l, el('div', 'r', x.strong ? (x.dir > 0 ? '偏多' : '偏空') : '—'));
      det.append(r);
    });
    if (cd.stocks?.length) {
      det.append(el('div', 'u-sub', `對應個股（${intl ? '與商品同日關係顯著' : '隔日關係顯著'}）`));
      cd.stocks.forEach(x => {
        const r = el('div', 'u-row'), l = el('div', 'l');
        const t = el('div', 't', x.name || x.code); t.append(el('span', null, x.code));
        l.append(t, el('div', 's', `經由 ${x.via}・${intl ? '同日' : '隔日'} t ${tval(x.t1)}・方向對 ${hit(x.hit)}／樣本外 ${hit(x.hit_test)}`));
        const rr = el('div', 'r ' + cls(x.last_chg), pct(x.last_chg)); rr.append(el('small', null, '最新收盤'));
        r.append(l, rr); det.append(r);
      });
    }
    det.append(el('p', 'u-note', '強度：弱 |z| 0.5～1、中 1～2、強 ≥2。命中率多在 52～60%，只比隨機略好，請當成「機率略偏一邊」的參考。樣本外＝最近 40% 期間。'));
    c.append(det);
    const gn = gateNote(gate, 'cards_' + (cd.kind || 'cn')); if (gn) c.append(gn);
    return c;
  }
  // 版本標題（今日盤前／今日回顧）
  function headBlock(box, d) {
    box.replaceChildren();
    const hl = headline(d);
    const head = el('div', 'u-head u-top'); head.append(el('h2', null, hl.title), el('span', 'u-when', hl.when));
    box.append(head, el('p', 'u-lead', hl.lead));
  }
  function predBlock(box, d, opts = {}) {
    box.replaceChildren();
    const cards = d.focus?.cards || [];
    if (!cards.length) return 0;
    const night = d.session === 'night';
    const head = el('div', 'u-head'); head.append(el('h2', null, night ? '盤前預測卡' : '明日預測卡'),
      el('span', 'u-when', '只用通過檢驗的預測關係'));
    box.append(head);
    const grid = el('div', 'u-grid');
    cards.forEach(cd => grid.append(predCard(cd, d, opts)));
    box.append(grid);
    return cards.length;
  }

  return { el, cls, pct, sc, tval, mmdd, hit, headline, headBlock, gateNote, lightCard, lightsBlock, predCard, predBlock, GATE_LABEL };
})();
