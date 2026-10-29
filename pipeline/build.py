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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapping import BENEFICIARIES, COMMODITIES, INDUSTRY_CODE, INTL, SPREADS  # noqa: E402
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
            "high": num(f[3]), "low": num(f[4]), "settle": num(f[9]), "oi": num(f[13]) if len(f) > 13 else None,
            "time": f[1] if len(f) > 1 else None,
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


NORM = os.environ.get("NORM", "z")   # z：漲跌 ÷ 20 日波動度；limit：漲跌 ÷ 漲跌停幅度（舊）


PAST_RET = {}     # 各商品今天以前最近 19 個交易日的漲跌（多日動能用）
KLINE_DUMP = {}   # 詳細面板用的日K摘要 → docs/data/klines.json
MOMENTUM = {"adopt": False}   # 校準檔決定是否把 5 日動能加進即時計分


def live_sigmas(src):
    """抓各商品日K，用最近 20 個交易日（不含今天）的漲跌估計穩健波動度；同時回傳每日 (收盤, 前結算) 給價差用。
    換月日用快取的換月對照修正；快取之後才發生的換月由穩健估計吸收。"""
    roll = json.loads(history.ROLL_JSON.read_text(encoding="utf-8")) if history.ROLL_JSON.exists() else {}
    sig, pairs_by, ok, err = {}, {}, 0, ""
    PAST_RET.clear()
    today = datetime.now(TPE).date().isoformat()
    for _, code, _, limit, _ in COMMODITIES:
        try:
            rows = history.fetch_kline(src.s, code)
            pairs = history.naive_pairs(rows[-140:])
            for d, b in (roll.get(code) or {}).get("base", {}).items():
                if d in pairs:
                    pairs[d] = (pairs[d][0], b)
            pairs = {d: v for d, v in pairs.items() if d < today}
            pairs_by[code] = pairs
            ret, _ = history.pairs_returns(pairs, limit)
            sd = history.robust_sigma([ret[d] for d in sorted(ret)[-history.Z_WINDOW:]])
            PAST_RET[code] = [ret[d] for d in sorted(ret)[-19:]]
            # 詳細面板用：最近 120 天 收盤、換月修正後漲跌、當日 z（以當天以前 20 日波動度計算）
            rs = history.rolling_sigma(ret)
            close = {r["d"]: float(r["c"]) for r in rows}
            KLINE_DUMP[code] = [[d, close.get(d), round(ret[d], 2) if d in ret else None,
                                 round(ret[d] / rs[d], 2) if d in ret and rs.get(d) else None]
                                for d in sorted(close)[-120:] if d < today]
            if sd:
                sig[code] = sd
                ok += 1
        except Exception as e:  # noqa: BLE001
            err = str(e)[:120]
        time.sleep(0.1)
    record("sina_kline", "新浪財經 陸期日K（20 日波動度）", history.KLINE_URL.format("XX"), "漲跌 ÷ 波動度（z 分數）",
           "ok" if ok == len(COMMODITIES) else "warn" if ok else "fail", ok, None, err if ok < len(COMMODITIES) else "")
    return sig, pairs_by


