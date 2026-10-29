"""歷史資料：證交所類股指數（本地快取、增量更新）與新浪陸期日K。

校準（calibrate.py）與每日命中率追蹤（build.py → track.py）共用這裡的資料，
兩者用同一套「類股指數超額報酬」衡量，結果才能互相比較。
"""
from __future__ import annotations

import csv
import json
import re
import time
from datetime import date, datetime, timedelta
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


def estimate_betas(idx_ret, d_end=None):
    """各類股對加權指數的 β（只用 d_end 以前的資料，回測時避免偷看測試期）"""
    xs, ys = {}, {}
    for d, row in idx_ret.items():
        if (d_end and d > d_end) or TAIEX not in row:
            continue
        for n, v in row.items():
            if n != TAIEX:
                xs.setdefault(n, []).append(row[TAIEX])
                ys.setdefault(n, []).append(v)
    betas = {}
    for n in xs:
        x, y = xs[n], ys[n]
        if len(x) < 60:
            continue
        mx, my = sum(x) / len(x), sum(y) / len(y)
        vx = sum((a - mx) ** 2 for a in x)
        betas[n] = round(sum((a - mx) * (b - my) for a, b in zip(x, y)) / vx, 3) if vx else 1.0
    return betas


def excess_returns(idx_ret, betas=None):
    """{date: {sector: 類股漲跌 − β × 加權指數漲跌}}
    β 調整後的超額報酬：去掉大盤漲跌的影響。只用「類股 − 加權指數」時，低 β 的防禦型類股
    在大盤上漲日會被誤判為「受壓」，產生與產業無關的假關聯。betas 為 None 時 β 一律視為 1。"""
    betas = betas or {}
    out = {}
    for d, row in idx_ret.items():
        if TAIEX not in row:
            continue
        out[d] = {n: v - betas.get(n, 1.0) * row[TAIEX] for n, v in row.items() if n != TAIEX}
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


# ───────────── 台股休市日曆 ─────────────
HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
HOLIDAY_JSON = CACHE_DIR / "tw_holidays.json"


def roc_iso(s):
    s = str(s).strip()
    return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}" if len(s) == 7 and s.isdigit() else None


def parse_holidays(rows):
    """證交所公告的休市日。名稱含「開始交易」「最後交易」的是照常交易的日子，要排除。"""
    out = {}
    for r in rows:
        name, d = str(r.get("Name", "")), roc_iso(r.get("Date"))
        if not d or "開始交易" in name or "最後交易" in name:
            continue
        out[d] = name
    return out


def load_holidays():
    return json.loads(HOLIDAY_JSON.read_text(encoding="utf-8")) if HOLIDAY_JSON.exists() else {}


def update_holidays(session, log=print):
    """抓證交所當年度休市日，與快取合併（跨年度累積）。失敗時沿用快取。"""
    cached = load_holidays()
    try:
        r = session.get(HOLIDAY_URL, timeout=25)
        new = parse_holidays(r.json())
        if new:
            cached.update(new)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            HOLIDAY_JSON.write_text(json.dumps(dict(sorted(cached.items())), ensure_ascii=False, indent=0),
                                    encoding="utf-8")
        return cached, len(new), None
    except Exception as e:  # noqa: BLE001
        log(f"⚠ 台股休市日取得失敗，沿用快取：{e}")
        return cached, 0, str(e)[:120]


def is_tw_trading_day(d: date, holidays):
    return d.weekday() < 5 and d.isoformat() not in holidays


# ───────────── 主力合約換月 ─────────────
ROLL_JSON = CACHE_DIR / "rollmap.json"


def contract_months(start: date, end: date):
    """start~end（含）之間的所有 YYMM"""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y % 100:02d}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def fetch_contract(session, code, yymm):
    r = session.get(KLINE_URL.replace("{0}0", "{0}").format(code + yymm), timeout=25,
                    headers={"Referer": "https://finance.sina.com.cn/"})
    m = re.search(r"\((\[.*\])\)", r.text, re.S)
    return json.loads(m.group(1)) if m else []


def _same_bar(a, b):
    try:
        return abs(float(a["c"]) - float(b["c"])) < 1e-6 and int(float(a["v"])) == int(float(b["v"]))
    except (KeyError, ValueError):
        return False


def naive_pairs(rows):
    """{date: (收盤, 基準)}：基準 = 連續主力前一日結算價（換月日會錯用舊合約的結算價）"""
    out = {}
    for prev, cur in zip(rows, rows[1:]):
        try:
            out[cur["d"]] = (float(cur["c"]), float(prev.get("s") or prev["c"]))
        except (KeyError, ValueError):
            continue
    return out


