"""一次性探測（結果寫到 docs/data/probe_log.txt）"""
import requests, json, re
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}
REF = {"Referer": "https://finance.sina.com.cn/"}
def get(url, **h):
    try:
        r = requests.get(url, headers={**UA, **h}, timeout=25)
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)
c, t = get("https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule")
print("== TWSE holiday", c, t[:1500])
for sym in ["EG2601", "EG601", "TA2601", "TA601", "SR2601", "SR601", "CU2512", "SC2512", "RB2601", "I2601", "EC2512", "PX2601"]:
    c, t = get(f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/InnerFuturesNewService.getDailyKLine?symbol={sym}", **REF)
    m = re.search(r"\((\[.*\])\)", t, re.S)
    rows = json.loads(m.group(1)) if m else []
    print(f"== kline {sym}: HTTP {c} rows={len(rows)} first={rows[0] if rows else t[:120]!r} last={rows[-1] if rows else ''}")
# 連續主力 vs 個別合約：最近 3 天比對
c, t = get("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/InnerFuturesNewService.getDailyKLine?symbol=EG0", **REF)
m = re.search(r"\((\[.*\])\)", t, re.S); print("== EG0 tail", json.loads(m.group(1))[-3:] if m else t[:100])
c, t = get("https://hq.sinajs.cn/list=nf_EG0,nf_EG2601,nf_EG2605,nf_TA601,nf_TA2601,nf_CU0", **REF)
print("== realtime", t[:1500])
