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
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapping import BENEFICIARIES, COMMODITIES, INDUSTRY_CODE, SPREADS  # noqa: E402
import history  # noqa: E402
import track  # noqa: E402

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
TWSE_RWD = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=open_data"
TWSE_INFO = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_QUOTES = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
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
        name = "twse_rwd.csv" if url == TWSE_RWD else "isin_otc.html"
        f = self.d / name
        if url == TWSE_RWD and not f.exists():
            raise FileNotFoundError(name)
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def sina(self, symbols):
        return (self.d / "sina.txt").read_text(encoding="utf-8")

    def json(self, url):
        name = {TWSE_QUOTES: "twse_quotes", TWSE_INFO: "twse_info", TPEX_QUOTES: "tpex_quotes",
                TWSE_RWD: "twse_rwd"}[url]
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


CALIBRATION = ROOT / "docs" / "data" / "calibration.json"


def load_links():
    """每日計分用的「商品／價差 → 類股」權重：有校準結果用實測權重，否則用 mapping.py 的手動權重。
    回傳 (links_by_code, 模式, 校準摘要, 完整校準檔)"""
    manual = {code: [{"sector": s, "w": w, "why": why, "src": "手動"} for s, w, why in links]
              for _, code, _, _, links in COMMODITIES}
    manual.update({code: [{"sector": s, "w": w, "why": why, "src": "手動"} for s, w, why in links]
                   for code, _, _, links in SPREADS})
    if os.environ.get("WEIGHTS") == "manual" or not CALIBRATION.exists():
        return manual, "手動", None, {}
    try:
        cal = json.loads(CALIBRATION.read_text(encoding="utf-8"))
        live = cal["live_links"]
        links = {code: live.get(code, manual[code]) for code in manual}
        meta = {"generated_at": cal.get("generated_at"), "period": cal.get("period"),
                "counts": cal.get("counts")}
        return links, "實測", meta, cal
    except Exception as e:  # noqa: BLE001
        print("⚠ 校準檔讀取失敗，改用手動權重：", e)
        return manual, "手動", None, {}


def commodity_board(src, links_by_code):
    symbols = [f"nf_{c[1]}0" for c in COMMODITIES]
    quotes, err = {}, ""
    for i in range(0, len(symbols), 30):          # 分批避免網址過長
        try:
            quotes.update(parse_sina(src.sina(symbols[i:i + 30])))
        except Exception as e:  # noqa: BLE001
            err = str(e)[:160]
    ds = sorted({q["date"] for q in quotes.values() if q.get("date")})
    n, total = len(quotes), len(symbols)
    record("sina", "新浪財經 陸期主力連續（上期所／大商所／鄭商所）", SINA_URL + "nf_XX0",
           "商品漲跌 → 類股多空分數", "ok" if n == total else "warn" if n else "fail",
           n, ds[-1] if ds else None, err or ("" if n == total else f"{total - n} 個商品缺報價"))
    board = []
    for ex, code, name, limit, links in COMMODITIES:
        q = quotes.get(code)
        item = {"ex": ex, "code": code, "name": name, "limit": limit,
                "links": links_by_code.get(code, []),
                "manual": [{"sector": s, "w": w} for s, w, _ in links]}
        if not q:
            item.update(status="missing", chg=None, signal=0, at_limit=None)
        else:
            chg = q["chg"]
            at_limit = "up" if chg >= limit - 0.1 else "down" if chg <= -(limit - 0.1) else None
            sig = max(-1.0, min(1.0, chg / limit))
            if at_limit:
                sig *= LIMIT_BONUS
            item.update(status="ok", last=q["last"], base=q["base"], chg=chg, signal=round(sig, 4),
                        at_limit=at_limit, quote_date=q["date"])
        board.append(item)
    return board


