import requests, time
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36", "Accept": "application/json, text/plain, */*"}
for url in ["https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading",
            "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading",
            "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=2026%2F10%2F29&response=json",
            "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=AL&date=2026%2F10%2F29&response=json",
            "https://www.tpex.org.tw/www/zh-tw/margin/balance?date=2026%2F10%2F29&response=json"]:
    for i in range(2):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            print("==", url, r.status_code, r.headers.get("content-type"), len(r.text)); print(r.text[:900]); break
        except Exception as e:
            print("==", url, "ERR", e); time.sleep(3)
