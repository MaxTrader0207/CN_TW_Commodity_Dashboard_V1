"""一次性探測：確認歷史資料端點格式（結果寫到 docs/data/probe_log.txt）"""
import requests, json
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}
def show(label, url, **kw):
    try:
        r = requests.get(url, headers={**UA, **kw.pop("headers", {})}, timeout=25)
        print(f"== {label} HTTP {r.status_code} {r.headers.get('content-type')} len={len(r.content)}")
        t = r.text
        print(t[:1200].replace("\n", " | "))
        return t
    except Exception as e:
        print(f"== {label} ERROR {e}")
        return ""
t = show("TWSE MI_INDEX IND json", "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=20261027&type=IND&response=json")
try:
    j = json.loads(t)
    print("keys", list(j.keys()))
    for tb in j.get("tables", []):
        print("TABLE", tb.get("title"), tb.get("fields"), len(tb.get("data", [])))
        for row in tb.get("data", [])[:40]:
            print("   ", row)
except Exception as e:
    print("json parse fail", e)
show("TWSE MI_INDEX IND holiday", "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=20261025&type=IND&response=json")
show("Sina kline EG0", "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_EG0=/InnerFuturesNewService.getDailyKLine?symbol=EG0", headers={"Referer": "https://finance.sina.com.cn/"})
t = show("Sina kline EG0 tail", "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_EG0=/InnerFuturesNewService.getDailyKLine?symbol=EG0", headers={"Referer": "https://finance.sina.com.cn/"})
print("TAIL", t[-600:])