def commodity_board(src, links_by_code, sigmas=None):
    symbols = [f"nf_{c[1]}0" for c in COMMODITIES]
    quotes, err = {}, ""
    for i in range(0, len(symbols), 30):          # 分批避免網址過長
        try:
            quotes.update(parse_sina(src.sina(symbols[i:i + 30])))
        except Exception as e:  # noqa: BLE001
            err = str(e)[:160]
    ds = sorted({q["date"] for q in quotes.values() if q.get("date")})
    n, total = len(quotes), len(symbols)
    record("sina", "新浪財經 陸期主力連續（上期所／大商所／鄭商所／廣期所）", SINA_URL + "nf_XX0",
           "商品漲跌 → 類股多空分數", "ok" if n == total else "warn" if n else "fail",
           n, ds[-1] if ds else None, err or ("" if n == total else f"{total - n} 個商品缺報價"))
    board = []
    for ex, code, name, limit0, links in COMMODITIES:
        q = quotes.get(code)
        lim = LIVE_LIMITS.get(code) or {}
        limit = lim.get("limit") or limit0
        item = {"ex": ex, "code": code, "name": name, "limit": limit, "limit_default": limit0,
                "limit_src": f"{lim['src']} {lim['date']}" if lim else "預設",
                "links": links_by_code.get(code, []),
                "manual": [{"sector": s, "w": w} for s, w, _ in links]}
        if not q:
            item.update(status="missing", chg=None, signal=0, at_limit=None)
        else:
            chg = q["chg"]
            at_limit = "up" if chg >= limit - 0.1 else "down" if chg <= -(limit - 0.1) else None
            sd = (sigmas or {}).get(code)
            if NORM == "z" and sd:
                past = PAST_RET.get(code, [])
                m5 = (sum(past[-4:]) + chg) / (sd * 5 ** 0.5) if len(past) >= 4 else None
                m20 = (sum(past[-19:]) + chg) / (sd * 20 ** 0.5) if len(past) >= 19 else None
                if MOMENTUM.get("adopt") and m5 is not None:
                    zc = history.combo_z(chg / sd, m5, MOMENTUM.get("combo_w", history.MOM_COMBO_W))
                    sig = max(-1.0, min(1.0, zc / history.Z_FULL)) * (LIMIT_BONUS if at_limit else 1)
                else:
                    sig = history.z_signal(chg, sd, limit, True)
                item.update(sigma=round(sd, 3), z=round(chg / sd, 2), norm="z",
                            m5=None if m5 is None else round(m5, 2), m20=None if m20 is None else round(m20, 2))
            else:
                sig = max(-1.0, min(1.0, chg / limit))
                if at_limit:
                    sig *= LIMIT_BONUS
                item.update(norm="limit")
            item.update(status="ok", last=q["last"], base=q["base"], chg=chg, signal=round(sig, 4),
                        at_limit=at_limit, quote_date=q["date"], quote_time=q.get("time"),
                        settle=q.get("settle"), oi=q.get("oi"))
        board.append(item)
    return board


def spread_board(board, links_by_code, cal, pairs_by=None):
    """價差（加工利潤）：Δ價差 ÷ 產品前一日結算價 × 100；訊號以最近 20 日波動度的 2σ 為滿格
    （抓不到日K時退回校準檔的長期 σ）"""
    q = {c["code"]: c for c in board if c["status"] == "ok"}
    sig_tab = (cal.get("spreads") or {})
    limits = {code: limit for _, code, _, limit, _ in COMMODITIES}
    out = []
    for code, name, legs, links in SPREADS:
        item = {"ex": "價差", "code": code, "name": name, "kind": "spread",
                "legs": [{"code": c, "coef": k} for c, k in legs],
                "links": links_by_code.get(code, []),
                "manual": [{"sector": s, "w": w} for s, w, _ in links]}
        sd = None
        if pairs_by:
            hist = history.spread_returns(pairs_by, legs, limits)
            sd = history.robust_sigma([hist[d] for d in sorted(hist)[-history.Z_WINDOW:]])
        sd = sd or (sig_tab.get(code) or {}).get("sigma")
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


