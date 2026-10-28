"""陸期 × 台股類股 多空儀表板 — 每日資料管線

流程：
 1. 新浪期貨抓上期所(含INE)／大商所／鄭商所主力連續合約，算漲跌幅（對昨結算）
 2. 證交所／櫃買中心 OpenAPI 抓上市櫃個股收盤與產業別
 3. 依 mapping.py 關聯權重，計算各三竹類股的「陸期助攻分數」
 4. 取分數最高 3 類股為多頭、最低 3 類股為空頭，列出各類股前5強／前5弱個股
 5. 輸出 docs/data/latest.json 與歷史檔，供 GitHub Pages 儀表板讀取

用法：python pipeline/build.py [--session day|night] [--fixtures DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapping import COMMODITIES, INDUSTRY_CODE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
CACHE_FILE = ROOT / "pipeline" / "industry_cache.json"
TPE = timezone(timedelta(hours=8))

MIN_LOTS = 100          # 個股排行的最低成交量（張），過濾冷門股
TOP_N_SECTORS = 3
TOP_N_STOCKS = 5
LIMIT_BONUS = 1.5       # 漲跌停商品訊號放大倍數
HISTORY_KEEP = 90

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*"}
SINA_URL = "https://hq.sinajs.cn/list="
TWSE_QUOTES = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_INFO = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_QUOTES = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_INFO = "https://openapi.twse.com.tw/v1/opendata/t187ap03_O"
TPEX_INFO_ALT = "https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap03_O"
ISIN_OTC = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"


# ───────────────────────── 取數 ─────────────────────────
class Source:
    """真實網路來源；測試時用 FixtureSource 取代。"""

    def __init__(self):
        import requests
        self.s = requests.Session()
        self.s.headers.update(UA)

    def _get(self, url, **kw):
        last = None
        for i in range(3):
            try:
                r = self.s.get(url, timeout=25, **kw)
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (i + 1))
        raise RuntimeError(f"{url} 取得失敗：{last}")

    def sina(self, symbols):
        r = self._get(SINA_URL + ",".join(symbols),
                      headers={"Referer": "https://finance.sina.com.cn/"})
        r.encoding = "gbk"
        return r.text

    def text(self, url, encoding):
        r = self._get(url)
        r.encoding = encoding
        return r.text

    def json(self, url):
        r = self._get(url)
        try:
            return r.json()
        except ValueError:
            print(f"⚠ {url} 非 JSON（HTTP {r.status_code}, {r.headers.get('content-type')}）：{r.text[:200]!r}")
            raise


class FixtureSource:
    def __init__(self, d):
        self.d = Path(d)

    def text(self, url, encoding):
        f = self.d / "isin_otc.html"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def sina(self, symbols):
        return (self.d / "sina.txt").read_text(encoding="utf-8")

    def json(self, url):
        name = {TWSE_QUOTES: "twse_quotes", TWSE_INFO: "twse_info", TPEX_QUOTES: "tpex_quotes",
                TPEX_INFO: "tpex_info", TPEX_INFO_ALT: "tpex_info"}[url]
        return json.loads((self.d / f"{name}.json").read_text(encoding="utf-8"))


def num(x):
    if x is None:
        return None
    s = str(x).replace(",", "").replace("+", "").replace("X", "").strip()
    if s in ("", "--", "---", "除權息", "除權", "除息"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pick(row, *keys):
    for k in keys:
        if k in row and str(row[k]).strip() != "":
            return row[k]
    return None


# ───────────────────────── 陸期 ─────────────────────────
SINA_LINE = re.compile(r'hq_str_nf_([A-Za-z]+)0="([^"]*)"')


def parse_sina(text):
    """新浪國內期貨欄位：0名稱 1時間 2開 3高 4低 5昨收 6買 7賣 8最新 9結算 10昨結算 ... 17日期"""
    out = {}
    for code, body in SINA_LINE.findall(text):
        f = body.split(",")
        if len(f) < 11:
            continue
        last, prev_settle, prev_close = num(f[8]), num(f[10]), num(f[5])
        base = prev_settle if prev_settle else prev_close
        if not last or not base:
            continue
        out[code.upper()] = {
            "last": last, "base": base, "chg": round((last / base - 1) * 100, 2),
            "high": num(f[3]), "low": num(f[4]),
            "date": f[17] if len(f) > 17 else None,
        }
    return out


def commodity_board(src):
    symbols = [f"nf_{c[1]}0" for c in COMMODITIES]
    quotes = {}
    for i in range(0, len(symbols), 30):          # 分批避免網址過長
        text = src.sina(symbols[i:i + 30])
        if i == 0:
            print("新浪回應樣本：", text[:300].replace("\n", " | "))
        quotes.update(parse_sina(text))
    board = []
    for ex, code, name, limit, links in COMMODITIES:
        q = quotes.get(code)
        item = {"ex": ex, "code": code, "name": name, "limit": limit,
                "links": [{"sector": s, "w": w, "why": why} for s, w, why in links]}
        if not q:
            item.update(status="missing", chg=None, signal=0, at_limit=None)
        else:
            chg = q["chg"]
            at_limit = "up" if chg >= limit - 0.1 else "down" if chg <= -(limit - 0.1) else None
            sig = max(-1.0, min(1.0, chg / limit))
            if at_limit:
                sig *= LIMIT_BONUS
            item.update(status="ok", last=q["last"], chg=chg, signal=round(sig, 4),
                        at_limit=at_limit, quote_date=q["date"])
        board.append(item)
    return board


# ───────────────────────── 台股 ─────────────────────────
ISIN_NAME_FIX = {"電腦及週邊設備業": "電腦週邊", "其他業": "其他", "生技醫療業": "生技醫療"}


def isin_industry(name):
    name = name.strip()
    if name in ISIN_NAME_FIX:
        return ISIN_NAME_FIX[name]
    base = name
    for suf in ("工業", "業"):
        if name.endswith(suf) and name not in INDUSTRY_CODE.values():
            base = name[: -len(suf)]
            break
    names = INDUSTRY_CODE.values()
    if name in names:
        return name
    if base in names:
        return base
    hits = [v for v in names if name.startswith(v)]
    return max(hits, key=len) if hits else None


def parse_isin(html):
    """證交所 ISIN 公開查詢頁：每列 代號　名稱 | ISIN | 上市日 | 市場別 | 產業別 | CFI | 備註"""
    out = {}
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S | re.I):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) < 5:
            continue
        m = re.match(r"^([0-9A-Z]{4,6})[\u3000\s]", cells[0])
        sec = isin_industry(cells[4]) if m and cells[4] else None
        if m and sec:
            out[m.group(1)] = sec
    return out


def optional(src, url, label):
    """櫃買資料抓不到時不中斷，只用上市股票。"""
    try:
        rows = src.json(url)
        show_keys(label, rows)
        return rows
    except Exception as e:  # noqa: BLE001
        print(f"⚠ {label} 取得失敗，本次略過上櫃股票：{e}")
        return []


def show_keys(label, rows):
    if rows:
        print(f"{label}：{len(rows)} 筆，欄位 {list(rows[0].keys())[:12]}")
    else:
        print(f"{label}：0 筆")


def load_industry(src):
    """回傳 {代號: 類股名稱}；失敗時用上次快取。"""
    mapping = {}
    try:
        twse_rows = src.json(TWSE_INFO)
        show_keys("證交所公司資料", twse_rows)
        tpex_rows = []  # 櫃買 OpenAPI 公司資料端點目前回傳網頁，改用下方 ISIN 頁
        for row in twse_rows:
            code = pick(row, "公司代號", "Code")
            ind = pick(row, "產業別", "IndustryCode")
            if code and ind:
                mapping[str(code).strip()] = INDUSTRY_CODE.get(str(ind).strip().zfill(2))
        if not tpex_rows:
            try:
                otc = parse_isin(src.text(ISIN_OTC, "cp950"))
                print(f"上櫃產業別(ISIN 頁)：{len(otc)} 檔")
                mapping.update(otc)
            except Exception as e:  # noqa: BLE001
                print("⚠ ISIN 上櫃產業別取得失敗：", e)
        for row in tpex_rows:
            code = pick(row, "SecuritiesCompanyCode", "公司代號", "CompanyCode")
            ind = pick(row, "SecuritiesIndustryCode", "產業別", "IndustryCode")
            if code and ind:
                mapping[str(code).strip()] = INDUSTRY_CODE.get(str(ind).strip().zfill(2))
        mapping = {k: v for k, v in mapping.items() if v}
        if len(mapping) > 500:
            CACHE_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=0), encoding="utf-8")
            return mapping, "live"
    except Exception as e:  # noqa: BLE001
        print("⚠ 產業別抓取失敗，改用快取：", e)
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8")), "cache"
    raise RuntimeError("無產業別資料且無快取")


def roc_to_iso(s):
    s = str(s or "").strip().replace("/", "")
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def load_quotes(src):
    stocks, dates = {}, set()
    for row in src.json(TWSE_QUOTES):
        code = str(pick(row, "Code", "證券代號") or "").strip()
        close, chg = num(pick(row, "ClosingPrice", "收盤價")), num(pick(row, "Change", "漲跌價差"))
        vol = num(pick(row, "TradeVolume", "成交股數"))
        if pick(row, "Date"):
            dates.add(roc_to_iso(row["Date"]))
        if code and close and chg is not None and close - chg > 0:
            stocks[code] = {"code": code, "name": str(pick(row, "Name", "證券名稱")).strip(),
                            "close": close, "chg_pct": round(chg / (close - chg) * 100, 2),
                            "lots": int((vol or 0) / 1000), "mkt": "上市"}
    for row in optional(src, TPEX_QUOTES, "櫃買收盤行情"):
        code = str(pick(row, "SecuritiesCompanyCode", "代號") or "").strip()
        close, chg = num(pick(row, "Close", "收盤")), num(pick(row, "Change", "漲跌"))
        vol = num(pick(row, "TradingShares", "TradeVolume", "成交股數"))
        if pick(row, "Date"):
            dates.add(roc_to_iso(row["Date"]))
        if code and close and chg is not None and close - chg > 0:
            stocks[code] = {"code": code, "name": str(pick(row, "CompanyName", "名稱")).strip(),
                            "close": close, "chg_pct": round(chg / (close - chg) * 100, 2),
                            "lots": int((vol or 0) / 1000), "mkt": "上櫃"}
    dates.discard(None)
    print("台股各來源資料日：", sorted(dates))
    return stocks, (max(dates) if dates else None)


# ───────────────────────── 評分 ─────────────────────────
def rank_stocks(members, reverse, n=TOP_N_STOCKS):
    """依漲跌幅排序；流動性足夠的個股不足 n 檔時才放寬成交量門檻。"""
    liquid = [s for s in members if s["lots"] >= MIN_LOTS]
    pool = liquid if len(liquid) >= n else members
    return sorted(pool, key=lambda s: s["chg_pct"], reverse=reverse)[:n]


def norm(den):
    """以權重和的平方根正規化：多項商品同向時分數較高（廣度），
    單一弱關聯商品(分母<1)不會被放大。"""
    return max(1.0, den) ** 0.5


def score_sectors(board, industry, stocks):
    sectors = {}
    for c in board:
        for l in c["links"]:
            sec = sectors.setdefault(l["sector"], {"name": l["sector"], "num": 0.0, "den": 0.0, "drivers": []})
            sec["den"] += abs(l["w"])
            if c["status"] != "ok":
                continue
            contrib = l["w"] * c["signal"]
            sec["num"] += contrib
            sec["drivers"].append({"code": c["code"], "name": c["name"], "ex": c["ex"], "chg": c["chg"],
                                   "at_limit": c["at_limit"], "w": l["w"], "why": l["why"],
                                   "_raw": contrib})
    by_sector = {}
    for code, sec_name in industry.items():
        if code in stocks:
            by_sector.setdefault(sec_name, []).append({**stocks[code], "sector": sec_name})

    out = []
    for sec in sectors.values():
        members = by_sector.get(sec["name"], [])
        score = round(sec["num"] / norm(sec["den"]) * 100, 1)
        for d in sec["drivers"]:
            d["contrib"] = round(d.pop("_raw") / norm(sec["den"]) * 100, 1)
        sec["drivers"].sort(key=lambda d: abs(d["contrib"]), reverse=True)
        strong = rank_stocks(members, True, min(TOP_N_STOCKS, (len(members) + 1) // 2))
        avg = round(sum(m["chg_pct"] for m in members) / len(members), 2) if members else None
        out.append({
            "name": sec["name"], "score": score, "drivers": sec["drivers"],
            "tw_avg_chg": avg, "n": len(members),
            "tw_up": sum(m["chg_pct"] > 0 for m in members),
            "tw_down": sum(m["chg_pct"] < 0 for m in members),
            "resonance": bool(avg is not None and score and (avg > 0) == (score > 0) and avg != 0),
            "strong": strong,
            "weak": rank_stocks([m for m in members if m["code"] not in {x["code"] for x in strong}],
                                False, TOP_N_STOCKS),
        })
    out.sort(key=lambda s: s["score"], reverse=True)
    bull = [s["name"] for s in out if s["score"] > 0][:TOP_N_SECTORS]
    bear = [s["name"] for s in sorted(out, key=lambda s: s["score"]) if s["score"] < 0][:TOP_N_SECTORS]
    return out, bull, bear


# ───────────────────────── 輸出 ─────────────────────────
def write_outputs(payload):
    hist = DATA_DIR / "history"
    hist.mkdir(parents=True, exist_ok=True)
    key = f"{payload['run_date']}_{payload['session']}"
    (DATA_DIR / "latest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (hist / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    idx_file = hist / "index.json"
    idx = json.loads(idx_file.read_text(encoding="utf-8")) if idx_file.exists() else []
    idx = [k for k in idx if k != key] + [key]
    idx = sorted(idx)[-HISTORY_KEEP:]
    for f in hist.glob("*_*.json"):
        if f.stem not in idx:
            f.unlink()
    idx_file.write_text(json.dumps(idx), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=["day", "night"])
    ap.add_argument("--fixtures")
    a = ap.parse_args()

    now = datetime.now(TPE)
    session = a.session or ("night" if now.hour < 12 else "day")
    src = FixtureSource(a.fixtures) if a.fixtures else Source()

    board = commodity_board(src)
    ok = sum(c["status"] == "ok" for c in board)
    print(f"陸期：{ok}/{len(board)} 個商品取得報價")
    if ok < len(board) * 0.5:
        raise SystemExit("陸期報價過少，停止更新以免覆蓋舊資料")

    industry, ind_src = load_industry(src)
    stocks, tw_date = load_quotes(src)
    if session == "day" and tw_date and tw_date < now.strftime("%Y-%m-%d"):
        print(f"⚠ 台股資料日 {tw_date} 早於今天，可能尚未公布或今日休市")
    print(f"台股：{len(stocks)} 檔報價、{len(industry)} 檔產業別（{ind_src}），資料日 {tw_date}")

    sectors, bull, bear = score_sectors(board, industry, stocks)
    payload = {
        "generated_at": now.isoformat(timespec="minutes"),
        "run_date": now.strftime("%Y-%m-%d"),
        "session": session,
        "session_label": "夜盤收盤（台股盤前）" if session == "night" else "日盤收盤",
        "tw_date": tw_date,
        "demo": bool(a.fixtures),
        "params": {"min_lots": MIN_LOTS, "limit_bonus": LIMIT_BONUS},
        "commodities": board, "sectors": sectors, "bull": bull, "bear": bear,
    }
    write_outputs(payload)
    print("多頭：", bull, "空頭：", bear)


if __name__ == "__main__":
    main()
