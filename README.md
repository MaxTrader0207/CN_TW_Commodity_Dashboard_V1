# 陸期 × 台股 多空儀表板

以大陸三大商品交易所（上期所含 INE、大商所、鄭商所）主力合約漲跌，透過「商品 → 三竹類股」關聯權重，每日找出台股 **多頭助攻前 3 類股** 與 **空頭壓力前 3 類股**，並列出每個類股的 **前 5 強／前 5 弱** 個股。

例：大商所乙二醇（EG）漲停 → 塑膠類股（南亞、台化）與化學工業（東聯）分數大幅上升，成為多頭助攻燃料。

## 部署（約 3 分鐘）

1. 在 GitHub 建立新 repo（例：`MaxTrader0207/cn-tw-commodity-dashboard`），把本資料夾內容推上 `main`。
2. Settings → Pages → Source 選 **Deploy from a branch**，Branch `main`、資料夾 `/docs`。
3. Actions 頁籤 → **Daily update** → Run workflow，跑第一次資料。
4. 開啟 `https://<帳號>.github.io/<repo>/`。

之後自動排程（台北時間）：
- 週一～五 **15:40**：日盤收盤版（陸期日盤收盤 + 台股當日收盤）
- 週二～六 **08:10**：夜盤盤前版（陸期夜盤收盤，給台股開盤前參考）

## 檔案

| 檔案 | 用途 |
|---|---|
| `pipeline/mapping.py` | 商品 ↔ 類股關聯表（權重、邏輯說明、漲跌停幅度）— 想調整關聯就改這裡 |
| `pipeline/build.py` | 抓資料、計分、輸出 `docs/data/latest.json` 與歷史檔 |
| `docs/index.html` | 儀表板頁面（GitHub Pages） |
| `tests/make_fixtures.py` | 產生離線假資料：`python tests/make_fixtures.py /tmp/fx && python pipeline/build.py --fixtures /tmp/fx` |

## 資料來源

- 陸期：新浪財經期貨行情 `hq.sinajs.cn`（`nf_XX0` 連續主力），漲跌幅以昨結算為基準
- 台股：證交所 OpenAPI `STOCK_DAY_ALL`、`t187ap03_L`；櫃買中心 OpenAPI 上櫃收盤與公司基本資料
- 類股：以證交所／櫃買產業別代碼歸類，對應三竹傳產類股名稱（塑膠、鋼鐵、航運、電器電纜…）

## 計分

- 商品訊號 = 漲跌幅 ÷ 漲跌停幅度，限制在 ±1；觸及漲跌停再 ×1.5
- 類股分數 = Σ(權重 × 商品訊號) ÷ √Σ|權重| × 100
- 共振 = 陸期訊號方向與台股類股當日平均漲跌同向
- 個股排行只計成交量 ≥ 100 張（`MIN_LOTS`）

漲跌停幅度 `limit` 為一般值，交易所會依行情調整，可在 `mapping.py` 更新。