def intl_board(session, board, cal, premarket=False):
    """國際期貨（Yahoo 日線）。
    - 交叉確認：最新價（可能仍在交易中）vs 前一個交易日收盤，與對應陸期比對方向（同向／背離）。
    - 盤前計分（premarket=True，夜盤版 08:10）：用「最後一根已收盤」的美國日線（每根於紐約 17:00 收盤），
      漲跌 ÷ 前 20 日穩健波動度（z 分數），以實測的「美盤 → 次一台股交易日」權重計入類股分數。"""
    status = {(p["code"], p["sector"]): p for p in ((cal.get("intl") or {}).get("pairs") or [])}
    live_links = (cal.get("intl") or {}).get("live_links") or {}
    by_code = {c["code"]: c for c in board}
    now_utc = datetime.now(timezone.utc)
    out, ok, err, scored = [], 0, "", 0
    for code, sym, name, cn, links in INTL:
        item = {"ex": "國際", "code": code, "name": name, "kind": "intl", "symbol": sym, "links": [],
                "cn": cn, "ref_links": [{"sector": sct, "w": w, "why": why,
                                         "status": (status.get((code, sct)) or {}).get("status"),
                                         "rho": (status.get((code, sct)) or {}).get("rho0"),
                                         "t": (status.get((code, sct)) or {}).get("t0")} for sct, w, why in links]}
        try:
            closes, meta = history.fetch_yahoo(session, sym, "3mo")
            ds = sorted(closes)
            if len(ds) < 3:
                raise ValueError("資料不足")
            off = int(meta.get("gmtoffset") or -14400)
            # 已收盤的日線：日期 X 的收盤時間 = X 當地 17:00
            done = [d for d in ds
                    if datetime.fromisoformat(d).replace(hour=17, tzinfo=timezone(timedelta(seconds=off))) <= now_utc]
            last, prev = closes[ds[-1]], closes[ds[-2]]
            chg_live = round((last / prev - 1) * 100, 2)
            asof = meta.get("regularMarketTime")
            item.update(status="ok", last=round(last, 3), chg=chg_live, bar_date=ds[-1],
                        asof=datetime.fromtimestamp(asof, TPE).isoformat(timespec="minutes") if asof else None,
                        signal=0, at_limit=None)
            if len(done) >= 22:
                rets = history.intl_returns({d: closes[d] for d in done})
                rd = sorted(rets)
                if rd and rd[-1] == done[-1]:
                    chg_set = round(rets[rd[-1]], 2)
                    sd = history.robust_sigma([rets[d] for d in rd[-1 - history.Z_WINDOW:-1]])
                    item.update(settled_date=done[-1], chg_settled=chg_set,
                                sigma=round(sd, 3) if sd else None, z=round(chg_set / sd, 2) if sd else None)
                    if premarket and sd and live_links.get(code):
                        # 盤前：以已收盤的美國日線計分，頁面也改顯示這根
                        item.update(chg=chg_set, links=live_links[code],
                                    signal=round(history.z_signal(chg_set, sd), 4), norm="z")
                        scored += 1
            ok += 1
            for c in cn:   # 交叉確認：國際與陸期方向
                ci = by_code.get(c)
                if ci and ci.get("chg") is not None and item["chg"]:
                    ci.setdefault("intl", []).append({"code": code, "name": name, "chg": item["chg"],
                                                      "agree": (item["chg"] > 0) == (ci["chg"] > 0) if ci["chg"] else None})
        except Exception as e:  # noqa: BLE001
            item.update(status="missing", chg=None, signal=0, at_limit=None)
            err = str(e)[:120]
        out.append(item)
        time.sleep(0.2)
    record("yahoo_intl", "Yahoo 國際期貨" + ("（盤前計分＋交叉確認）" if premarket else "（交叉確認）"),
           history.YAHOO.format("XX=F", "3mo"), "隔夜美盤 → 台股" if premarket else "國際價格 vs 陸期方向",
           "ok" if ok == len(INTL) else "warn" if ok else "fail", ok, None,
           (err if ok < len(INTL) else "") + (f"｜{scored} 項計入盤前分數" if premarket else ""))
    print(f"國際：{ok}/{len(INTL)}" + (f"，盤前計分 {scored} 項" if premarket else ""))
    return out


# ───────────────────────── 籌碼面 ─────────────────────────
T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={}&selectType=ALLBUT0999&response=json"
TPEX_INSTI_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=AL&date={}&response=json"
MARGN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={}&selectType=ALL&response=json"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance?date={}&response=json"
MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={}&type=ALLBUT0999&response=json"
VOL_HIST = ROOT / "pipeline" / "cache" / "vol_hist.json"


def _lots(x):
    v = num(x)
    return None if v is None else round(v / 1000)


def _fetch_json(src, url):
    r = src._get(url)
    try:
        return r.json()
    except ValueError:
        raise ValueError(f"非 JSON：{r.text[:80]!r}")


