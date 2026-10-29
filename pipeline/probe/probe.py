"""一次性探測（結果寫到 docs/data/probe_log.txt）"""
import requests, json, re
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}
REF = {"Referer": "https://finance.sina.com.cn/"}
def get(url, **h):
    try:
        r = requests.get(url, headers={**UA, **h}, timeout=30)
        return r.status_code, r.headers.get("content-type", ""), r.text
    except Exception as e:
        return 0, "", str(e)
def show(label, url, n=700, **h):
    c, ct, t = get(url, **h)
    print(f"== {label} HTTP {c} {ct} len={len(t)}\n{t[:n]}\n")
    return t
# 9 廣期所
show("sina rt GFEX", "https://hq.sinajs.cn/list=nf_LC0,nf_SI0,nf_PS0,nf_PT0,nf_PD0", **REF)
for sym in ["LC0", "SI0", "PS0", "LC2601", "SI2601", "PS2601"]:
    c, ct, t = get(f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/InnerFuturesNewService.getDailyKLine?symbol={sym}", **REF)
    m = re.search(r"\((\[.*\])\)", t, re.S); rows = json.loads(m.group(1)) if m else []
    print(f"== kline {sym} rows={len(rows)} first={rows[0] if rows else t[:80]!r} last={rows[-1] if rows else ''}\n")
# 8 籌碼
show("TWSE T86", "https://www.twse.com.tw/rwd/zh/fund/T86?date=20261029&selectType=ALLBUT0999&response=json", 900)
show("TWSE MI_MARGN", "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=20261029&selectType=ALL&response=json", 1500)
show("TPEx 3insti openapi", "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading", 900)
show("TPEx margin openapi", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance", 900)
show("TWSE MI_INDEX ALLBUT0999 date", "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=20261027&type=ALLBUT0999&response=json", 300)
show("TWSE STOCK_DAY_ALL date", "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?date=20261027&response=open_data", 300)
show("TPEx daily quotes date (new site)", "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQ?date=2026%2F10%2F27&response=json", 600)
# 10 國際
for sym in ["BZ=F", "CL=F", "HG=F", "ALI=F", "ZS=F", "ZM=F", "ZL=F", "ZC=F", "CT=F", "SB=F", "GC=F", "SI=F", "NG=F", "HO=F", "RB=F", "TIO=F", "LIT", "PL=F"]:
    c, ct, t = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d")
    try:
        j = json.loads(t)["chart"]["result"][0]; meta = j["meta"]
        print(f"== yahoo {sym}: {meta.get('shortName') or meta.get('longName')} {meta.get('currency')} last={meta.get('regularMarketPrice')} prev={meta.get('chartPreviousClose')} tz={meta.get('exchangeTimezoneName')} ts={j.get('timestamp')}")
    except Exception as e:
        print(f"== yahoo {sym}: HTTP {c} {t[:120]!r}")