def spread_board(board, links_by_code, cal):
    """價差（加工利潤）：Δ價差 ÷ 產品前一日結算價 × 100；訊號以歷史 2σ 為滿格（σ 來自校準檔）"""
    q = {c["code"]: c for c in board if c["status"] == "ok"}
    sig_tab = (cal.get("spreads") or {})
    out = []
    for code, name, legs, links in SPREADS:
        item = {"ex": "價差", "code": code, "name": name, "kind": "spread",
                "legs": [{"code": c, "coef": k} for c, k in legs],
                "links": links_by_code.get(code, []),
                "manual": [{"sector": s, "w": w} for s, w, _ in links]}
        sd = (sig_tab.get(code) or {}).get("sigma")
        if not all(c in q for c, _ in legs) or not sd:
            item.update(status="missing", chg=None, signal=0, at_limit=None, limit=None)
            out.append(item)
            continue
        prod_base = q[legs[0][0]]["base"]
        delta = sum(k * (q[c]["last"] - q[c]["base"]) for c, k in legs)
        level = sum(k * q[c]["last"] for c, k in legs)
        chg = round(delta / prod_base * 100, 2)
        item.update(status="ok", chg=chg, level=round(level, 1), sigma=sd, limit=round(2 * sd, 3),
                    signal=round(max(-1.0, min(1.0, chg / (2 * sd))), 4), at_limit=None,
                    z=round(chg / sd, 2))
        out.append(item)
    ok = sum(i["status"] == "ok" for i in out)
    print(f"價差：{ok}/{len(out)} 個可計算" + ("" if sig_tab else "（尚無校準 σ，暫不計分）"))
    return out


def attach_beneficiaries(board, stocks, cal):
    """每個商品／價差附上受惠（產品）／受壓（成本）個股的今日表現與歷史實測結果"""
    verdicts = {item: {r["code"]: r for r in rows} for item, rows in (cal.get("stocks") or {}).items()}
    for c in board:
        lst = []
        for code, role, why in BENEFICIARIES.get(c["code"], []):
            st = stocks.get(code)
            v = verdicts.get(c["code"], {}).get(code, {})
            lst.append({"code": code, "role": role, "why": why,
                        "name": st["name"] if st else None,
                        "chg_pct": st["chg_pct"] if st else None,
                        "lots": st["lots"] if st else None,
                        "verdict": v.get("verdict"), "rho0": v.get("rho0"), "rho1": v.get("rho1"),
                        "oos_same": (v.get("oos") or {}).get("same_ok"),
                        "oos_next": (v.get("oos") or {}).get("next_ok")})
        c["stocks"] = lst


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


SOURCES = []


def record(sid, name, url, used_for, status, rows=None, data_date=None, note=""):
    """記錄每個資料來源的更新情況，輸出到 latest.json 供儀表板顯示。"""
    SOURCES.append({"id": sid, "name": name, "url": url, "used_for": used_for, "status": status,
                    "rows": rows, "data_date": data_date, "note": note,
                    "fetched_at": datetime.now(TPE).isoformat(timespec="seconds")})
    print(f"[{status}] {name}：{rows if rows is not None else '-'} 筆 {data_date or ''} {note}")


def rows_date(rows, key="Date"):
    ds = {roc_to_iso(r.get(key)) for r in rows if r.get(key)}
    ds.discard(None)
    return max(ds) if ds else None


def load_industry(src):
    """回傳 {代號: 類股名稱}；上市用證交所 OpenAPI，上櫃用證交所 ISIN 頁；全部失敗時用上次快取。"""
    mapping = {}
    try:
        rows = src.json(TWSE_INFO)
        n0 = len(mapping)
        for row in rows:
            code = pick(row, "公司代號", "Code")
            ind = INDUSTRY_CODE.get(str(pick(row, "產業別", "IndustryCode") or "").strip().zfill(2))
            if code and ind:
                mapping[str(code).strip()] = ind
        record("twse_info", "證交所 上市公司產業別", TWSE_INFO, "上市個股 → 類股", "ok",
               len(mapping) - n0, roc_to_iso(rows[0].get("出表日期")) if rows else None)
    except Exception as e:  # noqa: BLE001
        record("twse_info", "證交所 上市公司產業別", TWSE_INFO, "上市個股 → 類股", "fail", note=str(e)[:160])
    try:
        otc = parse_isin(src.text(ISIN_OTC, "cp950"))
        if not otc:
            raise ValueError("頁面解析不到任何上櫃股票")
        mapping.update(otc)
        record("isin_otc", "證交所 ISIN 上櫃產業別", ISIN_OTC, "上櫃個股 → 類股", "ok", len(otc))
    except Exception as e:  # noqa: BLE001
        record("isin_otc", "證交所 ISIN 上櫃產業別", ISIN_OTC, "上櫃個股 → 類股", "fail", note=str(e)[:160])

    if len(mapping) > 500:
        CACHE_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=0), encoding="utf-8")
        return mapping
    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cached.update(mapping)
        record("industry_cache", "產業別快取（上次成功資料）", "pipeline/industry_cache.json",
               "即時來源失敗時補位", "warn", len(cached), note="即時產業別不足，改用快取")
        return cached
    raise RuntimeError("無產業別資料且無快取")


