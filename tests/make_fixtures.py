"""產生離線測試用的假行情（格式仿新浪、證交所、櫃買 OpenAPI），數值為隨機示範。"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from mapping import COMMODITIES  # noqa: E402

random.seed(28)
out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "fixtures")
out.mkdir(parents=True, exist_ok=True)

# 陸期：EG 漲停示範，其餘隨機
lines = []
for ex, code, name, limit, _ in COMMODITIES:
    base = random.uniform(1000, 9000)
    chg = limit if code == "EG" else random.gauss(0, limit / 4)
    last = round(base * (1 + chg / 100), 1)
    f = [f"{name}连续", "150000", base, last * 1.01, base * 0.99, base, last, last, last, last,
         round(base, 1), 10, 10, 100000, 50000, "", f"{name}", "2026-10-28"]
    lines.append(f'var hq_str_nf_{code}0="{",".join(map(str, f))}";')
(out / "sina.txt").write_text("\n".join(lines), encoding="utf-8")

SECTORS = {
    "03": ["1301 台塑", "1303 南亞", "1326 台化", "1304 台聚", "1305 華夏", "1308 亞聚", "1309 台達化",
           "1310 台苯", "1312 國喬", "1313 聯成", "1314 中石化", "1325 恆大"],
    "10": ["2002 中鋼", "2006 東和鋼鐵", "2014 中鴻", "2015 豐興", "2023 燁輝", "2027 大成鋼",
           "2031 新光鋼", "2009 第一銅", "2010 春源", "2020 美亞"],
    "15": ["2603 長榮", "2609 陽明", "2615 萬海", "2605 新興", "2606 裕民", "2637 慧洋-KY",
           "2612 中航", "2617 台航"],
    "06": ["1605 華新", "1609 大亞", "1608 華榮", "1603 華電", "1604 聲寶", "1611 中電", "1612 宏泰"],
    "02": ["1201 味全", "1216 統一", "1215 卜蜂", "1210 大成", "1219 福壽", "1227 佳格", "1229 聯華"],
    "04": ["1402 遠東新", "1409 新纖", "1440 南紡", "1444 力麗", "1476 儒鴻", "1477 聚陽", "1434 福懋"],
    "23": ["6505 台塑化", "9908 大台北", "9918 欣天然", "9926 新海", "9937 全國", "8926 台汽電"],
    "21": ["1710 東聯", "1722 台肥", "1717 長興", "1711 永光", "1708 東鹼", "1723 中碳", "4722 國精化"],
    "11": ["2105 正新", "2106 建大", "2103 台橡", "2104 國際中橡", "2108 南帝", "2109 華豐"],
    "08": ["1802 台玻", "1806 冠軍", "1809 中釉", "1810 和成", "1817 凱撒衛"],
    "09": ["1905 華紙", "1907 永豐餘", "1904 正隆", "1906 寶隆", "1909 榮成"],
    "01": ["1101 台泥", "1102 亞泥", "1103 嘉泥", "1104 環泥", "1108 幸福", "1109 信大", "1110 東泥"],
    "28": ["2383 台光電", "6274 台燿", "6213 聯茂", "8358 金居", "2316 楠梓電"],
    "14": ["2501 國建", "2542 興富發", "5534 長虹", "2548 華固", "2520 冠德"],
    "12": ["2201 裕隆", "2207 和泰車", "2204 中華", "1319 東陽", "1522 堤維西"],
    "26": ["3576 聯合再生", "6244 茂迪", "3708 上緯投控", "6443 元晶", "3481 群創"],
}
OTC = {"6274", "8358", "6244", "6443", "4722", "5534", "8926"}
tw_q, tw_i, ot_q, ot_i = [], [], [], []
for ind, items in SECTORS.items():
    for it in items:
        code, name = it.split(" ", 1)
        close = round(random.uniform(15, 400), 2)
        chg = round(close * random.gauss(0.002, 0.025), 2)
        vol = random.choice([50_000, 300_000, 1_500_000, 8_000_000, 30_000_000])
        if code in OTC:
            ot_q.append({"Date": "1151028", "SecuritiesCompanyCode": code, "CompanyName": name,
                         "Close": str(close), "Change": f"{chg:+.2f}", "TradingShares": f"{vol:,}"})
            ot_i.append({"SecuritiesCompanyCode": code, "SecuritiesIndustryCode": ind})
        else:
            tw_q.append({"Date": "1151028", "Code": code, "Name": name, "ClosingPrice": str(close),
                         "Change": f"{chg:+.2f}", "TradeVolume": str(vol)})
            tw_i.append({"公司代號": code, "產業別": ind})
# 讓產業別筆數超過快取門檻
for k in range(600):
    tw_i.append({"公司代號": f"9{k:04d}", "產業別": "20"})
NAMES = {"02": "食品工業", "21": "化學工業", "23": "油電燃氣業", "26": "光電業", "28": "電子零組件業", "14": "建材營造業"}
isin = "".join(f"<tr><td>{r['SecuritiesCompanyCode']}\u3000x</td><td>TW</td><td>2000/01/01</td><td>上櫃</td>"
               f"<td>{NAMES[r['SecuritiesIndustryCode']]}</td><td>ES</td><td></td></tr>" for r in ot_i)
(out / "isin_otc.html").write_text(f"<table>{isin}</table>", encoding="utf-8")
for name, data in [("twse_quotes", tw_q), ("twse_info", tw_i), ("tpex_quotes", ot_q)]:
    (out / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
print("fixtures →", out)