def load_insti(src, d):
    """三大法人買賣超（張）：{代號: {f 外資, t 投信, d 自營, sum 合計}}"""
    out = {}
    j = _fetch_json(src, T86_URL.format(d.replace("-", "")))
    if j.get("stat") != "OK":
        raise ValueError(f"證交所 T86：{j.get('stat')}")
    f = {k: i for i, k in enumerate(j["fields"])}
    for r in j["data"]:
        fx = (_lots(r[f["外陸資買賣超股數(不含外資自營商)"]]) or 0) + (_lots(r[f["外資自營商買賣超股數"]]) or 0)
        out[r[0].strip()] = {"f": fx, "t": _lots(r[f["投信買賣超股數"]]), "d": _lots(r[f["自營商買賣超股數"]]),
                             "sum": _lots(r[f["三大法人買賣超股數"]])}
    n_twse = len(out)
    j = _fetch_json(src, TPEX_INSTI_URL.format(d.replace("-", "%2F")))
    rows = (j.get("tables") or [{}])[0].get("data") or []
    for r in rows:
        out[r[0].strip()] = {"f": _lots(r[10]), "t": _lots(r[13]), "d": _lots(r[22]), "sum": _lots(r[23])}
    return out, n_twse, len(rows)


def load_margin(src, d):
    """融資、融券餘額增減（張）：{代號: {mg 融資增減, st 融券增減, mgb 融資餘額, stb 融券餘額}}"""
    out = {}
    j = _fetch_json(src, MARGN_URL.format(d.replace("-", "")))
    for tb in j.get("tables", []):
        if "融資融券彙總" not in (tb.get("title") or ""):
            continue
        for r in tb.get("data", []):
            a, b, c, e = num(r[5]), num(r[6]), num(r[11]), num(r[12])
            if b is not None:
                out[r[0].strip()] = {"mg": None if a is None else round(b - a), "st": None if c is None or e is None else round(e - c),
                                     "mgb": round(b), "stb": None if e is None else round(e)}
    n_twse = len(out)
    j = _fetch_json(src, TPEX_MARGIN_URL.format(d.replace("-", "%2F")))
    rows = (j.get("tables") or [{}])[0].get("data") or []
    for r in rows:
        a, b, c, e = num(r[2]), num(r[6]), num(r[10]), num(r[14])
        if b is not None:
            out[r[0].strip()] = {"mg": None if a is None else round(b - a), "st": None if c is None or e is None else round(e - c),
                                 "mgb": round(b), "stb": None if e is None else round(e)}
    return out, n_twse, len(rows)


def update_vol_hist(src, stocks, industry, tw_date, keep=10):
    """成交量歷史（張）：今天的寫入快取；上市股缺少的前幾個交易日用證交所 MI_INDEX 回補（上櫃無歷史端點，逐日累積）"""
    hist = json.loads(VOL_HIST.read_text(encoding="utf-8")) if VOL_HIST.exists() else {}
    hist[tw_date] = {c: s["lots"] for c, s in stocks.items() if c in industry}
    # 回補前 5 個交易日（只回補上市）
    d = date.fromisoformat(tw_date)
    need, back = [], d
    while len(need) < 5:
        back -= timedelta(days=1)
        if history.is_tw_trading_day(back, HOLIDAYS):
            need.append(back.isoformat())
        if (d - back).days > 20:
            break
    filled = 0
    for k in need:
        if k in hist and len(hist[k]) > 500:
            continue
        try:
            j = _fetch_json(src, MI_INDEX_URL.format(k.replace("-", "")))
            for tb in j.get("tables", []):
                if "每日收盤行情" not in (tb.get("title") or ""):
                    continue
                f = {x: i for i, x in enumerate(tb["fields"])}
                row = hist.setdefault(k, {})
                for r in tb["data"]:
                    c = r[f["證券代號"]].strip()
                    if c in industry:
                        row[c] = _lots(r[f["成交股數"]])
                filled += 1
            time.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ 成交量回補 {k} 失敗：{e}")
    hist = {k: hist[k] for k in sorted(hist)[-keep:]}
    VOL_HIST.parent.mkdir(parents=True, exist_ok=True)
    VOL_HIST.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return hist, filled


