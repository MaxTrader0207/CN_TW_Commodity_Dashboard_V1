import requests, json, re
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}
def show(label, url, n=900, method="get", **kw):
    try:
        r = requests.request(method, url, headers=UA, timeout=30, **kw)
        r.encoding = r.apparent_encoding
        t = r.text
        print(f"== {label} HTTP {r.status_code} {r.headers.get('content-type')} len={len(t)}")
        for kwd in ("涨跌停", "UPPER_VALUE", "涨停板"):
            i = t.find(kwd)
            if i >= 0:
                print(t[max(0, i - 400): i + n]); break
        else:
            print(t[:n])
        print()
        return t
    except Exception as e:
        print(f"== {label} ERR {e}\n")
t = show("SHFE codes", "https://www.shfe.com.cn/data/busiparamdata/future/ContractDailyTradeArgument20261029.dat", 10)
if t:
    j = json.loads(t)["ContractDailyTradeArgument"]
    print("SHFE prefixes:", sorted({re.match(r"[a-z]+", x["INSTRUMENTID"]).group(0) for x in j}))
show("INE", "https://www.ine.cn/data/busiparamdata/future/ContractDailyTradeArgument20261029.dat", 600)
show("ZCE clear params", "http://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20261029/FutureDataClearParams.htm", 1500)
show("ZCE clear params txt", "http://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20261029/FutureDataClearParams.txt", 1500)
show("DCE trade params", "http://www.dce.com.cn/dcereport/publicweb/tradepara/dayTradPara", 1500, method="post",
     data={"tradeDate": "20261029", "varietyId": "all", "tradeType": "1", "lang": "zh"})
show("DCE trade params2", "http://www.dce.com.cn/publicweb/businessdata/tradingPara.html", 1500)
show("GFEX trade params", "http://www.gfex.com.cn/u/interfacesWebTiTradeParameter/loadList", 1500, method="post",
     data={"trade_date": "20261029", "variety": ""})
