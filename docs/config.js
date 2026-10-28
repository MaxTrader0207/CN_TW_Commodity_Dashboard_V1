// 儀表板設定
// triggerUrl：更新按鈕使用的 Cloudflare Worker 網址（見 worker/trigger-worker.js）。
//   留空時，按鈕會改為開啟 GitHub Actions 頁面，讓你手動按 Run workflow。
window.DASH_CONFIG = {
  triggerUrl: "https://cn-tw-dashboard-trigger.maxwei6699.workers.dev",
  actionsUrl: "https://github.com/MaxTrader0207/CN_TW_Commodity_Dashboard_V1/actions/workflows/daily.yml",
};