def volume_ratio(hist, code, tw_date):
    prev = [hist[k].get(code) for k in sorted(hist) if k < tw_date][-5:]
    prev = [v for v in prev if v]
    today = (hist.get(tw_date) or {}).get(code)
    if len(prev) < 3 or not today:
        return None
    return round(today / (sum(prev) / len(prev)), 2)


def build_chips(src, stocks, industry, tw_date, codes):
    """顯示中的個股：法人買賣超、量比、融資券增減"""
    insti, margin, hist = {}, {}, {}
    try:
        insti, n1, n2 = load_insti(src, tw_date)
        record("insti", "三大法人買賣超（證交所 T86＋櫃買）", T86_URL.format("YYYYMMDD"), "個股籌碼：外資、投信、自營",
               "ok" if n1 and n2 else "warn", len(insti), tw_date, "" if n1 and n2 else f"上市 {n1}／上櫃 {n2} 筆")
    except Exception as e:  # noqa: BLE001
        record("insti", "三大法人買賣超（證交所 T86＋櫃買）", T86_URL.format("YYYYMMDD"), "個股籌碼：外資、投信、自營",
               "fail", note=str(e)[:160])
    try:
        margin, n1, n2 = load_margin(src, tw_date)
        record("margin", "融資融券（證交所＋櫃買）", MARGN_URL.format("YYYYMMDD"), "個股融資、融券增減",
               "ok" if n1 and n2 else "warn", len(margin), tw_date, "" if n1 and n2 else f"上市 {n1}／上櫃 {n2} 筆")
    except Exception as e:  # noqa: BLE001
        record("margin", "融資融券（證交所＋櫃買）", MARGN_URL.format("YYYYMMDD"), "個股融資、融券增減",
               "fail", note=str(e)[:160])
    try:
        hist, filled = update_vol_hist(src, stocks, industry, tw_date)
        record("volhist", "成交量歷史（量比用）", "pipeline/cache/vol_hist.json", "量比 = 今日量 ÷ 前 5 日均量",
               "ok", len(hist), tw_date, f"本次回補 {filled} 天" if filled else "")
    except Exception as e:  # noqa: BLE001
        record("volhist", "成交量歷史（量比用）", "pipeline/cache/vol_hist.json", "量比", "fail", note=str(e)[:160])
    out = {}
    for c in codes:
        i, m = insti.get(c) or {}, margin.get(c) or {}
        out[c] = {"inst": i.get("sum"), "f": i.get("f"), "t": i.get("t"), "d": i.get("d"),
                  "vr": volume_ratio(hist, c, tw_date) if hist else None,
                  "mg": m.get("mg"), "st": m.get("st"), "mgb": m.get("mgb"), "stb": m.get("stb")}
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


INDUSTRY_META = ROOT / "pipeline" / "cache" / "industry_meta.json"
INDUSTRY_STATE = {}
STALE_DAYS = 7


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

    # 一律與快取合併：新抓到的覆蓋舊的，某一來源失敗時該部分沿用快取（避免單一來源失敗就讓整個市場的股票失去類股）
    cached = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    merged = {**cached, **mapping}
    if len(merged) < 500:
        raise RuntimeError(f"產業別資料不足（{len(merged)} 檔）")
    # 記錄各產業別來源上次成功的日子；失敗且快取超過 STALE_DAYS 天 → 警示（新上市櫃股票會缺類股）
    meta = json.loads(INDUSTRY_META.read_text(encoding="utf-8")) if INDUSTRY_META.exists() else {}
    today = datetime.now(TPE).date()
    stale = []
    for sid, label in (("twse_info", "上市"), ("isin_otc", "上櫃")):
        st = next((x for x in SOURCES if x["id"] == sid), None)
        if st and st["status"] == "ok":
            meta[sid] = today.isoformat()
        elif meta.get(sid):
            age = (today - date.fromisoformat(meta[sid])).days
            st["note"] = (st.get("note") or "") + f"；上次成功 {meta[sid]}（{age} 天前）"
            if age > STALE_DAYS:
                stale.append(f"{label}產業別已 {age} 天未更新")
        elif st:
            stale.append(f"{label}產業別從未成功抓取")
    INDUSTRY_META.parent.mkdir(parents=True, exist_ok=True)
    INDUSTRY_META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    INDUSTRY_STATE.update(last_ok=meta, stale=stale)
    if any(x["id"] in ("twse_info", "isin_otc") and x["status"] == "fail" for x in SOURCES):
        n_cache = len(set(merged) - set(mapping))
        record("industry_cache", "產業別快取（補位）", "pipeline/industry_cache.json",
               "即時來源失敗的部分改用上次成功資料", "fail" if stale else "warn", n_cache,
               note=f"{n_cache} 檔沿用快取" + ("；⚠ " + "、".join(stale) + "，新上市櫃股票可能沒有類股" if stale else ""))
    if len(mapping) >= 500 or not cached:
        CACHE_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=0), encoding="utf-8")
    return merged