def roll_pairs(session, code, rows, start: date, cache, sleep=0.15, log=print):
    """比對連續主力每天實際是哪個合約；換約日改用「新合約自己的前一日結算價」當基準。
    cache[code] = {"contract": {date: YYMM}, "base": {date: 修正後基準}}，已比對過的日期不重抓。
    回傳 (pairs, 換月清單)"""
    c = cache.setdefault(code, {"contract": {}, "base": {}})
    todo = [r for r in rows if r["d"] >= start.isoformat() and r["d"] not in c["contract"]]
    if todo:
        first = date.fromisoformat(todo[0]["d"])
        months = contract_months(first, date.fromisoformat(todo[-1]["d"]) + timedelta(days=400))
        bars = {}
        for ym in months:
            try:
                k = fetch_contract(session, code, ym)
            except Exception:  # noqa: BLE001
                k = []
            if k:
                bars[ym] = {b["d"]: (i, b) for i, b in enumerate(k)}
                bars[ym]["_rows"] = k
            time.sleep(sleep)
        for r in todo:
            hit = [ym for ym, m in bars.items() if r["d"] in m and _same_bar(m[r["d"]][1], r)]
            if len(hit) == 1:
                c["contract"][r["d"]] = hit[0]
            elif len(hit) > 1:   # 多個合約當天價量完全相同（極少見）：取持倉最大的
                c["contract"][r["d"]] = max(hit, key=lambda ym: float(bars[ym][r["d"]][1].get("p") or 0))
            else:
                c["contract"][r["d"]] = None
        # 換約日：用新合約自己前一筆的結算價
        days = sorted(c["contract"])
        for prev, cur in zip(days, days[1:]):
            a, b = c["contract"][prev], c["contract"][cur]
            if cur in c["base"] or not a or not b or a == b or b not in bars:
                continue
            i, _ = bars[b][cur]
            if i > 0:
                p = bars[b]["_rows"][i - 1]
                c["base"][cur] = float(p.get("s") or p["c"])
    pairs = naive_pairs(rows)
    rolls = []
    days = sorted(c["contract"])
    for prev, cur in zip(days, days[1:]):
        a, b = c["contract"][prev], c["contract"][cur]
        if a and b and a != b and cur in pairs:
            naive = pairs[cur]
            if cur in c["base"]:
                pairs[cur] = (naive[0], c["base"][cur])
            rolls.append({"date": cur, "from": a, "to": b,
                          "naive": round((naive[0] / naive[1] - 1) * 100, 2),
                          "fixed": round((pairs[cur][0] / pairs[cur][1] - 1) * 100, 2) if cur in c["base"] else None})
    return pairs, rolls


def anomaly_cap(limit):
    """換月已修正後，只剔除明顯錯誤的資料。交易所在劇烈行情（如 2026/3 油價）會擴大漲跌停，
    長假後首日也會累積多日漲跌，這些是真實的大波動，不能用一般漲跌停幅度剔除。"""
    return max(limit * 2.5, 15.0)


def pairs_returns(pairs, limit):
    """{date: 漲跌%}；超過 anomaly_cap 的視為資料異常剔除"""
    out, dropped = {}, 0
    for d, (c, base) in pairs.items():
        if base <= 0:
            continue
        r = (c / base - 1) * 100
        if abs(r) > anomaly_cap(limit):
            dropped += 1
            continue
        out[d] = r
    return out, dropped


# ───────────── Yahoo 日線（國際期貨、個股）─────────────
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range={}&interval=1d"


def fetch_yahoo(session, symbol, rng="2y"):
    """回傳 ({交易所當地日期: 收盤}, meta)。優先用還原權息收盤（個股），期貨則用收盤。"""
    r = session.get(YAHOO.format(symbol, rng), timeout=25)
    res = ((r.json().get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise ValueError(f"{symbol} 無資料（HTTP {r.status_code}）")
    meta = res.get("meta") or {}
    off = int(meta.get("gmtoffset") or 0)
    ts = res.get("timestamp") or []
    ind = res.get("indicators") or {}
    closes = ((ind.get("adjclose") or [{}])[0].get("adjclose")) or (ind.get("quote") or [{}])[0].get("close") or []
    out = {}
    for t, c in zip(ts, closes):
        if c:
            out[datetime.utcfromtimestamp(t + off).date().isoformat()] = float(c)
    return out, meta


def intl_returns(closes, cap=15.0):
    """{美國日期: 漲跌%}；超過 cap 的視為連續月換約跳空剔除（Yahoo 連續月未做換月修正）"""
    ds = sorted(closes)
    out = {}
    for p, c in zip(ds, ds[1:]):
        if closes[p]:
            r = (closes[c] / closes[p] - 1) * 100
            if abs(r) <= cap:
                out[c] = r
    return out


def shift_to_tw(ret, tw_dates):
    """美國 D 日的漲跌 → 對齊到 D 之後第一個台股交易日（同一台股日有多個美國日時加總）"""
    import bisect
    out = {}
    for d, r in ret.items():
        i = bisect.bisect_right(tw_dates, d)
        if i < len(tw_dates) and (date.fromisoformat(tw_dates[i]) - date.fromisoformat(d)).days <= 5:
            out[tw_dates[i]] = out.get(tw_dates[i], 0) + r
    return out
