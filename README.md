# 陸期 × 台股 多空儀表板

以大陸三大商品交易所（上期所含 INE、大商所、鄭商所）主力合約漲跌，透過「商品 → 三竹類股」關聯權重，每日找出台股 **多頭助攻前 3 類股** 與 **空頭壓力前 3 類股**，並列出每個類股的 **前 5 強／前 5 弱** 個股。

例：大商所乙二醇（EG）漲停 → 塑膠類股（南亞、台化）與化學工業（東聯）分數大幅上升，成為多頭助攻燃料。

## 部署（約 3 分鐘）

1. 在 GitHub 建立新 repo（例：`MaxTrader0207/cn-tw-commodity-dashboard`），把本資料夾內容推上 `main`。
2. Settings → Pages → Source 選 **Deploy from a branch**，Branch `main`、資料夾 `/docs`。
3. Actions 頁籤 → **Daily update** → Run workflow，跑第一次資料。
4. 開啟 `https://<帳號>.github.io/<repo>/`。

之後自動排程（台北時間）：
- 週一～五 **17:10**：日盤收盤版（陸期日盤收盤 + 台股當日收盤）
- 週二～六 **08:10**：夜盤盤前版（陸期夜盤收盤，給台股開盤前參考）；GitHub 排程延遲到 09:00 之後會自動略過
- 每週六 **10:20**：重新校準實測權重與回測（`calibrate.yml`）

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

## 立即更新按鈕

儀表板右上角「⟳ 立即更新」：

- **未設定 Worker**（預設）：開啟 GitHub Actions 頁面，按 Run workflow 後，儀表板會自動偵測並載入新資料。
- **設定 Worker 後**：按一下直接觸發更新，約 1–2 分鐘自動載入，不用離開頁面。

設定 Worker：
1. GitHub → Settings → Developer settings → Fine-grained tokens → 新增 token，Repository access 只選本 repo，權限 **Actions: Read and write**。
2. Cloudflare → Workers → 新建 Worker，貼上 `worker/trigger-worker.js`，部署。
3. Worker → Settings → Variables and Secrets → 新增 Secret `GH_TOKEN` = 上一步的 token。
4. 把 Worker 網址填進 `docs/config.js` 的 `triggerUrl`。

## 資料來源狀態

每次執行都會寫出 `docs/data/status.json`（成功或失敗都會寫），儀表板上方「資料來源狀態」逐一列出：新浪陸期、證交所上市產業別、ISIN 上櫃產業別、證交所上市收盤、櫃買上櫃收盤，各自的狀態（正常／注意／失敗）、筆數、資料日與抓取時間。

## 訊號驗證

- **實測權重**（`pipeline/calibrate.py`）：近 2 年陸期主力日報酬 × 證交所類股指數超額報酬（類股 − 加權指數）。
  每組「商品 → 類股」算同日相關 ρ0 與隔日相關 ρ1；|t| ≥ 2 且前後兩段方向一致才採用，不顯著的權重歸 0，
  對照表外 |t| ≥ 4 的組合列為「數據新增」。結果寫在 `docs/data/calibration.json`，每日計分自動改用實測權重
  （設定環境變數 `WEIGHTS=manual` 可改回手動）。
- **樣本外回測**：前 60% 期間校準、後 40% 期間測試，比較手動 vs 實測權重選出的多空前 3 類股，當日與隔日超額報酬。
- **實盤命中率**（`pipeline/track.py`）：每次訊號記入 `docs/data/track/signals.json`，以下一個台股交易日的類股指數超額報酬開獎，
  彙整在 `docs/data/track.json`。夜盤版 09:00 後、日盤版 15:00 前產生的訊號視為延遲，不計入。

## 價差與受惠個股

- **價差（加工利潤）**（`mapping.py` 的 `SPREADS`）：螺紋鋼／熱卷利潤、PTA／短纖／瓶片加工費、PX／PP／PE 對原油、
  燃料油裂解、玻璃利潤、大豆壓榨、養殖利潤共 13 組。每日變化 = Δ價差 ÷ 產品前一日結算價；訊號以歷史 2σ 為滿格，
  與單一商品一樣經過實測校準才計分。
- **受惠／受壓個股**（`mapping.py` 的 `BENEFICIARIES`）：47 檔，標註「產品」或「成本」角色。校準時以 Yahoo 2 年日線
  （還原權息）計算個股 β 調整報酬與陸期的相關，並分前 60%／後 40% 做樣本外檢驗。
- 輸入資料保存在 `pipeline/cache/calib_inputs.json.gz`，可在本機重現校準結果。
