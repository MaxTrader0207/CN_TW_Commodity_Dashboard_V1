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
import history as H  # noqa: E402
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
            if cw:
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
def daily_scores(items, cmd_ret, links_by_code, d, zsig=None):
    """仿 build.score_sectors：回傳 {sector: score}。
    zsig=None：訊號 = 漲跌 ÷ 漲跌停幅度（舊）；zsig={code: {d: σ}}：訊號 = 漲跌 ÷ 滾動 20 日波動度，2σ 滿格（新）"""
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
    return {s: num.get(s, 0) / norm(den[s]) * 100 for s in den if den[s]}


def backtest(items, cmd_ret, excess, tw_dates, links_by_code, d0, d1, zsig=None):
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
            sc = {s: v for s, v in daily_scores(items, cmd_ret, links_by_code, d, zsig).items() if s in row}
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
    a = ap.parse_args()
    s = requests.Session()
    s.headers.update(UA)
    today = datetime.now(TPE).date()
    start = today - timedelta(days=int(365 * a.years))

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
        H.ROLL_JSON.write_text(json.dumps(roll_cache, ensure_ascii=False), encoding="utf-8")
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
    _, _, live_all = build_links(items_all, series, excess_bt, tw_dates, d1=split)
    test = {"manual": backtest(items_c, series, excess_bt, tw_dates, manual_links(items_c), split, tw_dates[-1]),
            "calibrated": backtest(items_c, series, excess_bt, tw_dates, live_c, split, tw_dates[-1]),
            "calibrated_spreads": backtest(items_all, series, excess_bt, tw_dates, live_all, split, tw_dates[-1])}
    # 標準化方式比較：滾動 20 日波動度（z 分數，只用當天以前的資料）
    zsig = {code: H.rolling_sigma(v) for code, v in series.items()}
    test.update({
        "manual_z": backtest(items_c, series, excess_bt, tw_dates, manual_links(items_c), split, tw_dates[-1], zsig),
        "calibrated_z": backtest(items_c, series, excess_bt, tw_dates, live_c, split, tw_dates[-1], zsig),
        "calibrated_spreads_z": backtest(items_all, series, excess_bt, tw_dates, live_all, split, tw_dates[-1], zsig)})
    # 多日動能：1 日 z、5 日、20 日累計（÷ σ√n）、組合（1 日 ＋ 0.5 × 5 日），同一組樣本內權重、不加漲跌停加成，公平比較
    zs = {n: {code: H.momentum_z(series[code], zsig[code], n) for code in series} for n in (1, 5, 20)}
    combo = {code: {d: H.combo_z(z, zs[5][code].get(d)) for d, z in zs[1][code].items()} for code in series}
    unit = {code: {d: 1.0 for d in v} for code, v in combo.items()}
    items_nb = [(*it[:5], False) for it in items_all]
    mom = {f"mom_{k}": backtest(items_nb, v, excess_bt, tw_dates, live_all, split, tw_dates[-1], unit)
           for k, v in (("1d", zs[1]), ("5d", zs[5]), ("20d", zs[20]), ("combo", combo))}
    test.update(mom)
    b1, bc = mom["mom_1d"], mom["mom_combo"]
    g = lambda r, lag: r[lag]["spread_t"] or 0  # noqa: E731
    mom_adopt = g(bc, "lag1") > g(b1, "lag1") + 0.3 and g(bc, "lag0") >= g(b1, "lag0") - 0.3
    for k, v in mom.items():
        print(f"動能回測 {k}：同日價差 {v['lag0']['spread_avg']}(t {v['lag0']['spread_t']})｜隔日 {v['lag1']['spread_avg']}(t {v['lag1']['spread_t']})")
    print(f"動能組合是否採用於即時計分：{mom_adopt}")
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
    for k in ("manual", "calibrated", "calibrated_spreads"):
        a, b = test[k], test[k + "_z"]
        print(f"標準化比較 {k}：同日價差 {a['lag0']['spread_avg']}(t {a['lag0']['spread_t']}) → z {b['lag0']['spread_avg']}(t {b['lag0']['spread_t']})"
              f"｜隔日 {a['lag1']['spread_avg']}(t {a['lag1']['spread_t']}) → z {b['lag1']['spread_avg']}(t {b['lag1']['spread_t']})")

    # 個股層級：受惠／受壓名單實測
    stocks, stock_meta = {}, {}
    closes_cache = {}
    for code in sorted({c for v in BENEFICIARIES.values() for c, *_ in v}):
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
            closes, meta = H.fetch_yahoo(s, sym)
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
        import time as _tm
        _tm.sleep(0.3)
    print(f"國際商品：{sum(1 for v in intl_info.values() if 'days' in v)}/{len(INTL)}")
    for r_ in sorted(intl_rows, key=lambda r_: -abs(r_["t0"] or 0)):
        print(f"  [國際] {r_['name']:<10}→{r_['sector']:<6} 隔夜→台股 ρ={r_['rho0']} t={r_['t0']} {r_['status']}")
    for code, v in intl_info.items():
        for f in v.get("follow", []):
            print(f"  [陸期跟隨] {code}→{f['cn']} ρ={f['rho']} t={f['t']} n={f['n']}")

    # ── 盤前版（08:10）：國際隔夜（＋陸期）→ 當天台股 ──
    # 所有序列都對齊到「資訊之後第一個台股交易日」，回測的同日（lag0）就是盤前預測力
    intl_items = [("國際", code, name, 1.0, links, False) for code, sym, name, cn, links in INTL if code in intl_series]
    intl_live = {}
    for r_ in intl_rows:
        if r_["cal_w"]:
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
    for k, v in pre.items():
        x = v['lag0']
        print(f"盤前回測 {k}：方向命中 {x['dir_avg']}（勝率 {x['dir_win']}, t={x['dir_t']}, n={x['dir_n']}）｜"
              f"多頭 {x['bull_avg']} 空頭 {x['bear_avg']} 價差 {x['spread_avg']} t={x['spread_t']}")

    # 保存本次輸入（陸期日K、個股殘差報酬、國際日線），方便本機重現與除錯
    import gzip
    inp = {"klines": klines, "stock_res": closes_cache, "intl": intl_closes,
           "pairs": {c: {d: list(v) for d, v in m.items()} for c, m in pairs_by.items()}}
    with gzip.open(H.CACHE_DIR / "calib_inputs.json.gz", "wt", encoding="utf-8") as f:
        json.dump(inp, f, ensure_ascii=False)
    ok_stocks = sum(1 for v in stock_meta.values() if "days" in v)
    print(f"個股日線：{ok_stocks}/{len(stock_meta)} 檔")

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
        "stocks": stocks, "stock_meta": stock_meta,
        "intl": {"pairs": intl_rows, "info": intl_info, "live_links": intl_live,
                 "note": "美國 D 日漲跌對齊到 D 之後第一個台股交易日（ρ0 即預測力）；僅交叉確認，不計入類股分數"},
        "momentum": {"adopt": mom_adopt, "combo_w": H.MOM_COMBO_W,
                     "rule": "組合版隔日價差 t 值比 1 日版高 0.3 以上、且同日 t 值不低於 1 日版 −0.3 才採用"},
        "backtest": {"train": [tw_dates[0], split], "test": [split, tw_dates[-1]], **test},
        "kline": kline_info,
        "rolls": {code: [x for x in rs if x["date"] >= start.isoformat()] for code, rs in roll_log.items()},
        "roll_changed": [{"name": nm, "sector": sct, "before": list(a), "after": list(b)} for nm, sct, a, b in changed],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

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
