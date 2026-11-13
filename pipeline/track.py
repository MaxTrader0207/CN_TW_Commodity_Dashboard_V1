"""實盤命中率追蹤

每次執行：
  1. 把這次的狀態燈、預測卡方向（以及舊排法的多空前 3 類股）寫進訊號帳本 docs/data/track/signals.json
     （同一天同一版本只留最後一次，因為那才是收盤後的最終訊號）
  2. 用證交所類股指數「超額報酬」（類股 − 加權指數，與 calibrate.py 回測同一把尺）開獎：
       隔日：訊號產生後的下一個台股交易日（日盤版 17:10 → 明天；夜盤版 08:10 → 今天）
       同日：僅日盤版，當天台股表現（解釋用，不算預測）
  3. 彙整到 docs/data/track.json 給儀表板顯示

第一次執行時會從 docs/data/history/ 既有的快照補建帳本。
"""
from __future__ import annotations

import bisect
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import focus as F
import history as H

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
LEDGER = DATA / "track" / "signals.json"
OUT = DATA / "track.json"
MAX_GAP_DAYS = 6
# 實盤停損：累積滿 GATE_N 次後，最近 GATE_WINDOW 次的方向命中率低於 GATE_HIT 且平均報酬為負 → 該區塊降級為「研究中」
GATE_N, GATE_WINDOW, GATE_HIT = 40, 60, 0.45


def load_ledger():
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    led = {}
    for f in sorted((DATA / "history").glob("*_*.json")):     # 首次：從歷史快照補建
        try:
            led.update(entry(json.loads(f.read_text(encoding="utf-8"))))
        except Exception:  # noqa: BLE001
            pass
    return led


def entry(p):
    if p.get("demo"):
        return {}
    by = {s["name"]: s for s in p.get("sectors", [])}
    pick = lambda names: [{"name": n, "score": by[n]["score"]} for n in names if n in by]  # noqa: E731
    run_at = p["generated_at"]
    e = {"run_at": run_at, "session": p["session"], "tw_date": p.get("tw_date"),
         "weights": p.get("weights_mode", "手動"),
         "bull": pick(p.get("bull", [])), "bear": pick(p.get("bear", []))}
    if p.get("focus"):
        # 訊號當下的信心等級（之後校準改變也不回頭改）、高信心排法的前 3、隔日預測卡方向
        e["conf"] = {s["name"]: (s.get("confidence") or {}).get("level") for s in p.get("sectors", [])}
        fs = [by[n] for n in p["focus"].get("sectors", []) if n in by]
        e["focus_bull"] = pick([s["name"] for s in sorted(fs, key=lambda s: -s["score"]) if s["score"] > 0][:3])
        e["focus_bear"] = pick([s["name"] for s in sorted(fs, key=lambda s: s["score"]) if s["score"] < 0][:3])
        e["cards"] = [{"sector": c["sector"], "direction": c["direction"], "target": c.get("target"),
                       "kind": c.get("kind", "cn"), "tier": c.get("tier")}
                      for c in p["focus"].get("cards", [])]
        # 狀態燈（有實測支持的類股）：state +1 偏多／−1 偏空／0 沒亮
        e["lights"] = [{"name": s["name"], "state": s["light"]["state"], "z": s["light"]["z"]}
                       for s in p.get("sectors", []) if s.get("light")]
    return {f"{run_at[:10]}_{p['session']}": e}


def validity(sig):
    """訊號是否在「該有的時間」產生：夜盤版要在台股開盤（09:00）前，日盤版要在陸期日盤收盤（15:00）後。
    GitHub 排程可能延遲數小時，延到盤中才產生的訊號不計入命中率。"""
    h = datetime.fromisoformat(sig["run_at"])
    hm = h.hour + h.minute / 60
    if sig["session"] == "night":
        return (hm < 9, "" if hm < 9 else f"{h:%H:%M} 才產生，已過台股開盤")
    return (hm >= 15, "" if hm >= 15 else f"{h:%H:%M} 產生，陸期日盤尚未收盤")


def target_dates(sig, tw_dates):
    """(同日, 隔日)；同日只對日盤版有意義"""
    run = datetime.fromisoformat(sig["run_at"])
    day = run.date().isoformat()
    # 14:00 後產生的訊號 → 下一個交易日；早上產生（夜盤版）→ 當天（含）之後第一個交易日
    i = bisect.bisect_right(tw_dates, day) if run.hour >= 14 else bisect.bisect_left(tw_dates, day)
    nxt = tw_dates[i] if i < len(tw_dates) else None
    if nxt and (date.fromisoformat(nxt) - run.date()).days > MAX_GAP_DAYS:
        nxt = None
    same = day if (sig["session"] == "day" and day in tw_dates) else None
    return same, nxt