def roc_to_iso(s):
    s = str(s or "").strip().replace("/", "")
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


LIVE_LIMITS = {}   # 交易所公告的漲跌停幅度 {code: {limit, date, src}}，run() 開頭載入
HOLIDAYS = {}   # 台股休市日 {日期: 名稱}，run() 開頭載入


def expected_trade_date(now, holidays=None):
    """此刻應該已有的最新台股交易日：交易日 14:30 後為當天，否則往前找最近的交易日（排除週末與證交所公告休市日）。"""
    holidays = HOLIDAYS if holidays is None else holidays
    d = now.date()
    if not (history.is_tw_trading_day(d, holidays) and (now.hour, now.minute) >= (14, 30)):
        d -= timedelta(days=1)
        while not history.is_tw_trading_day(d, holidays):
            d -= timedelta(days=1)
    return d.isoformat()


def china_status(board, now, prev):
    """陸期是否有新報價：與上一次快照的報價（日期、時間、價格）完全相同且已超過 18 小時沒更新 → 視為休市／未開盤。
    也偵測主力換月：今天的「昨結算」與上次快照記下的結算價不同 → 連續主力換了合約。"""
    prev_q = {c["code"]: c for c in (prev or {}).get("commodities", []) if c.get("status") == "ok"}
    same = total = 0
    newest = None
    for c in board:
        if c["status"] != "ok":
            continue
        total += 1
        p = prev_q.get(c["code"])
        if p and p.get("quote_date") == c.get("quote_date") and p.get("quote_time") == c.get("quote_time") \
                and p.get("last") == c.get("last"):
            same += 1
        try:
            t = datetime.strptime(f"{c['quote_date']} {c['quote_time']}", "%Y-%m-%d %H%M%S").replace(tzinfo=TPE)
            newest = max(newest, t) if newest else t
        except (KeyError, TypeError, ValueError):
            pass
        # 換月：上次快照（日盤收盤後）記下的結算價 ≠ 今天報價的昨結算
        ps = p.get("settle") if p else None
        if ps and c.get("base") and p.get("quote_date") != c.get("quote_date") and abs(c["base"] / ps - 1) > 0.0005:
            c["rolled"] = True
    age_h = (now - newest).total_seconds() / 3600 if newest else None
    stale = bool(total and prev_q and same >= total * 0.8 and age_h is not None and age_h > 18)
    rolled = [c["code"] for c in board if c.get("rolled")]
    return {"stale": stale, "unchanged": same, "total": total,
            "newest_quote": newest.isoformat(timespec="minutes") if newest else None,
            "age_hours": round(age_h, 1) if age_h is not None else None, "rolled": rolled}


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
SCORE_HIST = DATA_DIR / "score_history.json"


