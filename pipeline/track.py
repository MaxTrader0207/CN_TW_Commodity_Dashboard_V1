"""實盤命中率追蹤

每次執行：
  1. 把這次的多頭／空頭前 3 類股寫進訊號帳本 docs/data/track/signals.json
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

import history as H

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
LEDGER = DATA / "track" / "signals.json"
OUT = DATA / "track.json"
MAX_GAP_DAYS = 6


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
    return {f"{run_at[:10]}_{p['session']}": {
        "run_at": run_at, "session": p["session"], "tw_date": p.get("tw_date"),
        "weights": p.get("weights_mode", "手動"),
        "bull": pick(p.get("bull", [])), "bear": pick(p.get("bear", [])),
    }}


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


def update(payload, session=None, log=print):
    """session：requests.Session（None 時只用快取，不連網）"""
    if session is not None:
        today = date.fromisoformat(payload["run_date"])
        try:
            H.update_index(session, today - timedelta(days=21), today, sleep=1.5, max_fail=3, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ 類股指數更新失敗：{e}")
    data, empty = H.load_index(), H.load_empty()
    excess = H.excess_returns(H.index_returns(data, empty))
    tw_dates = sorted(excess)

    led = load_ledger()
    for k, v in entry(payload).items():
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
                     "next": score(sig, excess.get(nxt)) if nxt else None})
    valid = [r for r in rows if r["valid"]]
    summary = {s: {"next": summarize([r for r in valid if r["session"] == s], "next"),
                   "same": summarize([r for r in valid if r["session"] == s], "same")}
               for s in ("day", "night")}
    summary["all"] = {"next": summarize(valid, "next")}
    summary["excluded"] = len(rows) - len(valid)
    out = {"updated": payload["generated_at"], "since": rows[0]["run_at"][:10] if rows else None,
           "index_last": tw_dates[-1] if tw_dates else None,
           "summary": summary, "rows": list(reversed(rows))[:40]}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_done = summary["all"]["next"]["n"]
    log(f"命中率追蹤：帳本 {len(led)} 筆，已開獎（隔日）{n_done} 筆，類股指數最新 {out['index_last']}")
    return out