def score(sig, row):
    if not row:
        return None
    b = [row[x["name"]] for x in sig["bull"] if x["name"] in row]
    e = [row[x["name"]] for x in sig["bear"] if x["name"] in row]
    avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    r = {"bull": [round(v, 3) for v in b], "bear": [round(v, 3) for v in e],
         "bull_avg": avg(b), "bear_avg": avg(e)}
    r["spread"] = r["bull_avg"] - r["bear_avg"] if b and e else None
    return r


def summarize(rows, key):
    xs = [r[key] for r in rows if r.get(key)]
    bull = [v for x in xs for v in x["bull"]]
    bear = [v for x in xs for v in x["bear"]]
    sp = [x["spread"] for x in xs if x["spread"] is not None]
    rnd = lambda v: None if v is None else round(v, 3)  # noqa: E731
    return {
        "n": len(xs),
        "bull_avg": rnd(sum(bull) / len(bull)) if bull else None,
        "bull_win": rnd(sum(v > 0 for v in bull) / len(bull)) if bull else None,
        "bear_avg": rnd(sum(bear) / len(bear)) if bear else None,
        "bear_win": rnd(sum(v < 0 for v in bear) / len(bear)) if bear else None,
        "spread_avg": rnd(sum(sp) / len(sp)) if sp else None,
        "spread_win": rnd(sum(v > 0 for v in sp) / len(sp)) if sp else None,
    }


def current_conf():
    """舊帳本沒有記錄信心等級時，用目前的校準結果回推（標示為回推）"""
    f = DATA / "calibration.json"
    try:
        cal = json.loads(f.read_text(encoding="utf-8"))
        return {k: v["level"] for k, v in F.sector_confidence(cal).items()}
    except Exception:  # noqa: BLE001
        return {}


def _agg(xs):
    rnd = lambda v: None if v is None else round(v, 3)  # noqa: E731
    return {"n": len(xs), "avg": rnd(sum(xs) / len(xs)) if xs else None,
            "win": rnd(sum(v > 0 for v in xs) / len(xs)) if xs else None}


def sector_breakdown(pairs, excess, conf_now):
    """依類股、信心等級拆解隔日表現（數字 = 訊號方向 × 下一交易日超額報酬，正值代表方向對）；
    另外比較「高信心排法」的前 3 與隔日預測卡"""
    by_sec, by_lvl, focus_rows, card = {}, {"A": [], "B": [], "none": []}, [], []
    backfilled = 0
    for sig, r in pairs:
        row = excess.get(r["next_date"]) if r.get("next_date") else None
        if not row:
            continue
        conf = sig.get("conf")
        if conf is None:
            conf, backfilled = conf_now, backfilled + 1
        for side, d in (("bull", 1), ("bear", -1)):
            for x in sig[side]:
                v = row.get(x["name"])
                if v is None:
                    continue
                by_sec.setdefault(x["name"], []).append(d * v)
                by_lvl[conf.get(x["name"]) or "none"].append(d * v)
        if "focus_bull" in sig:
            focus_rows.append(score({"bull": sig["focus_bull"], "bear": sig["focus_bear"]}, row))
        for c in sig.get("cards", []):
            v = row.get(c["sector"])
            if v is not None and c["direction"]:
                card.append(c["direction"] * v)
    xs = {"focus": focus_rows}
    return {
        "by_level": {k: _agg(v) for k, v in by_lvl.items()},
        "by_sector": dict(sorted(((k, _agg(v)) for k, v in by_sec.items()), key=lambda kv: -kv[1]["n"])),
        "focus_rank": summarize([{"next": x} for x in xs["focus"] if x], "next"),
        "cards": _agg(card),
        "backfilled": backfilled,
        "note": "數字 = 訊號方向 × 下一交易日 β 調整超額報酬（正值 = 方向對）",
    }


def gate(xs):
    """xs：依時間排序的「訊號方向 × 實際超額報酬」。回傳最近 GATE_WINDOW 次的表現與狀態：
    collecting 累積中／ok 達標／demoted 未達標（頁面降級為研究中）；之後表現回升會自動恢復"""
    a = _agg(xs[-GATE_WINDOW:])
    if len(xs) < GATE_N:
        state = "collecting"
    else:
        state = "demoted" if (a["win"] < GATE_HIT and a["avg"] < 0) else "ok"
    return {**a, "total": len(xs), "need": GATE_N, "window": GATE_WINDOW, "min_hit": GATE_HIT, "state": state}


def light_outcomes(sig, same_row, next_row):
    """這筆訊號每個亮燈類股的結果（方向 × 超額報酬）"""
    out = []
    for x in sig.get("lights") or []:
        if not x.get("state"):
            continue
        r = {"name": x["name"], "state": x["state"], "z": x.get("z")}
        for k, row in (("same", same_row), ("next", next_row)):
            v = (row or {}).get(x["name"])
            r[k] = None if v is None else round(x["state"] * v, 3)
        out.append(r)
    return out