def roc_to_iso(s):
    s = str(s or "").strip().replace("/", "")
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def expected_trade_date(now):
    """此刻應該已有的最新交易日：平日 14:30 後為當天，否則為前一個平日（未計國定假日）。"""
    d = now.date()
    if not (d.weekday() < 5 and (now.hour, now.minute) >= (14, 30)):
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d.isoformat()


def twse_rwd_rows(src):
    """證交所官網每日收盤（比 OpenAPI 更新得快）。官網回 CSV（open_data）或 JSON，都轉成 OpenAPI 欄位。"""
    import csv
    import io
    text = src.text(TWSE_RWD, "utf-8").lstrip("\ufeff")
    if text.lstrip().startswith("{"):
        j = json.loads(text)
        if j.get("stat") != "OK" or not j.get("data"):
            raise ValueError(f"stat={j.get('stat')}")
        recs = [dict(zip(j["fields"], r), 日期=j.get("date")) for r in j["data"]]
    else:
        recs = list(csv.DictReader(io.StringIO(text)))
    if not recs or "收盤價" not in recs[0]:
        raise ValueError(f"官網格式無法辨識：{text[:80]!r}")
    return [{"Date": r.get("日期"), "Code": r["證券代號"], "Name": r["證券名稱"],
             "ClosingPrice": r["收盤價"], "Change": r["漲跌價差"], "TradeVolume": r["成交股數"]}
            for r in recs]


def load_twse_rows(src):
    """官網與 OpenAPI 都抓，取資料日較新的一份。"""
    cands, errs = [], []
    for label, url, fn in (("官網", TWSE_RWD, lambda: twse_rwd_rows(src)),
                           ("OpenAPI", TWSE_QUOTES, lambda: src.json(TWSE_QUOTES))):
        try:
            rows = fn()
            cands.append((rows_date(rows) or "", label, url, rows))
        except Exception as e:  # noqa: BLE001
            errs.append(f"{label}失敗：{str(e)[:80]}")
    if not cands:
        raise RuntimeError("；".join(errs))
    cands.sort(key=lambda c: c[0], reverse=True)
    d, label, url, rows = cands[0]
    other = "、".join(f"{c[1]} {c[0] or '—'}" for c in cands[1:]) + ("；" if errs and len(cands) > 1 else "")
    return rows, url, f"採用{label}" + (f"（另：{other}{'；'.join(errs)}）" if len(cands) > 1 or errs else "")


def load_quotes(src, now):
    expect = expected_trade_date(now)
    stocks, dates = {}, []
    specs = [
        ("twse_quotes", "證交所 上市個股收盤", "上市", lambda: load_twse_rows(src),
         ("Code", "證券代號"), ("Name", "證券名稱"), ("ClosingPrice", "收盤價"), ("Change", "漲跌價差"),
         ("TradeVolume", "成交股數")),
        ("tpex_quotes", "櫃買中心 上櫃個股收盤", "上櫃", lambda: (src.json(TPEX_QUOTES), TPEX_QUOTES, ""),
         ("SecuritiesCompanyCode", "代號"), ("CompanyName", "名稱"), ("Close", "收盤"), ("Change", "漲跌"),
         ("TradingShares", "TradeVolume", "成交股數")),
    ]
    for sid, name, mkt, fetch, k_code, k_name, k_close, k_chg, k_vol in specs:
        url = TWSE_QUOTES if mkt == "上市" else TPEX_QUOTES
        try:
            rows, url, note = fetch()
            n = 0
            for row in rows:
                code = str(pick(row, *k_code) or "").strip()
                close, chg = num(pick(row, *k_close)), num(pick(row, *k_chg))
                if code and close and chg is not None and close - chg > 0:
                    stocks[code] = {"code": code, "name": str(pick(row, *k_name)).strip(), "close": close,
                                    "chg_pct": round(chg / (close - chg) * 100, 2),
                                    "lots": int((num(pick(row, *k_vol)) or 0) / 1000), "mkt": mkt}
                    n += 1
            d = rows_date(rows)
            dates.append(d)
            stale = bool(d and d < expect)
            if stale:
                note = f"資料日早於應有的 {expect}（尚未公布或休市）" + (f"；{note}" if note else "")
            record(sid, name, url, f"{mkt}個股漲跌 → 前5強／前5弱", "warn" if stale else "ok", n, d, note)
        except Exception as e:  # noqa: BLE001
            record(sid, name, url, f"{mkt}個股漲跌 → 前5強／前5弱", "fail", note=str(e)[:160])
    dates = [d for d in dates if d]
    if not stocks:
        raise RuntimeError("上市櫃行情皆取得失敗")
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
                                   "at_limit": c["at_limit"], "w": l["w"], "why": l["why"], "src": l.get("src", "手動"), "kind": c.get("kind", "commodity"),
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


