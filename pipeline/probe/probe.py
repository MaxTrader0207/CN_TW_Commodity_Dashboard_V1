import requests, json, re
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}
def show(label, url, n=900, **h):
    try:
        r = requests.get(url, headers={**UA, **h}, timeout=30)
        r.encoding = r.apparent_encoding
        t = r.text
        print(f"== {label} HTTP {r.status_code} {r.headers.get('content-type')} len={len(t)}")
        i = t.find("涨跌停")
        print(t[:n] if i < 0 else t[max(0, i - 300): i + n])
        print()
    except Exception as e:
        print(f"== {label} ERR {e}\n")
show("SHFE trade args", "https://www.shfe.com.cn/data/busiparamdata/future/ContractDailyTradeArgument20261029.dat")
show("SHFE trade args2", "https://www.shfe.com.cn/data/instrument/ContractDailyTradeArgument20261029.dat")
show("9qihuo", "https://www.9qihuo.com/qihuoshouxufei")
show("jiaoyixingqiu", "https://www.jiaoyixingqiu.com/shouxufei")
show("qhsxf", "http://www.qhsxf.com/")
show("sina AU", "https://hq.sinajs.cn/list=nf_AU0", 400, Referer="https://finance.sina.com.cn/")
show("sina AU kline", "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/InnerFuturesNewService.getDailyKLine?symbol=AU0", 300, Referer="https://finance.sina.com.cn/")
show("yahoo GC", "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1d", 300)
