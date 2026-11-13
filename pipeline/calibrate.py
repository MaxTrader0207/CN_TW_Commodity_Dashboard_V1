"""實測權重校準 + 樣本外回測

1. 取近 2 年：陸期主力日報酬（新浪日K）× 證交所類股指數「超額報酬」（類股 − 加權指數）
2. 對照表中每組「商品 → 類股」計算：
     同日相關 ρ0（陸期今天 vs 類股今天）— 驗證關聯是否存在、方向是否正確
     隔日相關 ρ1（陸期今天 vs 類股下一個交易日）— 有沒有預測力
3. 依 ρ0 產生實測權重：顯著且前後兩段方向一致才採用；不顯著的降為 0；方向與手動相反的標記出來
4. 掃描對照表以外的組合，條件更嚴（多重比較）才列為「數據新增」
5. 樣本外回測：前 60% 期間校準、後 40% 期間測試，比較手動權重 vs 實測權重
   選出的多頭／空頭前 3 類股，當日與隔日的超額報酬與勝率

輸出 docs/data/calibration.json；build.py 讀取其中的 live_links 作為每日計分權重。
用法：python pipeline/calibrate.py [--years 2]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import focus as F  # noqa: E402
import history as H  # noqa: E402
import intraday as ID  # noqa: E402
from build import LIMIT_BONUS, TPE, UA, norm  # noqa: E402
from mapping import BENEFICIARIES, COMMODITIES, INTL, SPREADS  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "data" / "calibration.json"

MIN_N = 120              # 樣本少於此數：保留手動權重
T_VALID = 2.0            # 對照表內組合：|t| ≥ 2 視為顯著
T_LAG1 = 2.5             # 同日不顯著、但隔日顯著（領先關係）的門檻
T_DISCOVER = 4.0         # 對照表外組合：多重比較，門檻提高
RHO_DISCOVER = 0.15
MAX_DISCOVER_PER_COMMODITY = 3
DISCOVER_IN_SCORE = False  # 數據新增的組合預設只列為候選，不計分；確認產業邏輯後請加進 mapping.py
RHO_FULL = 0.30          # |ρ| 達此值給滿權重 1.0
TRAIN_FRAC = 0.6
TOP = 3
SCORED = ("有效", "樣本不足")   # 計入類股分數的關聯狀態


# ───────────── 統計 ─────────────
def corr(xs, ys):
    n = len(xs)
    if n < 3:
        return None, None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None, None
    r = sxy / math.sqrt(sxx * syy)
    t = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
    return r, t


def span_sum(sret, tw_dates, p, d):
    """台股在 (p, d] 期間的累計超額報酬：陸期一筆漲跌若跨越大陸長假（台股照常交易），要對應台股同一段期間"""
    i, j = bisect.bisect_right(tw_dates, p), bisect.bisect_right(tw_dates, d)
    vals = [sret[t] for t in tw_dates[i:j] if t in sret]
    return sum(vals) if vals else None


def align(cret, sret, tw_dates, lag, d0=None, d1=None):
    """回傳 (x, y, dates)。
    lag=0 同期：陸期在 d 的漲跌（前一個陸期交易日 p → d）對應台股 (p, d] 的累計超額報酬，
          平常就是同一天；大陸長假後首日則對應台股整段長假期間。
    lag=1 隔日：陸期日 d → 台股 d 之後的下一個交易日（5 天內）。"""
    xs, ys, ds = [], [], []
    cdates = sorted(cret)
    for k, d in enumerate(cdates):
        x = cret[d]
        if (d0 and d < d0) or (d1 and d > d1):
            continue
        if lag == 0:
            if k == 0:
                continue
            y = span_sum(sret, tw_dates, cdates[k - 1], d)
        else:
            i = bisect.bisect_right(tw_dates, d)
            if i >= len(tw_dates):
                continue
            t = tw_dates[i]
            if (date.fromisoformat(t) - date.fromisoformat(d)).days > 5:
                continue
            y = sret.get(t)
        if y is not None:
            xs.append(x)
            ys.append(y)
            ds.append(d)
    return xs, ys, ds


def pair_stats(cret, sret, tw_dates, d0=None, d1=None):
    x0, y0, ds = align(cret, sret, tw_dates, 0, d0, d1)
    r0, t0 = corr(x0, y0)
    x1, y1, _ = align(cret, sret, tw_dates, 1, d0, d1)
    r1, t1 = corr(x1, y1)
    stable, half_t = halves(x0, y0, r0)
    stable1, half_t1 = halves(x1, y1, r1)
    return {"n": len(x0), "rho0": r0, "t0": t0, "rho1": r1, "t1": t1,
            "half_t": half_t, "stable": stable, "half_t1": half_t1, "stable1": stable1}


def halves(xs, ys, r):
    """前後兩段方向是否一致（排除只在某一段時間成立的關係）"""
    h = len(xs) // 2
    ra, ta = corr(xs[:h], ys[:h])
    rb, tb = corr(xs[h:], ys[h:])
    ok = (ra is not None and rb is not None and r is not None
          and math.copysign(1, ra) == math.copysign(1, rb) == math.copysign(1, r))
    return ok, [ta, tb]


def weight_from_rho(r):
    return round(math.copysign(min(1.0, max(0.2, abs(r) / RHO_FULL)), r), 1)


def judge(st, manual_w):
    """回傳 (採用權重, 狀態)"""
    if st["n"] < MIN_N or st["rho0"] is None:
        return manual_w, "樣本不足"
    if abs(st["t0"]) >= T_VALID and st["stable"]:
        w = weight_from_rho(st["rho0"])
        return w, ("有效" if (w > 0) == (manual_w > 0) else "方向相反")
    if st["t1"] is not None and abs(st["t1"]) >= T_LAG1 and st["stable1"]:
        w = weight_from_rho(st["rho1"])
        return w, ("隔日有效" if (w > 0) == (manual_w > 0) else "方向相反")
    return 0.0, "不顯著"


def discover_ok(st):
    if st["n"] < MIN_N or st["rho0"] is None:
        return False
    ta, tb = st["half_t"]
    return (abs(st["t0"]) >= T_DISCOVER and abs(st["rho0"]) >= RHO_DISCOVER and st["stable"]
            and ta is not None and tb is not None and abs(ta) >= T_VALID and abs(tb) >= T_VALID)


FDR_Q = 0.05          # 多重比較：Benjamini-Hochberg 偽發現率
DECAY_DAYS = 120      # 關聯衰退：最近 120 個交易日


def bh_pass(ts, q=FDR_Q):
    """Benjamini-Hochberg：回傳通過的索引集合（p 值以常態近似 t 值）"""
    ps = [math.erfc(abs(t or 0) / math.sqrt(2)) for t in ts]
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    k = 0
    for r, i in enumerate(order, 1):
        if ps[i] <= q * r / m:
            k = r
    return {order[r] for r in range(k)}


def annotate_quality(rows, series_of, excess, tw_dates):
    """每組關聯加上：fdr_ok（同日）、fdr1_ok（隔日）＝多重比較校正後仍顯著；
    recent＝最近 DECAY_DAYS 個交易日的相關；decay＝原本有效、近期方向相反或 |t| < 1 時標「轉弱」"""
    ok0, ok1 = bh_pass([r["t0"] for r in rows]), bh_pass([r["t1"] for r in rows])
    d0 = tw_dates[-DECAY_DAYS] if len(tw_dates) > DECAY_DAYS else None
    sret_by = {}
    for i, r in enumerate(rows):
        r["fdr_ok"], r["fdr1_ok"] = i in ok0, i in ok1
        if r.get("status") not in ("有效", "數據新增") or not d0:
            continue
        sct = r["sector"]
        if sct not in sret_by:
            sret_by[sct] = {d: row[sct] for d, row in excess.items() if sct in row}
        intl = r.get("ex") == "國際"
        xs, ys, _ = align(series_of(r), sret_by[sct], tw_dates, 0, d0=d0)
        if intl:   # 國際序列已對齊到台股日：同一天比對
            sr = series_of(r)
            xs = [sr[d] for d in tw_dates[-DECAY_DAYS:] if d in sr and d in sret_by[sct]]
            ys = [sret_by[sct][d] for d in tw_dates[-DECAY_DAYS:] if d in sr and d in sret_by[sct]]
        rho, t = corr(xs, ys)
        if rho is None:
            continue
        r["recent"] = {"rho": round(rho, 3), "t": round(t, 2), "n": len(xs)}
        full = r["t0"] or 0
        r["decay"] = bool((rho > 0) != (full > 0) or abs(t) < 1)


DECAY_MULTS = {"decay_half": 0.5, "decay_zero": 0.0}


def decay_links(live, series, excess, tw_dates, d_end, mult):
    """近期轉弱的關聯降權：用 d_end 之前最近 DECAY_DAYS 個交易日檢查，方向與權重相反或 |t| < 1 → 權重 × mult。
    回傳（新的 links, 被降權的 [(code, sector)]）"""
    k = bisect.bisect_left(tw_dates, d_end)
    d0 = tw_dates[max(0, k - DECAY_DAYS)]
    out, hit = {}, []
    sret_by = {}
    for code, ls in live.items():
        out[code] = []
        for l in ls:
            sct = l["sector"]
            if sct not in sret_by:
                sret_by[sct] = {d: row[sct] for d, row in excess.items() if sct in row}
            xs, ys, _ = align(series.get(code, {}), sret_by[sct], tw_dates, 0, d0=d0, d1=d_end)
            rho, t = corr(xs, ys)
            weak = rho is not None and ((rho > 0) != (l["w"] > 0) or abs(t) < 1)
            if weak:
                hit.append((code, sct))
            out[code].append({**l, "w": l["w"] * mult} if weak else l)
    return out, hit


# ───────────── 類股迴歸式（第 3 步）─────────────
RIDGE_LAMS = (1.0, 3.0, 10.0, 30.0, 100.0)
RIDGE_VAL = 0.3        # 訓練期最後 30% 當驗證段，用來選收縮強度與決定這個類股要不要計分
RIDGE_MIN_T = 2.0      # 驗證段「預測值 vs 實際」的 t 值要達到這個門檻，類股才計分


def _solve(A, b):
    """高斯消去（部分選主元）解 A x = b"""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            return None
        M[c], M[piv] = M[piv], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for c in range(n - 1, -1, -1):
        x[c] = (M[c][n] - sum(M[c][k] * x[k] for k in range(c + 1, n))) / M[c][c]
    return x


def ridge_fit(X, y, lam):
    """最小化 Σ(y − Xβ)² + λ·Σβ²（不含截距：訊號與超額報酬都以 0 為中心）"""
    k = len(X[0])
    A = [[sum(r[i] * r[j] for r in X) + (lam if i == j else 0.0) for j in range(k)] for i in range(k)]
    return _solve(A, [sum(r[i] * yy for r, yy in zip(X, y)) for i in range(k)])


def ridge_links(items, cmd_ret, excess, tw_dates, zsig, d1=None, base=None):
    """每個類股一條收縮迴歸：類股當日超額報酬 ~ Σ β × 商品訊號。
    高度相關的商品（例如原油、燃料油、瀝青）由迴歸自己分攤權重，不再各算一票。
    base=現行的計分關聯（links_by_code）：候選商品與計分類股都跟現行相同，只換權重，公平比較；
    base=None：候選 = 對照表連到這個類股的全部商品，並用驗證段決定類股要不要計分。
    只用 d1 以前的資料；驗證段（訓練期最後 30%）選收縮強度 λ。回傳 (links_by_code, 各類股資訊)"""
    meta = {code: (limit, bonus) for _ex, code, _n, limit, _l, bonus in items}
    cand = {}
    if base is not None:
        for code, ls in base.items():
            for l in ls:
                if code in meta:
                    cand.setdefault(l["sector"], []).append((code, *meta[code], l["why"]))
    else:
        for ex, code, name, limit, links, bonus in items:
            for sector, _w, why in links:
                cand.setdefault(sector, []).append((code, limit, bonus, why))
    days = [d for d in tw_dates if (not d1 or d < d1)]
    live, info = {}, {}
    for sector, cs in cand.items():
        rows = []
        for d in days:
            y = (excess.get(d) or {}).get(sector)
            if y is None:
                continue
            x, seen = [], 0
            for code, limit, bonus, _ in cs:
                r, sd = cmd_ret.get(code, {}).get(d), zsig.get(code, {}).get(d)
                if r is None or not sd:
                    x.append(0.0)
                else:
                    x.append(H.z_signal(r, sd, limit if bonus else None, bonus))
                    seen += 1
            if seen:
                rows.append((x, y))
        if len(rows) < MIN_N:
            continue
        cut = int(len(rows) * (1 - RIDGE_VAL))
        tr, va = rows[:cut], rows[cut:]
        best = None
        for lam in RIDGE_LAMS:
            b = ridge_fit([r[0] for r in tr], [r[1] for r in tr], lam)
            if b is None:
                continue
            pred = [sum(bi * xi for bi, xi in zip(b, r[0])) for r in va]
            rho, t = corr(pred, [r[1] for r in va])
            if rho is not None and (best is None or t > best[0]):
                best = (t, lam, rho)
        if not best:
            continue
        t, lam, rho = best
        scored = base is not None or t >= RIDGE_MIN_T
        info[sector] = {"lam": lam, "val_t": round(t, 2), "val_rho": round(rho, 3), "n": len(rows), "k": len(cs),
                        "scored": scored}
        if not scored:
            continue
        b = ridge_fit([r[0] for r in rows], [r[1] for r in rows], lam)      # 選定 λ 後用全部訓練資料重估
        for (code, _l, _b, why), bi in zip(cs, b):
            if abs(bi) >= 1e-4:
                live.setdefault(code, []).append({"sector": sector, "w": round(bi, 4), "why": why, "src": "迴歸"})
    return live, info


LIGHT_Z = 1.0          # 狀態燈：|類股分數| ≥ 1 倍「該類股自己的歷史分數幅度」才亮
LIGHT_Z_STRONG = 2.0   # 強訊號


def _agg(xs):
    n = len(xs)
    return {"n": n, "hit": round(sum(x > 0 for x in xs) / n, 3) if n else None,
            "avg": round(sum(xs) / n, 3) if n else None}


def sector_records(items, cmd_ret, excess, tw_dates, links, zsig, raw=False):
    """全期每天每個類股的分數與實際超額報酬：{類股: {日期: [分數, 同期報酬, 隔日報酬]}}"""
    rec = []
    backtest(items, cmd_ret, excess, tw_dates, links, tw_dates[0], tw_dates[-1], zsig, record_sec=rec, raw=raw)
    by = {}
    for lag, d, sct, v, r in rec:
        row = by.setdefault(sct, {}).setdefault(d, [v, None, None])
        row[1 + lag] = r
    return by


def score_sd(by, d_end=None):
    """各類股分數的幅度（對 0 的均方根；只用 d_end 以前，回測不偷看）"""
    out = {}
    for sct, m in by.items():
        xs = [v[0] for d, v in m.items() if (not d_end or d < d_end) and abs(v[0]) > 1e-9]
        if len(xs) >= 60:
            out[sct] = round(math.sqrt(sum(x * x for x in xs) / len(xs)), 2)
    return out


def light_stats(by, sd, d0):
    """狀態燈的樣本外表現：d0 之後，|分數| ≥ LIGHT_Z × 幅度（亮燈）與沒亮燈的日子，
    訊號方向 × 實際超額報酬（lag0 同期、lag1 隔日）。回傳 (各類股, 合計)"""
    per, pool = {}, {"lit": {0: [], 1: []}, "unlit": {0: [], 1: []}, "strong": {0: [], 1: []}}
    for sct, m in by.items():
        if sct not in sd:
            continue
        lit = {0: [], 1: []}
        for d, (v, r0, r1) in m.items():
            if d < d0 or abs(v) < 1e-9:
                continue
            z = abs(v) / sd[sct]
            dr = 1 if v > 0 else -1
            for lag, r in ((0, r0), (1, r1)):
                if r is None:
                    continue
                (pool["lit"] if z >= LIGHT_Z else pool["unlit"])[lag].append(dr * r)
                if z >= LIGHT_Z:
                    lit[lag].append(dr * r)
                if z >= LIGHT_Z_STRONG:
                    pool["strong"][lag].append(dr * r)
        per[sct] = {"lag0": _agg(lit[0]), "lag1": _agg(lit[1])}
    return per, {k: {"lag0": _agg(v[0]), "lag1": _agg(v[1])} for k, v in pool.items()}


OPEN_CUT = "09:30"       # 頁面顯示的「開盤後」進場時間


def light_open_stats(by, sd, d0, seg_after):
    """亮燈日「進場時間 → 收盤」的方向報酬（d0 之後）：開盤前看到訊號、開盤後才進場還吃得到多少。回傳 (各類股, 合計)"""
    per, pool = {}, []
    for sct, m in by.items():
        if sct not in sd:
            continue
        xs = []
        for d, (v, _r0, _r1) in m.items():
            r = (seg_after.get(sct) or {}).get(d)
            if d >= d0 and r is not None and abs(v) >= LIGHT_Z * sd[sct]:
                xs.append((1 if v > 0 else -1) * r)
        per[sct] = _agg(xs)
        pool += xs
    return per, _aggt(pool)


def resonance_stats(by, d0):
    """共振（陸期方向與類股當天實際超額報酬同向）之後，隔日是否較容易延續：
    d0 之後每個「類股 × 日」的 訊號方向 × 隔日超額報酬，分成共振／非共振"""
    res, non = [], []
    for m in by.values():
        for d, (v, r0, r1) in m.items():
            if d < d0 or abs(v) < 1 or r0 is None or r1 is None or r0 == 0:
                continue
            dr = 1 if v > 0 else -1
            (res if (r0 > 0) == (v > 0) else non).append(dr * r1)
    return {"res": _agg(res), "non": _agg(non)}


NIGHT_MIN_DAYS = 80      # 夜盤檢驗：每個切點的測試期至少要有這麼多個台股交易日
NIGHT_FRACS = (0.6, 0.7, 0.8)


def _aggt(xs):
    a = _agg(xs)
    a["t"] = round(tstat(xs), 2) if len(xs) > 2 else None
    return a


def _dir_stats(by, d0, sectors=None, alt=None):
    """d0 之後、指定類股的「分數方向 × 當天超額報酬」：全部（|分數| ≥ 1）與亮燈（達到這段期間自己幅度的 LIGHT_Z 倍）"""
    allx, lit = [], []
    for sct, m in by.items():
        if sectors is not None and sct not in sectors:
            continue
        rows = [(v, r0, d) for d, (v, r0, _r1) in m.items() if d >= d0 and r0 is not None and abs(v) >= 1]
        if len(rows) < 20:
            continue
        sd = math.sqrt(sum(v * v for v, _, _ in rows) / len(rows))     # 亮燈門檻一律用整天版本的幅度，各段比的是同一批日子
        for v, r0, d in rows:
            if alt is not None:
                r0 = (alt.get(sct) or {}).get(d)
                if r0 is None:
                    continue
            x = (1 if v > 0 else -1) * r0
            allx.append(x)
            if abs(v) >= LIGHT_Z * sd:
                lit.append(x)
    return {"all": _aggt(allx), "lit": _aggt(lit)}


def night_check(items, intl_items, night_series, intl_series, ex, tw_dates, lv, il, zn, zpre, sp, segs=None):
    """用真正的陸期夜盤資料，檢驗盤前版對台股當天的預測力（權重只用切點 sp 之前估計的）：
      cn_only    沒有國際商品可對應的類股：只靠陸期夜盤的分數
      intl_only  有國際商品的類股：只用國際隔夜的分數
      intl_combo 有國際商品的類股：國際隔夜＋陸期夜盤合併的分數"""
    days = sorted({d for m in night_series.values() for d in m})
    if not days:
        return None
    d0 = max(sp, days[0])
    intl_secs = {l["sector"] for ls in il.values() for l in ls}
    by_n = sector_records(items, night_series, ex, tw_dates, lv, zn)
    by_i = sector_records(intl_items, intl_series, ex, tw_dates, il, zpre)
    by_c = sector_records(intl_items + items, {**night_series, **intl_series}, ex, tw_dates, {**lv, **il}, {**zn, **zpre})
    cn_secs = {s for s in by_n if s not in intl_secs}
    # 開盤後還剩多少：把當天報酬拆成「昨收 → 進場時間」與「進場時間 → 收盤」，同一批日子比較
    after = {}
    for cut, sg in (segs or {}).items():
        day = {sct: {d: g + sg["after"][sct][d] for d, g in m.items() if d in sg["after"].get(sct, {})} for sct, m in sg["gap"].items()}
        after[cut] = {k: {"day": _dir_stats(by, d0, intl_secs, day), "gap": _dir_stats(by, d0, intl_secs, sg["gap"]),
                          "after": _dir_stats(by, d0, intl_secs, sg["after"])}
                      for k, by in (("intl_only", by_i), ("intl_combo", by_c))}
    return {"from": d0, "days": sum(1 for d in days if d >= d0), "sectors_intl": sorted(intl_secs), "sectors_cn": sorted(cn_secs),
            "open": after,
            "cn_only": _dir_stats(by_n, d0, cn_secs), "intl_only": _dir_stats(by_i, d0, intl_secs),
            "intl_combo": _dir_stats(by_c, d0, intl_secs)}


def premarket_scope_decision(tests):
    """盤前版該讓哪些類股亮燈。三個切點（三組權重）都符合才切換：
      1. 只靠陸期夜盤的類股：方向報酬的 t 值不到 2（看不到預測力）→ 盤前不亮燈
      2. 有國際商品的類股：只用國際隔夜的 t 值達 2（確實有預測力）→ 盤前照常亮燈
    切換後這些類股的分數算法不變（國際隔夜＋陸期夜盤），只是其餘類股盤前不亮燈；之後證據改變會自動切回。"""
    rows = list(tests.values())
    rule = (f"每個切點的測試期至少 {NIGHT_MIN_DAYS} 天夜盤資料；3 個切點都是：只靠陸期夜盤的類股 t 值 < 2、"
            "有國際商品的類股只用國際隔夜 t 值 ≥ 2")
    if len(rows) < len(NIGHT_FRACS) or any(r["days"] < NIGHT_MIN_DAYS for r in rows):
        return {"adopt": False, "mode": "all", "rule": rule, "reason": "夜盤資料還不夠", "tests": tests}
    t = lambda x: x["t"] if x["t"] is not None else 0  # noqa: E731
    cn_weak = all(t(r["cn_only"]["all"]) < 2 and t(r["cn_only"]["lit"]) < 2 for r in rows)
    intl_ok = all(t(r["intl_only"]["all"]) >= 2 for r in rows)
    adopt = cn_weak and intl_ok
    # 亮燈類股的分數用哪一種：看「開盤後進場還剩多少」。三個切點都是只用國際的亮燈日顯著（t ≥ 2）且優於合併 → 只用國際
    def _a(r, k):
        return ((r.get("open") or {}).get(OPEN_CUT) or {}).get(k, {}).get("after", {}).get("lit") or {}
    use_intl = all((_a(r, "intl_only").get("t") or 0) >= 2 and
                   (_a(r, "intl_only").get("avg") or 0) > (_a(r, "intl_combo").get("avg") or 0) for r in rows)
    return {"adopt": adopt, "mode": "intl_only" if adopt else "all", "score": "intl" if adopt and use_intl else "combo", "open_cut": OPEN_CUT,
            "pre_sectors": sorted(set.intersection(*[set(r["sectors_intl"]) for r in rows])) if adopt else None, "days": min(r["days"] for r in rows),
            "cn_weak": cn_weak, "intl_ok": intl_ok, "rule": rule, "tests": tests}


ROBUST_FRACS = (0.5, 0.6, 0.7)   # 採用新計分方式前，用 3 個不同的訓練／測試切點檢查，全部通過才採用


def robust_checks(idx_ret, tw_dates, series, spr_ret, intl_series, zsig, zpre, cn_shift):
    """不同訓練期長度下，各計分方式在測試期的 t 值。
    同一份資料只換切點，隔日 t 值就可能從 1 跳到 2.8（哪些關聯在訓練期剛好顯著會改變），
    所以「回測過關才採用」要在每個切點都過關，避免每週校準結果來回切換。"""
    intl_items = [("國際", code, name, 1.0, links, False) for code, sym, name, cn, links in INTL if code in intl_series]
    pre_series = {**cn_shift, **intl_series}
    out = {}
    for frac in ROBUST_FRACS:
        sp = tw_dates[int(len(tw_dates) * frac)]
        ex = H.excess_returns(idx_ret, H.estimate_betas(idx_ret, d_end=sp))
        sp_items, _ = spread_items(spr_ret, d_end=sp)
        items = commodity_items() + sp_items
        _pr, _dc, lv = build_links(items, series, ex, tw_dates, d1=sp)
        t = lambda r: {"lag0": r["lag0"]["spread_t"] or 0, "lag1": r["lag1"]["spread_t"] or 0,  # noqa: E731
                       "dir0": r["lag0"]["dir_t"] or 0}
        row = {"split": sp, "base": t(backtest(items, series, ex, tw_dates, lv, sp, tw_dates[-1], zsig))}
        by = sector_records(items, series, ex, tw_dates, lv, zsig)
        row["lights"] = light_stats(by, score_sd(by, sp), sp)[1]
        row["resonance"] = resonance_stats(by, sp)
        rl, ri = ridge_links(items, series, ex, tw_dates, zsig, d1=sp, base=lv)
        row["ridge"] = t(backtest(items, series, ex, tw_dates, rl, sp, tw_dates[-1], zsig, raw=True))
        by_r = sector_records(items, series, ex, tw_dates, rl, zsig, raw=True)
        row["ridge"]["lights"] = light_stats(by_r, score_sd(by_r, sp), sp)[1]
        row["ridge"]["sectors"] = sorted(k for k, v in ri.items() if v["scored"])
        for v, m in DECAY_MULTS.items():
            lv_d, hit = decay_links(lv, series, ex, tw_dates, sp, m)
            row[v] = t(backtest(items, series, ex, tw_dates, lv_d, sp, tw_dates[-1], zsig))
            row[v]["n_links"] = len(hit)
        _, _, il = build_links(intl_items, intl_series, ex, tw_dates, d1=sp)
        row["pre_intl"] = t(backtest(intl_items, intl_series, ex, tw_dates, il, sp, tw_dates[-1], zpre))
        row["pre_combo"] = t(backtest(intl_items + items, pre_series, ex, tw_dates, {**lv, **il}, sp, tw_dates[-1], zpre))
        by_p = sector_records(intl_items + items, pre_series, ex, tw_dates, {**lv, **il}, zpre)
        row["lights_pre"] = light_stats(by_p, score_sd(by_p, sp), sp)[1]
        out[str(frac)] = row
        print(f"  穩健性 切點 {sp}：基準 同日 t {row['base']['lag0']} 隔日 t {row['base']['lag1']}"
              f"｜盤前 國際 {row['pre_intl']['lag0']} vs 合併 {row['pre_combo']['lag0']}"
              f"｜降權 半 {row['decay_half']['lag0']}/{row['decay_half']['lag1']} 零 {row['decay_zero']['lag0']}/{row['decay_zero']['lag1']}"
              f"（{row['decay_half']['n_links']} 組）")
        L0, L1, LP = row["lights"]["lit"], row["lights"]["unlit"], row["lights_pre"]
        print(f"    狀態燈 收盤版 亮燈：同日 {L0['lag0']} 隔日 {L0['lag1']}｜未亮：同日 {L1['lag0']} 隔日 {L1['lag1']}")
        print(f"    狀態燈 盤前版 亮燈：當天 {LP['lit']['lag0']}｜未亮：{LP['unlit']['lag0']}｜強：{LP['strong']['lag0']}")
        print(f"    共振後隔日：{row['resonance']['res']}｜非共振：{row['resonance']['non']}")
        R = row["ridge"]
        print(f"    迴歸式：同日 t {R['lag0']} 隔日 t {R['lag1']}（原 {row['base']['lag0']}／{row['base']['lag1']}）｜"
              f"亮燈 同日 {R['lights']['lit']['lag0']} 隔日 {R['lights']['lit']['lag1']}｜計分類股 {R['sectors']}")
    return out


def night_checks(idx_ret, tw_dates, series, spr_ret, intl_series, zpre, night_series, tw_bars=None):
    """盤前版的夜盤實測，換 3 個切點。夜盤資料只有近幾個月，切點比 ROBUST_FRACS 晚（訓練期夠長才估得出國際商品的關聯）。"""
    intl_items = [("國際", code, name, 1.0, links, False) for code, sym, name, cn, links in INTL if code in intl_series]
    zn = {c: H.rolling_sigma(v) for c, v in night_series.items()}
    out = {}
    for frac in NIGHT_FRACS:
        sp = tw_dates[int(len(tw_dates) * frac)]
        betas = H.estimate_betas(idx_ret, d_end=sp)
        ex = H.excess_returns(idx_ret, betas)
        sp_items, _ = spread_items(spr_ret, d_end=sp)
        items = commodity_items() + sp_items
        _pr, _dc, lv = build_links(items, series, ex, tw_dates, d1=sp)
        _, _, il = build_links(intl_items, intl_series, ex, tw_dates, d1=sp)
        segs = {cut: ID.tw_segments(tw_bars, betas, cut) for cut in ID.OPEN_CUTS} if tw_bars else None
        N = night_check(items, intl_items, night_series, intl_series, ex, tw_dates, lv, il, zn, zpre, sp, segs)
        if N is None:
            continue
        out[str(frac)] = N
        print(f"  夜盤實測 切點 {sp}（{N['from']} 起 {N['days']} 天）：只靠陸期夜盤的類股 {N['cn_only']['all']}｜亮燈 {N['cn_only']['lit']}")
        print(f"      有國際商品的類股 {N['sectors_intl']}：只用國際 {N['intl_only']['all']}｜亮燈 {N['intl_only']['lit']}"
              f"｜國際＋夜盤 {N['intl_combo']['all']}｜亮燈 {N['intl_combo']['lit']}")
        for cut, o in N["open"].items():
            for k, lab in (("intl_only", "只用國際"), ("intl_combo", "國際＋夜盤")):
                x = o[k]
                print(f"      開盤後 {cut} 進場・{lab}：整天 {x['day']['all']}｜到 {cut} {x['gap']['all']}｜{cut}→收盤 {x['after']['all']}"
                      f"｜亮燈 {cut}→收盤 {x['after']['lit']}")
    return out


def commodity_items():
    """(交易所, 代碼, 名稱, 訊號滿格幅度, 對照, 漲跌停加成)；價差的滿格幅度由 2σ 決定，見 spread_items()"""
    return [(ex, code, name, limit, links, True) for ex, code, name, limit, links in COMMODITIES]


def build_links(items, cmd_ret, excess, tw_dates, d0=None, d1=None):
    """依指定期間校準，回傳 (pairs, discovered, live_links)"""
    sectors = sorted({s for row in excess.values() for s in row})
    sret_by = {s: {d: row[s] for d, row in excess.items() if s in row} for s in sectors}
    pairs, discovered, live = [], [], {}
    for ex, code, name, limit, links, _ in items:
        cret = cmd_ret.get(code, {})
        mapped = set()
        live[code] = []
        for sector, w, why in links:
            mapped.add(sector)
            st = pair_stats(cret, sret_by.get(sector, {}), tw_dates, d0, d1)
            cw, status = judge(st, w)
            pairs.append({"ex": ex, "code": code, "name": name, "sector": sector, "manual_w": w,
                          "why": why, **rnd(st), "cal_w": cw, "status": status})
            # 只有方向符合產業邏輯且同日顯著（有效）的關聯才計分；「方向相反」「只有隔日顯著」不計分：
            # 沒有產業邏輯支持的翻號關聯，三個切點的回測都拖累亮燈日的命中率
            if cw and status in SCORED:
                live[code].append({"sector": sector, "w": cw, "why": why,
                                   "src": "手動" if status == "樣本不足" else "實測"})
        cands = []
        for sector in sectors:
            if sector in mapped:
                continue
            st = pair_stats(cret, sret_by[sector], tw_dates, d0, d1)
            if discover_ok(st):
                cands.append((abs(st["rho0"]), sector, st))
        for _, sector, st in sorted(cands, reverse=True)[:MAX_DISCOVER_PER_COMMODITY]:
            cw = weight_from_rho(st["rho0"])
            why = f"歷史同日相關 ρ={st['rho0']:+.2f}（數據發現，待確認產業邏輯）"
            discovered.append({"ex": ex, "code": code, "name": name, "sector": sector, "manual_w": None,
                               "why": why, **rnd(st), "cal_w": cw, "status": "數據新增",
                               "in_score": DISCOVER_IN_SCORE})
            if DISCOVER_IN_SCORE:
                live[code].append({"sector": sector, "w": cw, "why": why, "src": "新增"})
    return pairs, discovered, live


def rnd(st):
    f = lambda v, k=3: None if v is None else round(v, k)  # noqa: E731
    return {"n": st["n"], "rho0": f(st["rho0"]), "t0": f(st["t0"], 2), "rho1": f(st["rho1"]),
            "t1": f(st["t1"], 2), "stable": st["stable"], "stable1": st["stable1"]}


# ───────────── 回測 ─────────────
def daily_scores(items, cmd_ret, links_by_code, d, zsig=None, raw=False):
    """仿 build.score_sectors：回傳 {sector: score}。
    zsig=None：訊號 = 漲跌 ÷ 漲跌停幅度（舊）；zsig={code: {d: σ}}：訊號 = 漲跌 ÷ 滾動 20 日波動度，2σ 滿格（新）
    raw=True：權重是迴歸係數，分數 = Σ 係數 × 訊號 × 100，不再除以 √Σ|權重|"""
    num, den = {}, {}
    for ex, code, name, limit, _, bonus in items:
        for l in links_by_code.get(code, []):
            s = l["sector"]
            den[s] = den.get(s, 0) + abs(l["w"])
            r = cmd_ret.get(code, {}).get(d)
            if r is None:
                continue
            sd = (zsig or {}).get(code, {}).get(d) if zsig is not None else None
            if zsig is not None:
                if not sd:
                    continue
                sig = H.z_signal(r, sd, limit if bonus else None, bonus)
                num[s] = num.get(s, 0) + l["w"] * sig
                continue
            sig = max(-1.0, min(1.0, r / limit))
            if bonus and abs(r) >= limit - 0.1:
                sig *= LIMIT_BONUS
            num[s] = num.get(s, 0) + l["w"] * sig
    if raw:
        return {s: num.get(s, 0) * 100 for s in den if den[s]}
    return {s: num.get(s, 0) / norm(den[s]) * 100 for s in den if den[s]}


def backtest(items, cmd_ret, excess, tw_dates, links_by_code, d0, d1, zsig=None, record_sec=None, raw=False):
    """多空前 3 類股的回測（舊排法，用來比較各種計分方式）。
    record_sec：傳入 list 時，記錄每天每個類股的 (lag, 日期, 類股, 分數, 實際超額報酬)，給狀態燈統計用
    raw：權重是迴歸係數（見 daily_scores）"""
    all_days = sorted({d for c in cmd_ret.values() for d in c})
    days = [d for d in all_days if d0 <= d <= d1]
    prev_of = {d: all_days[k - 1] for k, d in enumerate(all_days) if k}
    res = {}
    for lag in (0, 1):
        bull, bear, spread, used, dirs = [], [], [], 0, []
        for d in days:
            if lag == 0:
                # 同期：陸期 (p, d] 對應台股同一段期間的累計超額報酬（處理大陸長假）
                p = prev_of.get(d)
                if not p:
                    continue
                i, j = bisect.bisect_right(tw_dates, p), bisect.bisect_right(tw_dates, d)
                span = [excess[t] for t in tw_dates[i:j] if t in excess]
                if not span:
                    continue
                row = {}
                for r in span:
                    for s_, v in r.items():
                        row[s_] = row.get(s_, 0) + v
            else:
                i = bisect.bisect_right(tw_dates, d)
                if i >= len(tw_dates):
                    continue
                t = tw_dates[i]
                if (date.fromisoformat(t) - date.fromisoformat(d)).days > 5:
                    continue
                row = excess.get(t)
            if not row:
                continue
            sc = {s: v for s, v in daily_scores(items, cmd_ret, links_by_code, d, zsig, raw).items() if s in row}
            if record_sec is not None:
                record_sec.extend((lag, d, s, v, row[s]) for s, v in sc.items())
            # 方向命中：每個有分數的類股，分數方向 × 實際超額報酬（適用於關聯很少、常常只有單邊的模型）
            dirs += [math.copysign(1, v) * row[s] for s, v in sc.items() if abs(v) >= 1]
            b = [s for s, v in sorted(sc.items(), key=lambda kv: -kv[1]) if v > 0][:TOP]
            e = [s for s, v in sorted(sc.items(), key=lambda kv: kv[1]) if v < 0][:TOP]
            if not b and not e:
                continue
            used += 1
            bull += [row[s] for s in b]
            bear += [row[s] for s in e]
            if b and e:
                spread.append(sum(row[s] for s in b) / len(b) - sum(row[s] for s in e) / len(e))
        avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
        res[f"lag{lag}"] = {
            "days": used,
            "bull_avg": avg(bull), "bull_win": round(sum(x > 0 for x in bull) / len(bull), 3) if bull else None,
            "bear_avg": avg(bear), "bear_win": round(sum(x < 0 for x in bear) / len(bear), 3) if bear else None,
            "spread_avg": avg(spread),
            "spread_win": round(sum(x > 0 for x in spread) / len(spread), 3) if spread else None,
            "spread_t": round(tstat(spread), 2) if len(spread) > 2 else None,
            "dir_n": len(dirs), "dir_avg": avg(dirs),
            "dir_win": round(sum(x > 0 for x in dirs) / len(dirs), 3) if dirs else None,
            "dir_t": round(tstat(dirs), 2) if len(dirs) > 2 else None,
        }
    return res


def tstat(xs):
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0
    return m / (sd / math.sqrt(n)) if sd else 0.0


def manual_links(items):
    return {code: [{"sector": s, "w": w, "why": why, "src": "手動"} for s, w, why in links]
            for _, code, _, _, links, _ in items}


# ───────────── 價差 ─────────────
spread_returns = H.spread_returns


def sigma(xs):
    xs = list(xs)
    if len(xs) < 30:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def spread_items(spr_ret, d_end=None):
    """價差訊號的滿格幅度 = 2σ（d_end 以前資料估計，回測不偷看）；不適用漲跌停加成"""
    items, sig = [], {}
    for code, name, legs, links in SPREADS:
        sd = sigma(v for d, v in spr_ret.get(code, {}).items() if not d_end or d <= d_end)
        if not sd:
            continue
        sig[code] = round(sd, 4)
        items.append(("價差", code, name, 2 * sd, links, False))
    return items, sig


# ───────────── 個股 ─────────────
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range={}&interval=1d"


def fetch_stock(session, code, rng="2y"):
    """Yahoo 日線（還原權息收盤）：{date: close}；先試上市 .TW 再試上櫃 .TWO"""
    import time as _t
    last_err = None
    for suf in (".TW", ".TWO"):
        try:
            r = session.get(YAHOO.format(code + suf, rng), timeout=25)
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                last_err = f"{suf} 無資料（HTTP {r.status_code}）"
                continue
            res = res[0]
            ts = res.get("timestamp") or []
            adj = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") \
                or res["indicators"]["quote"][0]["close"]
            out = {}
            for t, c in zip(ts, adj):
                if c:
                    out[(datetime.fromtimestamp(t, TPE)).date().isoformat()] = float(c)
            if len(out) > 50:
                return out, suf
            last_err = f"{suf} 筆數過少"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:100]
        _t.sleep(0.5)
    raise ValueError(last_err)


def stock_residuals(closes, idx_ret):
    """{date: 個股漲跌 − β × 加權指數漲跌}，β 用全期估計"""
    ds = sorted(closes)
    r = {cur: (closes[cur] / closes[prev] - 1) * 100 for prev, cur in zip(ds, ds[1:])
         if closes[prev] and cur in idx_ret and H.TAIEX in idx_ret[cur]}
    r = {d: v for d, v in r.items() if abs(v) < 11}          # 剔除除權息未還原等異常
    xs = [idx_ret[d][H.TAIEX] for d in r]
    ys = list(r.values())
    if len(xs) < 60:
        return {}, None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx = sum((a - mx) ** 2 for a in xs)
    beta = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / vx if vx else 1.0
    return {d: v - beta * idx_ret[d][H.TAIEX] for d, v in r.items()}, round(beta, 3)


def stock_verdict(st, role):
    if st["n"] < MIN_N or st["rho0"] is None:
        return "樣本不足"
    want = 1 if role == "產品" else -1
    if abs(st["t0"]) >= T_VALID:
        return "一致" if math.copysign(1, st["rho0"]) == want else "相反"
    if st["t1"] is not None and abs(st["t1"]) >= T_LAG1:
        return "隔日一致" if math.copysign(1, st["rho1"]) == want else "相反"
    return "不顯著"


# ───────────── 主程式 ─────────────
def git_checkpoint(msg):
    """CI 中途把快取推到 data 分支：長時間補抓不怕最後一步失敗而全部白費；失敗時寫成 annotation 方便除錯。"""
    import os
    if not os.environ.get("CI"):
        return True
    import datastore
    ok = datastore.save(msg)
    datastore.cleanup()
    return ok


HEAT_DAYS = 120
SCORE_HIST = OUT.parent / "score_history.json"


def write_score_history(rows):
    """回填的日子覆蓋舊回填；即時（每日 build 寫入）的日子保留即時分數，只補實際報酬"""
    old = json.loads(SCORE_HIST.read_text(encoding="utf-8")) if SCORE_HIST.exists() else {"days": []}
    by = {r["d"]: r for r in rows}
    for r in old.get("days", []):
        if r.get("src") == "即時":
            r["a"] = r.get("a") or by.get(r["d"], {}).get("a") or {}
            by[r["d"]] = r
    days = sorted(by.values(), key=lambda r: r["d"])[-(HEAT_DAYS + 60):]
    SCORE_HIST.write_text(json.dumps({"generated_at": datetime.now(TPE).isoformat(timespec="minutes"),
                                      "note": "回填＝以本次全期實測權重重算（樣本內）；即時＝當天日盤版實際分數",
                                      "days": days}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    import requests
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--offline", action="store_true",
                    help="不連網：用上次校準保存的輸入（pipeline/cache/calib_inputs.json.gz 與類股指數快取）重算，方便本機重現")
    ap.add_argument("--out", help="輸出檔（預設 docs/data/calibration.json）；離線試算時可指定別的路徑")
    a = ap.parse_args()
    global OUT, SCORE_HIST
    write_detail = (not a.offline) or bool(a.out)
    if a.out:
        OUT = Path(a.out).resolve()
        SCORE_HIST = OUT.parent / "score_history.json"
    s = requests.Session()
    s.headers.update(UA)
    today = datetime.now(TPE).date()
    start = today - timedelta(days=int(365 * a.years))
    import gzip
    INP = H.CACHE_DIR / "calib_inputs.json.gz"
    off = None
    if a.offline:
        with gzip.open(INP, "rt", encoding="utf-8") as f:
            off = json.load(f)
        data, empty = H.load_index(), H.load_empty()
        today = date.fromisoformat(max(data))
        start = today - timedelta(days=int(365 * a.years))
    else:
        data, empty = H.update_index(s, start, today, checkpoint=git_checkpoint)
        git_checkpoint(f"cache: 類股指數 {len(data)} 天")
    idx_ret = {d: v for d, v in H.index_returns(data, empty).items() if d >= start.isoformat()}
    betas = H.estimate_betas(idx_ret)
    excess = H.excess_returns(idx_ret, betas)
    tw_dates = sorted(excess)
    if len(tw_dates) < 200:
        print(f"⚠ 台股類股指數只有 {len(tw_dates)} 天，資料不足，下次執行會繼續補抓；本次不更新校準結果")
        return
    print(f"台股類股超額報酬：{len(tw_dates)} 天（{tw_dates[0]} ~ {tw_dates[-1]}）")

    cmd_ret, kline_info, klines, pairs_by, roll_log = {}, {}, {}, {}, {}
    limits = {code: limit for _, code, _, limit, _ in COMMODITIES}
    roll_cache = json.loads(H.ROLL_JSON.read_text(encoding="utf-8")) if H.ROLL_JSON.exists() else {}
    naive_ret = {}
    for ex, code, name, limit, _ in COMMODITIES:
        try:
            if off is not None:      # 離線：沿用上次保存的日K與換月修正後的（收盤, 前結算）
                rows = off["klines"][code]
                klines[code] = rows
                pairs, rolls = {d: tuple(v) for d, v in off["pairs"][code].items()}, []
                roll_cache.setdefault(code, {"contract": {}})
            else:
                rows = H.fetch_kline(s, code)
                klines[code] = [r for r in rows if r["d"] >= (start - timedelta(days=10)).isoformat()]
                # 主力換月修正：比對連續主力每天實際的合約，換約日改用新合約自己的前一日結算價
                pairs, rolls = H.roll_pairs(s, code, rows, start - timedelta(days=10), roll_cache)
            pairs_by[code] = pairs
            roll_log[code] = rolls
            r, dropped = H.pairs_returns(pairs, limit)
            cmd_ret[code] = {d: v for d, v in r.items() if d >= start.isoformat()}
            nr, nd = H.pairs_returns(H.naive_pairs(rows), limit)
            naive_ret[code] = {d: v for d, v in nr.items() if d >= start.isoformat()}
            unmatched = sum(1 for d, v in roll_cache[code]["contract"].items() if v is None and d >= start.isoformat())
            kline_info[code] = {"days": len(cmd_ret[code]), "dropped": dropped, "dropped_naive": nd,
                                "rolls": len([x for x in rolls if x["date"] >= start.isoformat()]),
                                "unmatched": unmatched, "first": rows[0]["d"] if rows else None}
        except Exception as e:  # noqa: BLE001
            kline_info[code] = {"error": str(e)[:120]}
            print(f"  ⚠ {code} 日K失敗：{e}")
        if off is None:
            H.ROLL_JSON.write_text(json.dumps(roll_cache, ensure_ascii=False), encoding="utf-8")
    if off is None:
        git_checkpoint("cache: 主力換月對照表")
    print(f"陸期日K：{sum(1 for v in kline_info.values() if 'days' in v)}/{len(COMMODITIES)} 個商品")
    n_roll = sum(v.get("rolls", 0) for v in kline_info.values())
    big = sorted((x for code, rs in roll_log.items() for x in rs if x["date"] >= start.isoformat() and x["fixed"] is not None),
                 key=lambda x: -abs(x["naive"] - x["fixed"]))
    print(f"主力換月：{n_roll} 次；未比對到合約的日數 {sum(v.get('unmatched', 0) for v in kline_info.values())}")
    for code, rs in roll_log.items():
        for x in rs:
            if x in big[:12]:
                print(f"  換月 {code} {x['date']} {x['from']}→{x['to']}：原算 {x['naive']:+.2f}% → 修正 {x['fixed']:+.2f}%")

    # 價差序列
    spr_ret = {code: {d: v for d, v in spread_returns(pairs_by, legs, limits).items() if d >= start.isoformat()}
               for code, _, legs, _ in SPREADS}
    for code, name, *_ in SPREADS:
        print(f"  價差 {name}：{len(spr_ret[code])} 天")
    series = {**cmd_ret, **spr_ret}

    # 換月修正前後，對照表組合的同日相關差異（看換月雜訊影響多大）
    _naive_series = {**naive_ret}
    _p_naive, _, _ = build_links(commodity_items(), _naive_series, excess, tw_dates)
    roll_effect = []
    for p in _p_naive:
        roll_effect.append((p["code"], p["sector"], p["rho0"], p["t0"], p["status"]))

    # 全期校準 → 每日計分使用
    sp_items, sp_sigma = spread_items(spr_ret)
    items = commodity_items() + sp_items
    pairs, discovered, live = build_links(items, series, excess, tw_dates)

    # 樣本外回測：前 60% 期間估 β、σ 與權重、後 40% 期間測試（測試期資料完全不參與）
    split = tw_dates[int(len(tw_dates) * TRAIN_FRAC)]
    betas_tr = H.estimate_betas(idx_ret, d_end=split)
    excess_bt = H.excess_returns(idx_ret, betas_tr)
    sp_items_tr, _ = spread_items(spr_ret, d_end=split)
    items_c = commodity_items()
    items_all = items_c + sp_items_tr
    _, _, live_c = build_links(items_c, series, excess_bt, tw_dates, d1=split)
    pairs_all_tr, disc_all_tr, live_all = build_links(items_all, series, excess_bt, tw_dates, d1=split)
    test = {"manual": backtest(items_c, series, excess_bt, tw_dates, manual_links(items_c), split, tw_dates[-1]),
            "calibrated": backtest(items_c, series, excess_bt, tw_dates, live_c, split, tw_dates[-1]),
            "calibrated_spreads": backtest(items_all, series, excess_bt, tw_dates, live_all, split, tw_dates[-1])}
    # 標準化方式比較：滾動 20 日波動度（z 分數，只用當天以前的資料）
    zsig = {code: H.rolling_sigma(v) for code, v in series.items()}
    test.update({
        "manual_z": backtest(items_c, series, excess_bt, tw_dates, manual_links(items_c), split, tw_dates[-1], zsig),
        "calibrated_z": backtest(items_c, series, excess_bt, tw_dates, live_c, split, tw_dates[-1], zsig),
        "calibrated_spreads_z": backtest(items_all, series, excess_bt, tw_dates, live_all, split, tw_dates[-1], zsig)})
    for k in ("manual", "calibrated", "calibrated_spreads"):
        a, b = test[k], test[k + "_z"]
        print(f"標準化比較 {k}：同日價差 {a['lag0']['spread_avg']}(t {a['lag0']['spread_t']}) → z {b['lag0']['spread_avg']}(t {b['lag0']['spread_t']})"
              f"｜隔日 {a['lag1']['spread_avg']}(t {a['lag1']['spread_t']}) → z {b['lag1']['spread_avg']}(t {b['lag1']['spread_t']})")

    # 個股層級：受惠／受壓名單實測
    stocks, stock_meta = {}, {}
    closes_cache = {}
    for code in sorted({c for v in BENEFICIARIES.values() for c, *_ in v}):
        if off is not None:
            if code in off["stock_res"]:
                closes_cache[code] = off["stock_res"][code]
                stock_meta[code] = {"beta": None, "days": len(closes_cache[code]), "suffix": "快取"}
            else:
                stock_meta[code] = {"error": "離線快取沒有這檔"}
            continue
        try:
            closes, suf = fetch_stock(s, code)
            res, beta = stock_residuals(closes, idx_ret)
            closes_cache[code] = res
            stock_meta[code] = {"beta": beta, "days": len(res), "suffix": suf}
        except Exception as e:  # noqa: BLE001
            stock_meta[code] = {"error": str(e)[:120]}
            print(f"  ⚠ 個股 {code} 日線失敗：{e}")
        import time as _t
        _t.sleep(0.4)
    tw_stock_dates = sorted({d for v in closes_cache.values() for d in v})
    for item_code, lst in BENEFICIARIES.items():
        rows = []
        for code, role, why in lst:
            res = closes_cache.get(code)
            if not res:
                rows.append({"code": code, "role": role, "why": why, "verdict": "無資料"})
                continue
            st = pair_stats(series.get(item_code, {}), res, tw_stock_dates)
            # 樣本外：前 60% 與後 40% 分開算，看關係在測試期是否還成立
            tr = pair_stats(series.get(item_code, {}), res, tw_stock_dates, d1=split)
            te = pair_stats(series.get(item_code, {}), res, tw_stock_dates, d0=split)
            same_dir = lambda a, b: a is not None and b is not None and (a > 0) == (b > 0)  # noqa: E731
            oos = {"rho0_tr": rnd(tr)["rho0"], "t0_tr": rnd(tr)["t0"], "rho0_te": rnd(te)["rho0"], "t0_te": rnd(te)["t0"],
                   "rho1_tr": rnd(tr)["rho1"], "t1_tr": rnd(tr)["t1"], "rho1_te": rnd(te)["rho1"], "t1_te": rnd(te)["t1"],
                   "same_ok": bool(tr["t0"] and abs(tr["t0"]) >= T_VALID and te["t0"] and abs(te["t0"]) >= T_VALID
                                   and same_dir(tr["rho0"], te["rho0"])),
                   "next_ok": bool(tr["t1"] and abs(tr["t1"]) >= T_VALID and te["t1"] and abs(te["t1"]) >= T_VALID
                                   and same_dir(tr["rho1"], te["rho1"]))}
            rows.append({"code": code, "role": role, "why": why, **rnd(st), "verdict": stock_verdict(st, role),
                         "oos": oos})
        stocks[item_code] = rows
    # 國際商品：美國 D 日漲跌對齊到 D 之後第一個台股交易日（台股開盤前已知 → 檢驗的是預測力）
    intl_rows, intl_info, intl_series, intl_closes = [], {}, {}, {}
    for code, sym, name, cn, links in INTL:
        try:
            closes = off["intl"][code] if off is not None else H.fetch_yahoo(s, sym)[0]
            intl_closes[code] = closes
            ret = {d: v for d, v in H.intl_returns(closes).items() if d >= start.isoformat()}
            sh = H.shift_to_tw(ret, tw_dates)
            intl_series[code] = sh
            # 陸期是否跟著前一晚的國際價格走（同一個交易日的陸期漲跌）
            follow = []
            for c in cn:
                xs = [sh[d] for d in sh if d in series.get(c, {})]
                ys = [series[c][d] for d in sh if d in series.get(c, {})]
                r, t = corr(xs, ys)
                follow.append({"cn": c, "rho": None if r is None else round(r, 3), "t": None if t is None else round(t, 2),
                               "n": len(xs)})
            intl_info[code] = {"symbol": sym, "days": len(ret), "follow": follow}
            sret_by = {sct: {d: row[sct] for d, row in excess.items() if sct in row} for sct, _, _ in links}
            for sector, w, why in links:
                st = pair_stats(sh, sret_by.get(sector, {}), tw_dates)
                cw, status = judge(st, w)
                intl_rows.append({"ex": "國際", "code": code, "name": name, "sector": sector, "manual_w": w, "why": why,
                                  **rnd(st), "cal_w": cw, "status": status})
        except Exception as e:  # noqa: BLE001
            intl_info[code] = {"symbol": sym, "error": str(e)[:120]}
            print(f"  ⚠ 國際 {sym} 失敗：{e}")
        if off is None:
            import time as _tm
            _tm.sleep(0.3)
    print(f"國際商品：{sum(1 for v in intl_info.values() if 'days' in v)}/{len(INTL)}")
    for r_ in sorted(intl_rows, key=lambda r_: -abs(r_["t0"] or 0)):
        print(f"  [國際] {r_['name']:<10}→{r_['sector']:<6} 隔夜→台股 ρ={r_['rho0']} t={r_['t0']} {r_['status']}")
    for code, v in intl_info.items():
        for f in v.get("follow", []):
            print(f"  [陸期跟隨] {code}→{f['cn']} ρ={f['rho']} t={f['t']} n={f['n']}")

    # 多重比較校正與關聯衰退（陸期、價差、國際、數據新增一起當作同一批檢定）
    qrows = pairs + discovered + intl_rows
    annotate_quality(qrows, lambda r: (intl_series if r.get("ex") == "國際" else series).get(r["code"], {}), excess, tw_dates)
    n_fdr = sum(1 for r in qrows if r.get("status") == "有效" and not r["fdr_ok"])
    n_decay = sum(1 for r in qrows if r.get("decay"))
    print(f"多重比較校正（BH q={FDR_Q}）：有效但未通過 {n_fdr} 組；近 {DECAY_DAYS} 日轉弱 {n_decay} 組")
    for r in qrows:
        if r.get("decay") or (r.get("status") == "有效" and not r["fdr_ok"]):
            print(f"  {r['name']}→{r['sector']} t0={r['t0']} FDR {'過' if r['fdr_ok'] else '未過'} 近期 {r.get('recent')}")
    # 全期計分權重：再去掉多重比較校正沒過的（可能巧合），讓「有計分」＝「有實測支持」
    bad = {(r["code"], r["sector"]) for r in qrows if r.get("status") == "有效" and not r["fdr_ok"]}
    live = {c: [l for l in ls if (c, l["sector"]) not in bad] for c, ls in live.items()}
    print(f"計分關聯：{sum(len(v) for v in live.values())} 組（陸期＋價差）")
    # 歷史分數熱力圖：最近 HEAT_DAYS 個台股交易日，用全期實測權重＋z 分數回填（樣本內，僅供觀察）
    heat_days = [d for d in tw_dates[-HEAT_DAYS:]]
    hist_rows = []
    for d in heat_days:
        sc = daily_scores(items, series, live, d, zsig)
        if not sc:
            continue
        hist_rows.append({"d": d, "src": "回填", "s": {k: round(v, 1) for k, v in sc.items()},
                          "a": {k: round(v, 3) for k, v in excess.get(d, {}).items() if k in sc}})
    write_score_history(hist_rows)
    print(f"分數熱力圖回填：{len(hist_rows)} 天")
    # ── 盤前版（08:10）：國際隔夜（＋陸期）→ 當天台股 ──
    # 所有序列都對齊到「資訊之後第一個台股交易日」，回測的同日（lag0）就是盤前預測力
    intl_items = [("國際", code, name, 1.0, links, False) for code, sym, name, cn, links in INTL if code in intl_series]
    intl_live = {}
    for r_ in intl_rows:
        if r_["cal_w"] and r_["status"] in SCORED and r_.get("fdr_ok") is not False:
            intl_live.setdefault(r_["code"], []).append(
                {"sector": r_["sector"], "w": r_["cal_w"], "why": r_["why"], "src": "實測"})
    _, _, intl_live_tr = build_links(intl_items, intl_series, excess_bt, tw_dates, d1=split)
    cn_shift = {code: H.shift_to_tw(v, tw_dates) for code, v in series.items()}
    pre_series = {**cn_shift, **intl_series}
    zpre = {code: H.rolling_sigma(v) for code, v in pre_series.items()}
    pre = {
        "pre_intl": backtest(intl_items, intl_series, excess_bt, tw_dates, intl_live_tr, split, tw_dates[-1], zpre),
        "pre_cn": backtest(items_all, cn_shift, excess_bt, tw_dates, live_all, split, tw_dates[-1], zpre),
        "pre_combo": backtest(intl_items + items_all, pre_series, excess_bt, tw_dates, {**live_all, **intl_live_tr},
                              split, tw_dates[-1], zpre),
    }
    test.update(pre)
    # 盤前版排名：只用國際隔夜 vs 國際＋陸期，回測過關才改用（以多空價差 t 值比較）
    gi = lambda r, k: (r["lag0"].get(k) or 0)  # noqa: E731
    pm_adopt = (gi(pre["pre_intl"], "spread_t") > gi(pre["pre_combo"], "spread_t") + 0.3
                and gi(pre["pre_intl"], "dir_t") >= gi(pre["pre_combo"], "dir_t") - 0.3)
    premarket_mode = {"adopt": pm_adopt, "mode": "intl" if pm_adopt else "combo",
                      "intl_t": gi(pre["pre_intl"], "spread_t"), "combo_t": gi(pre["pre_combo"], "spread_t"),
                      "rule": "只用國際的多空價差 t 值比合併版高 0.3 以上、且方向命中 t 值不低於合併版 −0.3 才採用"}
    print(f"盤前版排名改用國際隔夜：{pm_adopt}（價差 t {premarket_mode['intl_t']} vs {premarket_mode['combo_t']}）")
    # 穩健性：3 個切點都通過才採用（盤前排名、可信度調整）
    # 真正的陸期夜盤漲跌（由每日更新累積的分鐘線算出），用來檢驗盤前版；沒有資料時略過
    try:
        night_series = ID.cn_moves(ID._load(ID.CN_FILE), tw_dates)["night"]
        night_series = {c: m for c, m in night_series.items() if c in cmd_ret and len(m) >= 30}
    except Exception as e:  # noqa: BLE001
        night_series = {}
        print("⚠ 夜盤資料讀取失敗：", e)
    robust = robust_checks(idx_ret, tw_dates, series, spr_ret, intl_series, zsig, zpre, cn_shift)
    night_tests = night_checks(idx_ret, tw_dates, series, spr_ret, intl_series, zpre, night_series,
                               ID._load(ID.TW_FILE)) if night_series else {}
    premarket_scope = premarket_scope_decision(night_tests)
    print(f"盤前版亮燈範圍：{premarket_scope['mode']}（{premarket_scope.get('reason') or ''}"
          f"只靠夜盤的類股無預測力 {premarket_scope.get('cn_weak')}、國際有預測力 {premarket_scope.get('intl_ok')}、分數用 {premarket_scope.get('score')}；"
          f"盤前亮燈類股 {premarket_scope.get('pre_sectors')}）")
    pm_all = all(r_["pre_intl"]["lag0"] > r_["pre_combo"]["lag0"] + 0.3 and r_["pre_intl"]["dir0"] >= r_["pre_combo"]["dir0"] - 0.3
                 for r_ in robust.values())
    premarket_mode.update(adopt=False, passed=pm_all, mode="combo", single_split=pm_adopt,
                          rule=premarket_mode["rule"] + "；並且在 3 個不同訓練期切點都要通過",
                          note="通過時只在這裡標示；盤前狀態燈目前用國際＋陸期合併分數")
    print(f"穩健性檢查後：盤前改用國際 {pm_all}")
    # 近期轉弱的關聯降權：同日或隔日其中一個 t 值提升 0.3 以上、另一個不降超過 0.3，且 3 個切點都通過
    def _ok(r_, v):
        b, x = r_["base"], r_[v]
        return ((x["lag0"] >= b["lag0"] + 0.3 and x["lag1"] >= b["lag1"] - 0.3)
                or (x["lag1"] >= b["lag1"] + 0.3 and x["lag0"] >= b["lag0"] - 0.3))
    ok_d = [v for v in DECAY_MULTS if all(_ok(r_, v) for r_ in robust.values())]
    decay_cfg = {"adopt": bool(ok_d), "variant": ok_d[0] if ok_d else None, "days": DECAY_DAYS,
                 "rule": "同日或隔日 t 值提升 0.3 以上、另一個不降超過 0.3，且 3 個切點都通過"}
    if ok_d:
        live, hit = decay_links(live, series, excess, tw_dates, tw_dates[-1], DECAY_MULTS[ok_d[0]])
        decay_cfg["links"] = [f"{c}→{s_}" for c, s_ in hit]
    # 類股迴歸式：計分類股與候選商品都跟現行相同、只換權重；過關只做紀錄與提醒，不自動切換
    ridge_ok = all(_ok(r_, "ridge") for r_ in robust.values())
    ridge = {"adopt": False, "passed": ridge_ok, "lams": list(RIDGE_LAMS),
             "rule": "同日或隔日多空價差 t 值提升 0.3 以上、另一個不降超過 0.3，且 3 個切點都通過",
             "note": "通過時只在這裡標示，需人工確認盤前版也適用後再啟用"}
    print(f"轉弱降權採用：{decay_cfg['adopt']} {decay_cfg['variant'] or ''}；類股迴歸式通過：{ridge_ok}")
    # 狀態燈：每個類股的分數除以「自己歷史分數的幅度」，夠異常才亮燈（取代多空前 3 名）
    #   幅度（sd）用全期實測權重算，給每日計分用；表現（stats）用訓練期權重與幅度、只看測試期
    by_day_tr = sector_records(items_all, series, excess_bt, tw_dates, live_all, zsig)
    by_pre_tr = sector_records(intl_items + items_all, pre_series, excess_bt, tw_dates, {**live_all, **intl_live_tr}, zpre)
    by_day = sector_records(items, series, excess, tw_dates, live, zsig)
    by_pre = sector_records(intl_items + items, pre_series, excess, tw_dates, {**live, **intl_live}, zpre)
    per_d, pool_d = light_stats(by_day_tr, score_sd(by_day_tr, split), split)
    per_p, pool_p = light_stats(by_pre_tr, score_sd(by_pre_tr, split), split)
    # 只用國際隔夜的盤前分數（盤前亮燈類股用），另算「開盤後進場」還剩多少
    by_pi_tr = sector_records(intl_items, intl_series, excess_bt, tw_dates, intl_live_tr, zpre)
    by_pi = sector_records(intl_items, intl_series, excess, tw_dates, intl_live, zpre)
    sd_pi_tr = score_sd(by_pi_tr, split)
    per_i, pool_i = light_stats(by_pi_tr, sd_pi_tr, split)
    try:
        seg_after = ID.tw_segments(ID._load(ID.TW_FILE), betas_tr, OPEN_CUT)["after"]
    except Exception as e:  # noqa: BLE001
        seg_after = {}
        print("⚠ 類股盤中資料讀取失敗：", e)
    open_i, open_pool_i = light_open_stats(by_pi_tr, sd_pi_tr, split, seg_after)
    open_p, open_pool_p = light_open_stats(by_pre_tr, score_sd(by_pre_tr, split), split, seg_after)
    for sct, v in per_i.items():
        v["open"] = open_i.get(sct)
    for sct, v in per_p.items():
        v["open"] = open_p.get(sct)
    print(f"盤前亮燈後 {OPEN_CUT} 進場到收盤（樣本外）：只用國際 {open_pool_i}｜{open_i}；合併 {open_pool_p}")
    lit_ok = all(r_["lights"]["lit"]["lag0"]["avg"] > r_["lights"]["unlit"]["lag0"]["avg"]
                 and r_["lights_pre"]["lit"]["lag0"]["avg"] > r_["lights_pre"]["unlit"]["lag0"]["avg"] for r_ in robust.values())
    lights = {"z": LIGHT_Z, "z_strong": LIGHT_Z_STRONG, "test_from": split, "robust_ok": lit_ok,
              "day": {"sd": score_sd(by_day), "stats": per_d, "pooled": pool_d},
              "pre": {"sd": score_sd(by_pre), "stats": per_p, "pooled": pool_p, "open": open_pool_p},
              "pre_intl": {"sd": score_sd(by_pi), "stats": per_i, "pooled": pool_i, "open": open_pool_i},
              "open_cut": OPEN_CUT}
    print(f"狀態燈：亮燈日優於未亮日（3 個切點）{lit_ok}；收盤版 {pool_d['lit']}；盤前版 {pool_p['lit']['lag0']}")
    # 共振：3 個切點都是「共振後隔日」的命中率與平均報酬高於非共振，才保留這個標記
    rs = [r_["resonance"] for r_ in robust.values()]
    reso_ok = all(x["res"]["n"] and x["non"]["n"] and x["res"]["hit"] > x["non"]["hit"] and x["res"]["avg"] > x["non"]["avg"]
                  for x in rs)
    resonance = {"adopt": reso_ok, **resonance_stats(by_day_tr, split),
                 "rule": "3 個切點都是共振後隔日的方向命中率與平均報酬高於非共振"}
    print(f"共振標記保留：{reso_ok}（共振 {resonance['res']}｜非共振 {resonance['non']}）")
    for k, v in pre.items():
        x = v['lag0']
        print(f"盤前回測 {k}：方向命中 {x['dir_avg']}（勝率 {x['dir_win']}, t={x['dir_t']}, n={x['dir_n']}）｜"
              f"多頭 {x['bull_avg']} 空頭 {x['bear_avg']} 價差 {x['spread_avg']} t={x['spread_t']}")

    # 保存本次輸入（陸期日K、個股殘差報酬、國際日線），方便本機重現與除錯
    if off is None:
        inp = {"klines": klines, "stock_res": closes_cache, "intl": intl_closes,
               "pairs": {c: {d: list(v) for d, v in m.items()} for c, m in pairs_by.items()}}
        with gzip.open(INP, "wt", encoding="utf-8") as f:
            json.dump(inp, f, ensure_ascii=False)
    ok_stocks = sum(1 for v in stock_meta.values() if "days" in v)
    print(f"個股日線：{ok_stocks}/{len(stock_meta)} 檔")

    # 高信心族群：隔日關係（陸期 D 日 → 台股下一交易日）的歷史命中率，給「隔日預測卡」使用
    focus_pred = F.predictive_stats(pairs + discovered, stocks, cmd_ret,
                                    {c: H.rolling_sigma(v) for c, v in cmd_ret.items()}, excess, closes_cache, split)
    focus_pred += F.predictive_stats_intl(intl_rows, intl_series, {c: H.rolling_sigma(v) for c, v in intl_series.items()},
                                          excess, closes_cache, stocks, {code: cn for code, _s, _n, cn, _l in INTL}, split)
    for r_ in focus_pred:
        print(f"  [隔日預測] {r_['name']}→{r_['sector']} t1={r_['t1']} 命中 {r_['all']} 樣本外 {r_['test']}")
    # 盤中／夜盤資料的初步檢驗（資料由每日更新累積；不足 40 天時只記錄天數）
    try:
        intraday_study = ID.study(ID._load(ID.CN_FILE), ID._load(ID.TW_FILE), live, betas, daily_excess=excess)
        print(f"盤中資料：台股 {intraday_study['tw_days']} 天，{'初步檢驗 ' + json.dumps(intraday_study['tests'], ensure_ascii=False) if intraday_study['ready'] else '還不夠 ' + str(ID.MIN_STUDY_DAYS) + ' 天，先累積'}")
    except Exception as e:  # noqa: BLE001
        intraday_study = {"ready": False, "error": str(e)[:120]}
        print("⚠ 盤中資料檢驗失敗：", e)
    eff = {(c, sct): (r, t, st) for c, sct, r, t, st in roll_effect}
    changed = [(p["name"], p["sector"], eff[(p["code"], p["sector"])], (p["rho0"], p["t0"], p["status"]))
               for p in pairs if (p["code"], p["sector"]) in eff and eff[(p["code"], p["sector"])][2] != p["status"]]
    print(f"換月修正後判定改變的組合：{len(changed)}")
    for nm, sct, a, b in changed:
        print(f"  {nm}→{sct}：修正前 ρ0={a[0]} t={a[1]} {a[2]} → 修正後 ρ0={b[0]} t={b[1]} {b[2]}")
    counts = {}
    for p in pairs + discovered:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    out = {
        "generated_at": datetime.now(TPE).isoformat(timespec="minutes"),
        "period": {"start": tw_dates[0], "end": tw_dates[-1], "tw_days": len(tw_dates)},
        "betas": betas,
        "method": {"target": "類股指數漲跌 − β × 加權指數漲跌（β 調整超額報酬）",
                   "discover_in_score": DISCOVER_IN_SCORE,
                   "signal": "陸期主力 收盤 ÷ 前一日結算價；價差 = Δ價差 ÷ 產品前一日結算價，以 2σ 為滿格",
                   "min_n": MIN_N, "t_valid": T_VALID,
                   "t_lag1": T_LAG1, "t_discover": T_DISCOVER, "rho_full": RHO_FULL, "train_frac": TRAIN_FRAC},
        "counts": counts,
        "pairs": pairs, "discovered": discovered, "live_links": live,
        "spreads": {code: {"name": name, "legs": legs, "sigma": sp_sigma.get(code), "days": len(spr_ret[code])}
                    for code, name, legs, _ in SPREADS},
        "stocks": stocks,
        "intl": {"pairs": intl_rows, "info": intl_info, "live_links": intl_live,
                 "note": "美國 D 日漲跌對齊到 D 之後第一個台股交易日（ρ0 即預測力）；僅交叉確認，不計入類股分數"},
        "backtest": {"train": [tw_dates[0], split], "test": [split, tw_dates[-1]], **test},
        "premarket_mode": premarket_mode,
        "premarket_scope": premarket_scope,
        "robust": robust,
        "decay_weight": decay_cfg,
        "ridge": ridge,
        "lights": lights,
        "resonance": resonance,
        "intraday": intraday_study,
        "quality": {"fdr_q": FDR_Q, "decay_days": DECAY_DAYS},
        "focus": {"predictive": focus_pred, "z_min": F.Z_MIN, "t_lag1": F.T_LAG1},
        # 詳細面板只顯示最近 3 次換月；完整紀錄在 calibration_detail.json
        "rolls": {code: [x for x in rs_ if x["date"] >= start.isoformat()][-3:] for code, rs_ in roll_log.items()},
    }
    detail = {
        "generated_at": out["generated_at"],
        "note": "研究用的完整紀錄，網頁不會載入",
        "kline": kline_info, "stock_meta": stock_meta,
        "rolls": {code: [x for x in rs_ if x["date"] >= start.isoformat()] for code, rs_ in roll_log.items()},
        "roll_changed": [{"name": nm, "sector": sct, "before": list(a), "after": list(b)} for nm, sct, a, b in changed],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if write_detail:
        (OUT.parent / "calibration_detail.json").write_text(json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                                                            encoding="utf-8")

    print("組合狀態：", counts)
    for p in [p for p in pairs if p["ex"] == "價差"]:
        print(f"  [價差] {p['name']:<8}→{p['sector']:<6} 手動{p['manual_w']:+.1f} ρ0={p['rho0']} t0={p['t0']} "
              f"ρ1={p['rho1']} t1={p['t1']} 實測{p['cal_w']:+.1f} {p['status']}")
    for item_code, rows in stocks.items():
        for r in rows:
            if r.get("verdict") not in ("不顯著",):
                print(f"  [個股] {item_code}→{r['code']} {r['role']} ρ0={r.get('rho0')} t0={r.get('t0')} "
                      f"ρ1={r.get('rho1')} {r['verdict']}")
    for p in sorted(pairs, key=lambda p: -(abs(p["t0"] or 0)))[:12]:
        print(f"  {p['name']:<8}→{p['sector']:<6} 手動{p['manual_w']:+.1f} ρ0={p['rho0']} t0={p['t0']} "
              f"ρ1={p['rho1']} 實測{p['cal_w']:+.1f} {p['status']}")
    for p in discovered:
        print(f"  [新增候選] {p['name']}→{p['sector']} ρ0={p['rho0']} t0={p['t0']} w={p['cal_w']}")
    print("β：", {k: v for k, v in sorted(betas.items(), key=lambda kv: kv[1])})
    for k, v in test.items():
        print(f"回測（樣本外 {split}~）{k}：當日 {v['lag0']}")
        print(f"                     {k}：隔日 {v['lag1']}")


if __name__ == "__main__":
    main()