def build_gates(valid_rows, led, excess):
    """各區塊的實盤表現與停損狀態。
    pre：盤前版亮燈 → 當天；day_next：收盤版亮燈 → 下一交易日；day_same：收盤版亮燈 → 當天（回顧用，不判定）；
    cards_cn／cards_intl：預測卡方向 → 目標日的類股超額報酬"""
    xs = {"pre": [], "day_next": [], "day_same": [], "cards_cn": [], "cards_intl": []}
    for r in valid_rows:                    # 依時間由舊到新
        sig = led[r["key"]]
        for x in r.get("lights") or []:
            if r["session"] == "night":
                if x["next"] is not None:
                    xs["pre"].append(x["next"])
            else:
                if x["next"] is not None:
                    xs["day_next"].append(x["next"])
                if x["same"] is not None:
                    xs["day_same"].append(x["same"])
        row = excess.get(r["next_date"]) if r.get("next_date") else None
        for c in sig.get("cards") or []:
            v = (row or {}).get(c["sector"])
            if v is not None and c.get("direction"):
                xs["cards_" + c.get("kind", "cn")].append(c["direction"] * v)
    g = {k: gate(v) for k, v in xs.items()}
    g["day_same"]["state"] = "info"         # 同日只是回顧，不做停損判定
    return g


def update(payload, session=None, log=print, skip_signal=False):
    """session：requests.Session（None 時只用快取，不連網）"""
    if session is not None:
        today = date.fromisoformat(payload["run_date"])
        try:
            H.update_index(session, today - timedelta(days=21), today, sleep=1.5, max_fail=3, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ 類股指數更新失敗：{e}")
    data, empty = H.load_index(), H.load_empty()
    betas = None
    cal = DATA / "calibration.json"
    if cal.exists():
        try:
            betas = json.loads(cal.read_text(encoding="utf-8")).get("betas")
        except Exception:  # noqa: BLE001
            pass
    excess = H.excess_returns(H.index_returns(data, empty), betas)   # 與校準同一把尺：β 調整超額報酬
    tw_dates = sorted(excess)

    led = load_ledger()
    new_entries = {} if skip_signal else entry(payload)
    if skip_signal:
        log("帳本：陸期無新報價（休市），本次訊號不記入")
    for k, v in new_entries.items():
        # 同一天同版本：已有準時的訊號時，不被之後延遲產生的訊號覆蓋
        if k in led and validity(led[k])[0] and not validity(v)[0]:
            log(f"帳本：{k} 已有準時訊號，這次（{v['run_at']}）不覆蓋")
            continue
        led[k] = v
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")

    rows = []
    for key in sorted(led):
        sig = led[key]
        same, nxt = target_dates(sig, tw_dates)
        ok, why = validity(sig)
        rows.append({"key": key, "run_at": sig["run_at"], "session": sig["session"],
                     "valid": ok, "invalid_reason": why,
                     "weights": sig.get("weights"),
                     "bull": [x["name"] for x in sig["bull"]], "bear": [x["name"] for x in sig["bear"]],
                     "same_date": same, "next_date": nxt,
                     "same": score(sig, excess.get(same)) if same else None,
                     "next": score(sig, excess.get(nxt)) if nxt else None,
                     "lights": light_outcomes(sig, excess.get(same) if same else None, excess.get(nxt) if nxt else None),
                     "cards": [{"sector": c["sector"], "kind": c.get("kind", "cn"), "direction": c["direction"],
                                "ret": None if not nxt or c["sector"] not in (excess.get(nxt) or {}) or not c["direction"]
                                else round(c["direction"] * excess[nxt][c["sector"]], 3)}
                               for c in sig.get("cards") or []]})
    valid = [r for r in rows if r["valid"]]
    breakdown = sector_breakdown([(led[r["key"]], r) for r in rows if r["valid"]], excess, current_conf())
    summary = {s: {"next": summarize([r for r in valid if r["session"] == s], "next"),
                   "same": summarize([r for r in valid if r["session"] == s], "same")}
               for s in ("day", "night")}
    summary["all"] = {"next": summarize(valid, "next")}
    summary["excluded"] = len(rows) - len(valid)
    summary["breakdown"] = breakdown
    summary["gates"] = build_gates(valid, led, excess)
    out = {"updated": payload["generated_at"], "since": rows[0]["run_at"][:10] if rows else None,
           "index_last": tw_dates[-1] if tw_dates else None,
           "summary": summary, "rows": list(reversed(rows))[:40]}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n_done = summary["all"]["next"]["n"]
    log(f"命中率追蹤：帳本 {len(led)} 筆，已開獎（隔日）{n_done} 筆，類股指數最新 {out['index_last']}")
    return out