def update_score_history(payload, skip=False):
    """日盤版：把今天各類股分數記入熱力圖歷史（src=即時），並用類股指數快取補上各日的 β 調整超額報酬"""
    h = json.loads(SCORE_HIST.read_text(encoding="utf-8")) if SCORE_HIST.exists() else {"days": []}
    by = {r["d"]: r for r in h.get("days", [])}
    d = payload.get("tw_date")
    if payload["session"] == "day" and d == payload["run_date"] and not skip:
        by[d] = {"d": d, "src": "即時", "s": {x["name"]: x["score"] for x in payload["sectors"]},
                 "a": by.get(d, {}).get("a") or {}}
    cal = json.loads(CALIBRATION.read_text(encoding="utf-8")) if CALIBRATION.exists() else {}
    excess = history.excess_returns(history.index_returns(history.load_index(), history.load_empty()), cal.get("betas"))
    for r in by.values():
        if not r.get("a") and r["d"] in excess:
            r["a"] = {k: round(v, 3) for k, v in excess[r["d"]].items() if k in r["s"]}
    h["days"] = sorted(by.values(), key=lambda r: r["d"])[-180:]
    SCORE_HIST.write_text(json.dumps(h, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


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
    except SystemExit as e:
        if e.code in (0, None):      # 正常略過（例如盤前版今天已完成），不改動狀態檔
            return
        write_status(now, "-", False, f"SystemExit: {e.code}")
        raise
    except BaseException as e:
        write_status(now, "-", False, f"{type(e).__name__}: {e}"[:300])
        raise


def run(now):
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=["day", "night"])
    ap.add_argument("--fixtures")
    ap.add_argument("--skip-if-done", action="store_true",
                    help="今天已有準時（09:00 前）的夜盤盤前版就不再執行（排程多次嘗試用）")
    a = ap.parse_args()

    session = a.session or ("night" if now.hour < 12 else "day")
    if a.skip_if_done and session == "night":
        f = DATA_DIR / "history" / f"{now.strftime('%Y-%m-%d')}_night.json"
        if f.exists():
            try:
                g = json.loads(f.read_text(encoding="utf-8"))["generated_at"]
                if datetime.fromisoformat(g).hour < 9:
                    print(f"今天的盤前版已在 {g[11:16]} 完成，這次排程略過")
                    raise SystemExit(0)
            except (KeyError, ValueError):
                pass
    src = FixtureSource(a.fixtures) if a.fixtures else Source()

    global HOLIDAYS
    if a.fixtures:
        HOLIDAYS = history.load_holidays()
    else:
        HOLIDAYS, n_h, h_err = history.update_holidays(src.s)
        record("twse_holiday", "證交所 休市日曆", history.HOLIDAY_URL, "判斷台股應有交易日、休市",
               "fail" if h_err and not HOLIDAYS else "warn" if h_err else "ok", len(HOLIDAYS), None,
               h_err or "")
    global LIVE_LIMITS
    codes = {c[1] for c in COMMODITIES}
    if a.fixtures:
        LIVE_LIMITS = {}
    else:
        LIVE_LIMITS, l_st = history.update_limits(src.s, now.date(), codes)
        LIVE_LIMITS = {k: v for k, v in LIVE_LIMITS.items() if k in codes}
        bad = [k for k, v in l_st.items() if not v.startswith("ok")]
        record("limits", "上期所／能源中心／鄭商所 每日漲跌停幅度", history.SHFE_PARAM_URL.format("YYYYMMDD"),
               "判斷漲跌停、異常值門檻（大商所／廣期所用預設值）",
               "ok" if not bad else "warn" if len(bad) < len(l_st) else "fail", len(LIVE_LIMITS), None,
               "；".join(f"{k} {v}" for k, v in l_st.items()))
    today = now.date()
    tw_open_today = history.is_tw_trading_day(today, HOLIDAYS)
    prev_payload = None
    if (DATA_DIR / "latest.json").exists():
        try:
            prev_payload = json.loads((DATA_DIR / "latest.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    links_by_code, weights_mode, cal_meta, cal = load_links()
    MOMENTUM.update(cal.get("momentum") or {})
    print(f"計分權重：{weights_mode}；標準化：{'z 分數（20 日波動度）' if NORM == 'z' else '漲跌停幅度'}")
    sigmas, pairs_by = live_sigmas(src) if (NORM == "z" and not a.fixtures) else ({}, None)
    board = commodity_board(src, links_by_code, sigmas)
    ok = sum(c["status"] == "ok" for c in board)
    print(f"陸期：{ok}/{len(board)} 個商品取得報價")
    cn = china_status(board, now, None if a.fixtures else prev_payload)
    if cn["stale"]:
        print(f"⚠ 陸期報價與上次相同且 {cn['age_hours']} 小時未更新（最新 {cn['newest_quote']}）→ 視為大陸休市，本次不記入命中率")
    if cn["rolled"]:
        print(f"主力換月：{cn['rolled']}（漲跌已用新合約的昨結算計算）")
    if ok < len(board) * 0.5:
        raise RuntimeError("陸期報價過少，停止更新以免覆蓋舊資料")

    industry = load_industry(src)
    stocks, tw_date = load_quotes(src, now)
    print(f"台股：{len(stocks)} 檔報價、{len(industry)} 檔產業別，資料日 {tw_date}")

    board = board + spread_board(board, links_by_code, cal, pairs_by)
    premarket = session == "night"
    if not a.fixtures:
        board = board + intl_board(src.s, board, cal, premarket=premarket)
    attach_beneficiaries(board, stocks, cal)
    sectors, bull, bear = score_sectors(board, industry, stocks)
    chips = {}
    if not a.fixtures and tw_date:
        shown = {x["code"] for sc in sectors for x in sc["strong"] + sc["weak"]}
        shown |= {x["code"] for c in board for x in c.get("stocks", [])}
        chips = build_chips(src, stocks, industry, tw_date, sorted(shown))
        print(f"籌碼：{len(chips)} 檔（法人 {sum(1 for v in chips.values() if v['inst'] is not None)}、"
              f"量比 {sum(1 for v in chips.values() if v['vr'] is not None)}、融資券 {sum(1 for v in chips.values() if v['mg'] is not None)}）")
    payload = {
        "generated_at": now.isoformat(timespec="minutes"),
        "run_date": now.strftime("%Y-%m-%d"),
        "session": session,
        "session_label": "夜盤收盤（台股盤前）" if session == "night" else "日盤收盤",
        "tw_date": tw_date,
        "demo": bool(a.fixtures),
        "weights_mode": weights_mode, "calibration": cal_meta,
        "norm": NORM if sigmas else "limit",
        "momentum": {"adopt": bool(MOMENTUM.get("adopt")), "combo_w": MOMENTUM.get("combo_w")},
        "premarket": {"on": session == "night",
                      "intl_scored": [c["code"] for c in board if c.get("kind") == "intl" and c.get("links")]},
        "industry": INDUSTRY_STATE,
        "all_sectors": sorted(set(INDUSTRY_CODE.values())),
        "market": {"cn": cn, "tw_open_today": tw_open_today,
                   "tw_holiday": HOLIDAYS.get(today.isoformat()),
                   "tw_expected": expected_trade_date(now)},
        "params": {"min_lots": MIN_LOTS, "limit_bonus": LIMIT_BONUS},
        "sources": SOURCES,
        "run_url": os.environ.get("RUN_URL"),
        "commodities": board, "sectors": sectors, "bull": bull, "bear": bear, "chips": chips,
    }
    if not a.fixtures:
        # 命中率追蹤：補類股指數快取、把這次訊號記入帳本並開獎過去的訊號
        try:
            tr = track.update(payload, getattr(src, "s", None), skip_signal=cn["stale"])
            last = tr.get("index_last")
            record("twse_index", "證交所 類股指數（命中率開獎用）", history.IND_URL.format("YYYYMMDD"),
                   "多空類股的實際超額報酬", "ok" if last else "warn", None, last)
        except Exception as e:  # noqa: BLE001
            record("twse_index", "證交所 類股指數（命中率開獎用）", history.IND_URL.format("YYYYMMDD"),
                   "多空類股的實際超額報酬", "fail", note=str(e)[:160])
        try:
            update_score_history(payload, skip=cn["stale"] or not tw_open_today)
        except Exception as e:  # noqa: BLE001
            print("⚠ 分數歷史更新失敗：", e)
    if KLINE_DUMP:
        (DATA_DIR / "klines.json").write_text(json.dumps(
            {"generated_at": payload["generated_at"], "fields": ["日期", "收盤", "漲跌%", "z"], "data": KLINE_DUMP},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_outputs(payload)
    print("多頭：", bull, "空頭：", bear)
    return session


if __name__ == "__main__":
    main()
