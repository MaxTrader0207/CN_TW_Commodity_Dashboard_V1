# 資安檢查紀錄

最近一次檢查：2026-10-30（main 與 data 分支全部檔案＋全部 106 個 commit）

## 金鑰掃描

- gitleaks 8.21.2 掃描完整 git 歷史與工作目錄：**無發現**
- 自訂樣式比對（GitHub token `ghp_`/`github_pat_`、AWS、私鑰、Bearer、Slack、OpenAI、`password=` 等）：**無發現**
- commit 作者只有 Claude 與 github-actions bot，未出現個人 Email
- 唯一的金鑰 `GH_TOKEN` 只存在 Cloudflare Worker 的加密 Secret；fine-grained、只授權本 repo、只有 Actions 讀寫、2027-10-29 到期

## 架構與防護

| 元件 | 防護 |
|---|---|
| 網頁（GitHub Pages） | 所有外部資料以 `textContent` 顯示，無 `innerHTML`／`eval`；CSP 只允許自家腳本，只能連線到本站與更新 Worker；網址 `#代碼` 只用來查表 |
| 更新 Worker | 手動觸發須帶管理密碼（Secret `ADMIN_KEY`，至少 20 字元，固定時間比對；未設定則一律拒絕）、須來自儀表板網頁（Origin）、全域 3 分鐘冷卻、24 小時最多 30 次；token 只在 Secret；回應加 `nosniff`、`no-store` |
| GitHub Actions | 每個 workflow 明確最小權限；手動輸入只經環境變數與選單，不嵌入 shell；Actions 鎖定 commit SHA；Python 套件以雜湊鎖定（`pip install --require-hashes`）；CI 檢查 workflow 語法 |
| 失敗通知 | 只處理本 repo `main` 的 push／排程／手動執行（fork PR 無法開關 Issue）；Issue 內容去除 @提及、HTML、反引號 |
| 資料來源 | 異常漲跌剔除、穩健 z 分數、來源狀態與休市偵測；鄭商所改 https 優先；漲跌停幅度須在 1~30% 才採用 |

## 已知殘留風險

- 管理密碼存在擁有者裝置的瀏覽器；裝置遺失或被他人使用時，到 Cloudflare 更換 `ADMIN_KEY` 即可讓舊密碼失效
- 訪客無法手動觸發；Origin 檢查與 24 小時上限仍保留作為第二層防護
- GitHub Pages 無法設定 `frame-ancestors`（防點擊劫持）；頁面唯一的動作是更新按鈕，影響低
- 上游行情（新浪、Yahoo、證交所）若出錯或被竄改，只會影響訊號數字，不會執行任何程式
- `GH_TOKEN` 2027-10-29 到期；到期後更新與排程都會停止，且因為沒有執行，失敗通知也不會發出 → 到期前輪替
- 「完全沒有執行」由停擺監控補上（`watchdog.yml`）：超過 30 小時沒有成功更新、且錯過交易日排程就開 Issue；
  由 GitHub 每小時排程與 Cloudflare Worker 每 4 小時觸發互為備援，頁面也會顯示過時警示

## 建議（需帳號擁有者操作）

1. GitHub、Cloudflare、Google 帳號都開啟兩步驟驗證
2. 不再需要 Claude 操作時，移除 Claude GitHub App 對本 repo 的權限，以及 Claude 桌面版瀏覽器對 dash.cloudflare.com 的永久允許
3. 2027-09 前輪替 `GH_TOKEN`（權限維持：只選本 repo、Actions: Read and write）
