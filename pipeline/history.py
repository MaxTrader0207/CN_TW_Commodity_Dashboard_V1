"""歷史資料：證交所類股指數（本地快取、增量更新）與新浪陸期日K。

校準（calibrate.py）與每日命中率追蹤（build.py → track.py）共用這裡的資料，
兩者用同一套「類股指數超額報酬」衡量，結果才能互相比較。
"""
from __future__ import annotations

import csv
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from mapping import INDUSTRY_CODE

CACHE_DIR = Path(__file__).resolve().parent / "cache"
INDEX_CSV = CACHE_DIR / "tw_index.csv"          # date,name,close
EMPTY_TXT = CACHE_DIR / "tw_index_empty.txt"    # 證交所回傳無資料的日期（休市）
IND_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={}&type=IND&response=json"
KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{0}0=/"
             "InnerFuturesNewService.getDailyKLine?symbol={0}0")
TAIEX = "加權指數"
SECTORS = set(INDUSTRY_CODE.values())
INDEX_NAME_FIX = {"化學": "化學工業", "電腦及週邊設備": "電腦週邊", "發行量加權股價指數": TAIEX}


# ───────────── 證交所類股指數 ─────────────
def index_name(raw):
    raw = raw.strip()
    if raw in INDEX_NAME_FIX:
        return INDEX_NAME_FIX[raw]
    if not raw.endswith("類指數"):
        return None
    base = raw[: -len("類指數")]
    base = INDEX_NAME_FIX.get(base, base)
    return base if base in SECTORS else None


def parse_ind(j):
    """回傳 {名稱: 收盤}；無資料（休市）回傳 {}。"""
    for tb in j.get("tables", []):
        if "價格指數(臺灣證券交易所)" not in (tb.get("title") or ""):
            continue
        out = {}
        for row in tb.get("data", []):
            name = index_name(row[0])
            close = float(str(row[1]).replace(",", "")) if row[1] not in ("", "--") else None
            if name and close:
                out[name] = close
        return out
    return {}


def load_index():
    """{date: {name: close}}"""
    data = {}
    if INDEX_CSV.exists():
        with INDEX_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                data.setdefault(r["date"], {})[r["name"]] = float(r["close"])
    return data


def load_empty():
    return set(EMPTY_TXT.read_text().split()) if EMPTY_TXT.exists() else set()


def save_index(data, empty):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with INDEX_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "name", "close"])
        for d in sorted(data):
            for n, c in sorted(data[d].items()):
                w.writerow([d, n, c])
    EMPTY_TXT.write_text("\n".join(sorted(empty)))


def update_index(session, start: date, end: date, sleep=2.5, max_fail=6, log=print,
                 checkpoint=None, checkpoint_every=100):
    """補齊 start~end 之間缺少的平日；證交所連續失敗 max_fail 次就停（下次再續抓）。"""
    data, empty = load_index(), load_empty()
    todo = []
    d = start
    while d <= end:
        k = d.isoformat()
        if d.weekday() < 5 and k not in data and k not in empty:
            todo.append(d)
        d += timedelta(days=1)
    log(f"類股指數快取：已有 {len(data)} 個交易日，待補 {len(todo)} 天")
    fails = got = 0
    for i, d in enumerate(todo):
        k = d.isoformat()
        try:
            r = session.get(IND_URL.format(d.strftime("%Y%m%d")), timeout=25)
            j = r.json()
            if j.get("stat") != "OK":
                raise ValueError(j.get("stat"))
            rows = parse_ind(j)
            if rows:
                data[k] = rows
                got += 1
            elif d < date.today():            # 今天可能只是還沒公布，不記成休市
                empty.add(k)
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"  ⚠ {k} 失敗（{fails}）：{str(e)[:80]}")
            if fails >= max_fail:
                log("  連續失敗過多，先停止，下次執行會從這裡續抓")
                break
        if (i + 1) % 25 == 0:
            log(f"  進度 {i + 1}/{len(todo)}")
            save_index(data, empty)
        if checkpoint and (i + 1) % checkpoint_every == 0:
            checkpoint(f"cache: 類股指數 {len(data)} 天")
        time.sleep(sleep)
    save_index(data, empty)
    log(f"類股指數快取：新增 {got} 個交易日，共 {len(data)} 個")
    return data, empty


def index_returns(data, empty):
    """{date: {name: 漲跌%}}；只計算前一個交易日確實在快取中的日期（避免跨缺口）。"""
    dates = sorted(data)
    out = {}
    for prev, cur in zip(dates, dates[1:]):
        d = date.fromisoformat(prev) + timedelta(days=1)
        gap = False
        while d < date.fromisoformat(cur):
            if d.weekday() < 5 and d.isoformat() not in empty:
                gap = True
                break
            d += timedelta(days=1)
        if gap:
            continue
        p, c = data[prev], data[cur]
        out[cur] = {n: (c[n] / p[n] - 1) * 100 for n in c if n in p and p[n]}
    return out


def excess_returns(idx_ret):
    """{date: {sector: 類股漲跌 − 加權指數漲跌}}"""
    out = {}
    for d, row in idx_ret.items():
        if TAIEX not in row:
            continue
        out[d] = {n: v - row[TAIEX] for n, v in row.items() if n != TAIEX}
    return out


# ───────────── 新浪陸期日K ─────────────
def fetch_kline(session, code):
    r = session.get(KLINE_URL.format(code), timeout=30, headers={"Referer": "https://finance.sina.com.cn/"})
    m = re.search(r"\((\[.*\])\)", r.text, re.S)
    if not m:
        raise ValueError(f"日K格式無法辨識：{r.text[:80]!r}")
    return json.loads(m.group(1))


def kline_returns(rows, limit):
    """{date: 漲跌%}，以「收盤 ÷ 前一日結算價」計算（與即時版一致）。
    超過漲跌停 +1.5% 的視為主力合約換月造成的跳空，剔除。"""
    out, dropped = {}, 0
    for prev, cur in zip(rows, rows[1:]):
        try:
            base = float(prev.get("s") or prev["c"])
            r = (float(cur["c"]) / base - 1) * 100
        except (KeyError, ValueError, ZeroDivisionError):
            continue
        if abs(r) > limit + 1.5:
            dropped += 1
            continue
        out[cur["d"]] = r
    return out, dropped
