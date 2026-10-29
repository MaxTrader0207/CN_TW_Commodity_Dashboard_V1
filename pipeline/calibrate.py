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
from mapping import COMMODITIES  # noqa: E402

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


def align(cret, sret, tw_dates, lag, d0=None, d1=None):
    """回傳 (x, y, dates)。lag=0 同日；lag=1 陸期日 d → 台股 d 之後的下一個交易日（5 天內）。"""
    xs, ys, ds = [], [], []
    for d, x in cret.items():
        if (d0 and d < d0) or (d1 and d > d1):
            continue
        if lag == 0:
            t = d
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


def build_links(cmd_ret, excess, tw_dates, d0=None, d1=None):
    """依指定期間校準，回傳 (pairs, discovered, live_links)"""
    sectors = sorted({s for row in excess.values() for s in row})
    sret_by = {s: {d: row[s] for d, row in excess.items() if s in row} for s in sectors}
    pairs, discovered, live = [], [], {}
    for ex, code, name, limit, links in COMMODITIES:
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
def daily_scores(cmd_ret, links_by_code, d):
    """仿 build.score_sectors：回傳 {sector: score}"""
    num, den = {}, {}
    for ex, code, name, limit, _ in COMMODITIES:
        for l in links_by_code.get(code, []):
            s = l["sector"]
            den[s] = den.get(s, 0) + abs(l["w"])
            r = cmd_ret.get(code, {}).get(d)
            if r is None:
                continue
            sig = max(-1.0, min(1.0, r / limit))
            if abs(r) >= limit - 0.1:
                sig *= LIMIT_BONUS
            num[s] = num.get(s, 0) + l["w"] * sig
    return {s: num.get(s, 0) / norm(den[s]) * 100 for s in den if den[s]}


def backtest(cmd_ret, excess, tw_dates, links_by_code, d0, d1):
    days = sorted({d for c in cmd_ret.values() for d in c if d0 <= d <= d1})
    res = {}
    for lag in (0, 1):
        bull, bear, spread, used = [], [], [], 0
        for d in days:
            if lag == 0:
                t = d
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
            sc = {s: v for s, v in daily_scores(cmd_ret, links_by_code, d).items() if s in row}
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
        }
    return res


def tstat(xs):
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0
    return m / (sd / math.sqrt(n)) if sd else 0.0


def manual_links():
    return {code: [{"sector": s, "w": w, "why": why, "src": "手動"} for s, w, why in links]
            for _, code, _, _, links in COMMODITIES}


# ───────────── 主程式 ─────────────
def git_checkpoint(msg):
    """CI 中途把快取推回 repo：長時間補抓不怕最後一步失敗而全部白費；失敗時寫成 annotation 方便除錯。"""
    import os
    import subprocess
    if not os.environ.get("CI"):
        return True

    def sh(*cmd):
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr).strip()

    sh("git", "add", "-A", "pipeline/cache")
    code, out = sh("git", "diff", "--cached", "--quiet")
    if code == 0:
        return True
    code, out = sh("git", "commit", "-m", msg)
    if code:
        print(f"::error title=checkpoint commit::{out[-300:]}")
        return False
    for i in range(4):
        c1, o1 = sh("git", "pull", "--rebase", "-X", "theirs", "origin", "main")
        c2, o2 = sh("git", "push") if c1 == 0 else (1, "")
        if c1 == 0 and c2 == 0:
            print(f"  ✓ 已推回：{msg}")
            return True
        sh("git", "rebase", "--abort")
        detail = (o1 if c1 else o2).replace("\n", " ")[-400:]
        print(f"  ⚠ 推回失敗（{i + 1}）：{detail}")
        if i == 3:
            print(f"::error title=checkpoint push::{detail}")
        import time
        time.sleep(5 * (i + 1))
    return False



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

    cmd_ret, kline_info = {}, {}
    for ex, code, name, limit, _ in COMMODITIES:
        try:
            rows = H.fetch_kline(s, code)
            r, dropped = H.kline_returns(rows, limit)
            cmd_ret[code] = {d: v for d, v in r.items() if d >= start.isoformat()}
            kline_info[code] = {"days": len(cmd_ret[code]), "dropped_roll": dropped,
                                "first": rows[0]["d"] if rows else None}
        except Exception as e:  # noqa: BLE001
            kline_info[code] = {"error": str(e)[:120]}
            print(f"  ⚠ {code} 日K失敗：{e}")
    print(f"陸期日K：{sum(1 for v in kline_info.values() if 'days' in v)}/{len(COMMODITIES)} 個商品")

    # 全期校準 → 每日計分使用
    pairs, discovered, live = build_links(cmd_ret, excess, tw_dates)

    # 樣本外回測：前 60% 校準、後 40% 測試
    split = tw_dates[int(len(tw_dates) * TRAIN_FRAC)]
    _, _, live_train = build_links(cmd_ret, excess, tw_dates, d1=split)
    test = {"manual": backtest(cmd_ret, excess, tw_dates, manual_links(), split, tw_dates[-1]),
            "calibrated": backtest(cmd_ret, excess, tw_dates, live_train, split, tw_dates[-1])}

    counts = {}
    for p in pairs + discovered:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    out = {
        "generated_at": datetime.now(TPE).isoformat(timespec="minutes"),
        "period": {"start": tw_dates[0], "end": tw_dates[-1], "tw_days": len(tw_dates)},
        "betas": betas,
        "method": {"target": "類股指數漲跌 − β × 加權指數漲跌（β 調整超額報酬）",
                   "discover_in_score": DISCOVER_IN_SCORE,
                   "signal": "陸期主力 收盤 ÷ 前一日結算價", "min_n": MIN_N, "t_valid": T_VALID,
                   "t_lag1": T_LAG1, "t_discover": T_DISCOVER, "rho_full": RHO_FULL, "train_frac": TRAIN_FRAC},
        "counts": counts,
        "pairs": pairs, "discovered": discovered, "live_links": live,
        "backtest": {"train": [tw_dates[0], split], "test": [split, tw_dates[-1]], **test},
        "kline": kline_info,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("組合狀態：", counts)
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