def write_status(now, session, ok, error=""):
    """不論成功或失敗都寫出，讓儀表板顯示每個資料來源的最新狀態。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    status = {"run_at": now.isoformat(timespec="seconds"), "session": session, "ok": ok,
              "error": error, "run_url": os.environ.get("RUN_URL"), "sources": SOURCES}
    (DATA_DIR / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    now = datetime.now(TPE)
    try:
        session = run(now)
        write_status(now, session, True)
    except BaseException as e:
        write_status(now, "-", False, f"{type(e).__name__}: {e}"[:300])
        raise


def run(now):
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=["day", "night"])
    ap.add_argument("--fixtures")
    a = ap.parse_args()

    session = a.session or ("night" if now.hour < 12 else "day")
    src = FixtureSource(a.fixtures) if a.fixtures else Source()

    links_by_code, weights_mode, cal_meta, cal = load_links()
    print(f"計分權重：{weights_mode}")
    board = commodity_board(src, links_by_code)
    ok = sum(c["status"] == "ok" for c in board)
    print(f"陸期：{ok}/{len(board)} 個商品取得報價")
    if ok < len(board) * 0.5:
        raise RuntimeError("陸期報價過少，停止更新以免覆蓋舊資料")

    industry = load_industry(src)
    stocks, tw_date = load_quotes(src, now)
    print(f"台股：{len(stocks)} 檔報價、{len(industry)} 檔產業別，資料日 {tw_date}")

    board = board + spread_board(board, links_by_code, cal)
    attach_beneficiaries(board, stocks, cal)
    sectors, bull, bear = score_sectors(board, industry, stocks)
    payload = {
        "generated_at": now.isoformat(timespec="minutes"),
        "run_date": now.strftime("%Y-%m-%d"),
        "session": session,
        "session_label": "夜盤收盤（台股盤前）" if session == "night" else "日盤收盤",
        "tw_date": tw_date,
        "demo": bool(a.fixtures),
        "weights_mode": weights_mode, "calibration": cal_meta,
        "params": {"min_lots": MIN_LOTS, "limit_bonus": LIMIT_BONUS},
        "sources": SOURCES,
        "run_url": os.environ.get("RUN_URL"),
        "commodities": board, "sectors": sectors, "bull": bull, "bear": bear,
    }
    if not a.fixtures:
        # 命中率追蹤：補類股指數快取、把這次訊號記入帳本並開獎過去的訊號
        try:
            tr = track.update(payload, getattr(src, "s", None))
            last = tr.get("index_last")
            record("twse_index", "證交所 類股指數（命中率開獎用）", history.IND_URL.format("YYYYMMDD"),
                   "多空類股的實際超額報酬", "ok" if last else "warn", None, last)
        except Exception as e:  # noqa: BLE001
            record("twse_index", "證交所 類股指數（命中率開獎用）", history.IND_URL.format("YYYYMMDD"),
                   "多空類股的實際超額報酬", "fail", note=str(e)[:160])
    write_outputs(payload)
    print("多頭：", bull, "空頭：", bear)
    return session


if __name__ == "__main__":
    main()
