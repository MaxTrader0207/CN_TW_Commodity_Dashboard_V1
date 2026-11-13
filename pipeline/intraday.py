"""盤中與夜盤資料蒐集（先累積、之後才驗證；目前不影響任何計分與頁面）

為什麼：證據最強的是「同日」關係，但收盤版 17:10 才出來，當天用不上。陸期日盤 09:00 開盤、和台股同時交易，
如果陸期早盤能領先台股午盤，才是當天可用的訊號。這需要盤中資料才能驗證。盤前版實際用的是陸期夜盤，
但回測只能拿「前一天日盤」代替，也需要夜盤資料才能做真正的回測。

蒐集三樣東西（都存在 pipeline/cache/intraday/，隨 data 分支保存）：
  cn_bars.json.gz    陸期 30／60 分鐘 K 線收盤（新浪，含夜盤）：{代碼: {"YYYY-MM-DD HH:MM": 收盤}}
  tw_bars.json.gz    台股類股指數每 30 分鐘的值（證交所每 5 秒指數統計，收盤後可查歷史）：{日期: {"HH:MM": {類股: 指數}}}
  night_snap.json    每天盤前版（06:17）實際用的陸期夜盤漲跌：{日期: {代碼: [漲跌%, z]}}

每天收盤版順便更新；也可以手動補抓：python pipeline/intraday.py [--tw-days 130] [--max-tw 40]
study() 是累積夠多天之後的檢驗（每週校準時執行並寫進 calibration.json 的 intraday）。
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import history as H  # noqa: E402

TPE = timezone(timedelta(hours=8))
DIR = H.CACHE_DIR / "intraday"
CN_FILE, TW_FILE, NIGHT_FILE = DIR / "cn_bars.json.gz", DIR / "tw_bars.json.gz", DIR / "night_snap.json"
MIN_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{0}0_{1}=/"
           "InnerFuturesNewService.getFewMinLine?symbol={0}0&type={1}")
TW5_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_INDEX?date={}&response=json"
BAR_TYPES = (30, 60)          # 30 分鐘較細、60 分鐘可回溯較久；合併存成同一條時間序列
KEEP_DAYS = 400
CUTOFFS = ("10:30", "12:00")   # 檢驗「到這個時間為止的陸期走勢 → 之後到台股收盤」
MIN_STUDY_DAYS = 40


def _load(path):
    if not path.exists():
        return {}
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(txt)
    else:
        path.write_text(txt, encoding="utf-8")


# ───────────── 陸期分鐘線 ─────────────
def parse_minline(text):
    """新浪分鐘線 JSONP → {"YYYY-MM-DD HH:MM": 收盤}"""
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if not m:
        raise ValueError(f"分鐘線格式無法辨識：{text[:80]!r}")
    out = {}
    for r in json.loads(m.group(1)):
        try:
            out[str(r["d"])[:16]] = float(r["c"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def update_cn(session, codes, log=print):
    """抓各商品的 30／60 分鐘線，併入快取（只增不減，超過 KEEP_DAYS 的丟掉）。回傳 (成功商品數, 最新時間, 錯誤)"""
    bars = _load(CN_FILE)
    ok, err, newest = 0, "", ""
    for code in sorted(codes):
        got = {}
        for typ in BAR_TYPES:
            try:
                r = session.get(MIN_URL.format(code, typ), timeout=25, headers={"Referer": "https://finance.sina.com.cn/"})
                got.update(parse_minline(r.text))
            except Exception as e:  # noqa: BLE001
                err = f"{code} {typ} 分：{str(e)[:80]}"
            time.sleep(0.15)
        if got:
            cur = bars.setdefault(code, {})
            cur.update(got)
            ok += 1
            newest = max(newest, max(got))
    cut = (datetime.now(TPE) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    bars = {c: {t: v for t, v in m.items() if t >= cut} for c, m in bars.items()}
    _save(CN_FILE, bars)
    n_days = len({t[:10] for m in bars.values() for t in m})
    log(f"盤中資料・陸期分鐘線：{ok}/{len(codes)} 個商品，累積 {n_days} 天，最新 {newest or '—'}" + (f"；{err}" if err else ""))
    return ok, newest, err, n_days


# ───────────── 台股類股指數（每 5 秒 → 取每 30 分鐘）─────────────
OPEN_EXTRA = ("09:05:00", "09:10:00", "09:15:00")     # 開盤後的細部時間點：檢驗「開盤後多快進場還吃得到」
OPEN_MARK = "09:05"                                    # 舊資料沒有這一筆 → 需要重抓


def parse_tw5(j):
    """證交所 MI_5MINS_INDEX → {"HH:MM": {類股: 指數}}，留整點與半點（09:00～13:30），外加開盤後 09:05／09:10／09:15"""
    if j.get("stat") != "OK" or not j.get("data"):
        return {}
    names = [None] + [H.index_name(f) for f in j["fields"][1:]]
    out = {}
    for row in j["data"]:
        t = str(row[0]).strip()
        if not (t.endswith(":00:00") or t.endswith(":30:00") or t[:8] in OPEN_EXTRA):
            continue
        vals = {}
        for n, v in zip(names[1:], row[1:]):
            if n:
                try:
                    vals[n] = float(str(v).replace(",", ""))
                except ValueError:
                    pass
        if vals:
            out[t[:5]] = vals
    return out


def update_tw(session, today, holidays, days=130, max_fetch=8, sleep=4.0, log=print):
    """補抓最近 days 天內還沒有的交易日（每次最多 max_fetch 天，避免拖慢每日更新；舊日期慢慢往回補）。
    回傳 (本次補了幾天, 累積天數, 錯誤)"""
    bars = _load(TW_FILE)
    todo, d = [], today
    while (today - d).days <= days:
        k = d.isoformat()
        if H.is_tw_trading_day(d, holidays) and (k not in bars or (bars[k] and OPEN_MARK not in bars[k])):
            todo.append(d)                      # 沒抓過，或是舊格式（缺開盤後細部時間點）
        d -= timedelta(days=1)
    got, err, fails = 0, "", 0
    for d in todo[:max_fetch]:          # 由近到遠
        try:
            r = session.get(TW5_URL.format(d.strftime("%Y%m%d")), timeout=30)
            rows = parse_tw5(r.json())
            if rows and "13:30" in rows:
                bars[d.isoformat()] = rows
                got += 1
            elif d == today:
                err = "今天的資料尚未公布"
            elif not bars.get(d.isoformat()):
                bars[d.isoformat()] = {}        # 查無資料（非交易日或太久以前）：記空的，下次不再重試
        except Exception as e:  # noqa: BLE001
            err, fails = str(e)[:100], fails + 1
            if fails >= 5:
                break
            time.sleep(25)              # 證交所連續查詢太快會斷線：等久一點再繼續
        time.sleep(sleep)
    cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
    bars = {k: v for k, v in bars.items() if k >= cut}
    _save(TW_FILE, bars)
    n = sum(1 for v in bars.values() if v)
    log(f"盤中資料・台股類股指數：本次補 {got} 天，累積 {n} 天，還缺 {max(0, len(todo) - got)} 天" + (f"；{err}" if err else ""))
    return got, n, err


# ───────────── 盤前版實際用的夜盤漲跌 ─────────────
def save_night_snapshot(run_date, board):
    snap = _load(NIGHT_FILE)
    snap[run_date] = {c["code"]: [c["chg"], c.get("z")] for c in board
                      if c.get("kind", "commodity") == "commodity" and c.get("verified") and c.get("status") == "ok"}
    keep = sorted(snap)[-KEEP_DAYS:]
    _save(NIGHT_FILE, {k: snap[k] for k in keep})
    return len(snap[run_date])


def update(session, codes, today, holidays, max_tw=8, tw_days=130, log=print):
    ok, newest, e1, n_cn = update_cn(session, codes, log)
    got, n_tw, e2 = update_tw(session, today, holidays, days=tw_days, max_fetch=max_tw, log=log)
    return {"cn_ok": ok, "cn_total": len(codes), "cn_days": n_cn, "cn_newest": newest, "tw_new": got, "tw_days": n_tw,
            "errors": [x for x in (e1, e2) if x]}


# ───────────── 檢驗（資料夠多才有意義）─────────────
def _last_at(bars, lo, hi):
    """lo < 時間 ≤ hi 的最後一根收盤"""
    ts = [t for t in bars if lo < t <= hi]
    return bars[max(ts)] if ts else None


def cn_moves(cn_bars, tw_dates):
    """每個台股交易日 D、每個商品：
      night  ＝ 前一個陸期日盤收盤（≤ D−1 15:00）→ 夜盤結束（D 09:00 前最後一根）
      到截點 ＝ 前一個陸期日盤收盤 → D 當天截點（10:30、12:00）最後一根
    回傳 {key: {代碼: {D: 漲跌%}}}，key 為 "night" 或截點"""
    out = {k: {} for k in ("night",) + CUTOFFS}
    for code, bars in cn_bars.items():
        # 有日盤的日子（週六凌晨只有夜盤尾段，不算）
        days = sorted({t[:10] for t in bars if "09:00" < t[11:] <= "15:00"})
        for d in tw_dates:
            prev = [x for x in days if x < d]
            if not prev:
                continue
            p = prev[-1]
            base = _last_at(bars, p + " 09:00", p + " 15:00")
            if not base:
                continue
            night = _last_at(bars, p + " 15:00", d + " 08:59")
            if night:
                out["night"].setdefault(code, {})[d] = (night / base - 1) * 100
            for c in CUTOFFS:
                v = _last_at(bars, d + " 08:59", f"{d} {c}")
                if v:
                    out[c].setdefault(code, {})[d] = (v / base - 1) * 100
    return out


def _sd(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else None


def _agg(xs):
    n = len(xs)
    if not n:
        return {"n": 0, "hit": None, "avg": None, "t": None}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0
    return {"n": n, "hit": round(sum(x > 0 for x in xs) / n, 3), "avg": round(m, 3),
            "t": round(m / (sd / math.sqrt(n)), 2) if sd else None}


OPEN_CUTS = ("09:05", "09:10", "09:15", "09:30", "10:00")     # 「開盤後」的進場時間點（證交所指數 09:00 那一筆是前一天收盤）


def tw_segments(tw_bars, betas, cut="09:30", taiex="加權指數"):
    """把每個類股當天的 β 調整超額報酬拆成兩段：
      gap   前一天收盤 → cut（開盤跳空＋開盤後到 cut 的反應，開盤前看到訊號的人買不到這段）
      after cut → 收盤（cut 進場還吃得到的部分）
    回傳 {"gap": {類股: {日期: %}}, "after": {...}}"""
    out = {"gap": {}, "after": {}}
    for d, bars in tw_bars.items():
        a, b, c = bars.get("09:00"), bars.get(cut), bars.get("13:30")
        if not (a and b and c) or not all(x.get(taiex) for x in (a, b, c)):
            continue
        mg, ma = (b[taiex] / a[taiex] - 1) * 100, (c[taiex] / b[taiex] - 1) * 100
        for sct, beta in betas.items():
            if a.get(sct) and b.get(sct) and c.get(sct):
                out["gap"].setdefault(sct, {})[d] = (b[sct] / a[sct] - 1) * 100 - beta * mg
                out["after"].setdefault(sct, {})[d] = (c[sct] / b[sct] - 1) * 100 - beta * ma
    return out


def study(cn_bars, tw_bars, links, betas, daily_excess=None, taiex="加權指數"):
    """用累積的盤中資料檢驗（全部是樣本內的初步觀察，資料不足 MIN_STUDY_DAYS 天時回傳 ready=False）：
      盤中：到 10:30／12:00 為止的陸期走勢 → 該時間到台股收盤的類股超額報酬
      夜盤：陸期夜盤走勢 → 台股當天的類股超額報酬（daily_excess，與校準同一把尺）
    商品訊號 = 漲跌 ÷ 該商品同一種漲跌的歷史幅度（均方根），類股分數 = Σ 權重 × 訊號；
    每個「類股 × 日」算 分數方向 × 實際超額報酬。"""
    tw_dates = sorted(d for d, v in tw_bars.items() if v and "13:30" in v)
    moves = cn_moves(cn_bars, tw_dates)
    out = {"tw_days": len(tw_dates), "ready": len(tw_dates) >= MIN_STUDY_DAYS, "min_days": MIN_STUDY_DAYS, "tests": {}}
    by_sector = {}
    for code, ls in links.items():
        for l in ls:
            by_sector.setdefault(l["sector"], []).append((code, l["w"]))

    def scores(mv, d):
        sc = {}
        for sct, cs in by_sector.items():
            num = 0.0
            for code, w in cs:
                r, sd = mv.get(code, {}).get(d), sds.get(code)
                if r is not None and sd:
                    num += w * max(-2.0, min(2.0, r / sd))
            if num:
                sc[sct] = num
        return sc

    for key in ("night",) + CUTOFFS:
        mv = moves[key]
        sds = {c: _sd(list(m.values())) for c, m in mv.items() if len(m) >= 20}
        outs, days_used = [], 0
        for d in tw_dates:
            sc = scores(mv, d)
            if not sc:
                continue
            if key == "night":
                row = (daily_excess or {}).get(d) or {}
            else:
                a, b = tw_bars[d].get(key), tw_bars[d]["13:30"]
                if not a or not a.get(taiex) or not b.get(taiex):
                    continue
                mkt = (b[taiex] / a[taiex] - 1) * 100
                row = {s: (b[s] / a[s] - 1) * 100 - (betas or {}).get(s, 1.0) * mkt for s in sc if a.get(s) and b.get(s)}
            xs = [(1 if v > 0 else -1) * row[s] for s, v in sc.items() if s in row]
            if xs:
                outs += xs
                days_used += 1
        out["tests"][key] = {**_agg(outs), "days": days_used}
    return out


def main():
    import requests
    ap = argparse.ArgumentParser()
    ap.add_argument("--tw-days", type=int, default=130)
    ap.add_argument("--max-tw", type=int, default=40, help="這次最多補幾個交易日的台股盤中指數")
    a = ap.parse_args()
    import focus
    from build import UA
    from mapping import COMMODITIES, SPREADS
    cal_f = Path(__file__).resolve().parent.parent / "docs" / "data" / "calibration.json"
    cal = json.loads(cal_f.read_text(encoding="utf-8")) if cal_f.exists() else {}
    ver = focus.verified_codes(cal, SPREADS)
    codes = ver["need"] if ver else {c[1] for c in COMMODITIES}
    s = requests.Session()
    s.headers.update(UA)
    hol, _n, _e = H.update_holidays(s)
    res = update(s, codes, datetime.now(TPE).date(), hol, max_tw=a.max_tw, tw_days=a.tw_days)
    print(json.dumps(res, ensure_ascii=False))
    st = study(_load(CN_FILE), _load(TW_FILE), cal.get("live_links") or {}, cal.get("betas"))
    print("初步檢驗：", json.dumps(st, ensure_ascii=False))
    if res["cn_ok"] == 0:
        raise SystemExit("陸期分鐘線全部抓取失敗")


if __name__ == "__main__":
    main()
