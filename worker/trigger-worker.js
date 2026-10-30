/**
 * 陸期 × 台股 多空儀表板 — 更新按鈕用的 Cloudflare Worker
 *
 * 儀表板按「立即更新」→ POST 到這個 Worker → Worker 用 GitHub token 觸發 Actions「Daily update」。
 * GitHub token 只存在 Worker 的 Secret，不會出現在網頁上。
 *
 * 需要設定：
 *   Secret  GH_TOKEN   Fine-grained token，只授權 CN_TW_Commodity_Dashboard_V1，權限 Actions: Read and write
 *   變數    REPO       MaxTrader0207/CN_TW_Commodity_Dashboard_V1（可省略，預設此值）
 *   變數    ALLOW_ORIGIN https://maxtrader0207.github.io（可省略，預設此值）
 *
 * 路由：
 *   POST /trigger  觸發一次更新（全域 3 分鐘冷卻：以 GitHub 上最近一次執行為準，進行中也不重複觸發）
 *   GET  /status   回傳最近一次執行的狀態
 *
 * 定時觸發（Cloudflare Cron Triggers，取代不準時的 GitHub 排程；GitHub 排程保留當備援）：
 *   Worker → Settings → Trigger events → Cron triggers 加入下面 SCHEDULES 的 5 個 cron（UTC）。
 *   注意 Cloudflare 的星期 1 = 星期日（和 GitHub 不同），所以一律用英文星期名稱。
 *   cron 字串對不上時，改用觸發時間（UTC 小時）判斷，避免後台格式差異造成漏跑。
 */
const WORKFLOW = "daily.yml";
const COOLDOWN_MS = 3 * 60 * 1000;
const DAILY_MAX = 30;   // 24 小時內最多手動觸發次數（含 Worker 定時觸發），防止被大量呼叫洗爆 Actions 與 data 分支
const NIGHT = { workflow: "daily.yml", inputs: { session: "night-once" } };
const DAY = { workflow: "daily.yml", inputs: { session: "day" } };
const CALIB = { workflow: "calibrate.yml", inputs: {} };
const WATCH = { workflow: "watchdog.yml", inputs: {} };
const SCHEDULES = {
  "17 22 * * SUN-THU": NIGHT,   // 台北 週一~五 06:17 盤前版
  "7 23 * * SUN-THU": NIGHT,    // 台北 週一~五 07:07 盤前版（備援，已完成則略過）
  "10 9 * * MON-FRI": DAY,      // 台北 週一~五 17:10 日盤版
  "20 2 * * SAT": CALIB,        // 台北 週六 10:20 校準
  "41 */4 * * *": WATCH,        // 每 4 小時停擺監控（與 GitHub 每小時排程互為備援）
};
function jobFor(event) {
  if (SCHEDULES[event.cron]) return SCHEDULES[event.cron];
  const h = new Date(event.scheduledTime).getUTCHours();
  return h === 22 || h === 23 ? NIGHT : h === 9 ? DAY : h === 2 ? CALIB : null;
}

function ghFetch(env, path, init = {}) {
  const repo = env.REPO || "MaxTrader0207/CN_TW_Commodity_Dashboard_V1";
  return fetch(`https://api.github.com/repos/${repo}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "cn-tw-dashboard-trigger",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(init.headers || {}),
    },
  });
}

export default {
  async scheduled(event, env, ctx) {
    const job = jobFor(event);
    if (!job || !env.GH_TOKEN) return;
    const body = JSON.stringify({ ref: "main", inputs: job.inputs });
    for (let i = 0; i < 3; i++) {   // GitHub 偶發錯誤時重試
      const r = await ghFetch(env, `/actions/workflows/${job.workflow}/dispatches`, { method: "POST", body });
      if (r.status === 204) return;
      console.log(`dispatch ${job.workflow} 失敗 ${r.status}: ${(await r.text()).slice(0, 200)}`);
      await new Promise(res => setTimeout(res, 5000 * (i + 1)));
    }
  },

  async fetch(req, env) {
    const origin = env.ALLOW_ORIGIN || "https://maxtrader0207.github.io";
    const cors = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Content-Type": "application/json; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "no-store",
      Vary: "Origin",
    };
    const reply = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: cors });

    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    const reqOrigin = req.headers.get("Origin");
    if (reqOrigin && reqOrigin !== origin) return reply({ ok: false, error: "來源網域不允許" }, 403);
    if (!env.GH_TOKEN) return reply({ ok: false, error: "Worker 尚未設定 GH_TOKEN" }, 500);

    const gh = (path, init) => ghFetch(env, path, init);

    const path = new URL(req.url).pathname;

    if (path === "/status" && req.method === "GET") {
      const r = await gh(`/actions/workflows/${WORKFLOW}/runs?per_page=1`);
      if (!r.ok) return reply({ ok: false, error: `GitHub ${r.status}` }, 502);
      const run = (await r.json()).workflow_runs?.[0];
      return reply({
        ok: true,
        run: run && {
          status: run.status, conclusion: run.conclusion, event: run.event,
          created_at: run.created_at, updated_at: run.updated_at, url: run.html_url,
        },
      });
    }

    if (path === "/trigger" && req.method === "POST") {
      // 觸發只接受從儀表板網頁送出的請求（瀏覽器一定會帶 Origin；沒帶的多半是腳本直接呼叫）
      if (reqOrigin !== origin) return reply({ ok: false, error: "只接受從儀表板網頁觸發" }, 403);
      // 24 小時上限
      const since = new Date(Date.now() - 864e5).toISOString().slice(0, 19) + "Z";
      const cnt = await gh(`/actions/workflows/${WORKFLOW}/runs?event=workflow_dispatch&per_page=1&created=%3E${since}`);
      if (cnt.ok && ((await cnt.json()).total_count || 0) >= DAILY_MAX) {
        return reply({ ok: false, error: `24 小時內已觸發 ${DAILY_MAX} 次，已達上限，請稍後再試` }, 429);
      }
      // 全域冷卻：看 GitHub 上最近一次執行（不論是誰、從哪觸發），所有 Worker 實例共用同一個判斷
      const last = await gh(`/actions/workflows/${WORKFLOW}/runs?per_page=1`);
      if (!last.ok) return reply({ ok: false, error: `GitHub ${last.status}` }, 502);
      const run = (await last.json()).workflow_runs?.[0];
      if (run) {
        if (run.status !== "completed") {
          return reply({ ok: false, error: "已有更新正在執行，完成後會自動載入" }, 429);
        }
        const wait = COOLDOWN_MS - (Date.now() - Date.parse(run.created_at));
        if (wait > 0) {
          return reply({ ok: false, error: `剛更新過，請 ${Math.ceil(wait / 1000)} 秒後再試` }, 429);
        }
      }
      const r = await gh(`/actions/workflows/${WORKFLOW}/dispatches`, {
        method: "POST",
        body: JSON.stringify({ ref: "main", inputs: { session: "auto" } }),
      });
      if (r.status !== 204) {
        return reply({ ok: false, error: `GitHub ${r.status}: ${(await r.text()).slice(0, 200)}` }, 502);
      }
      return reply({ ok: true, triggered_at: new Date().toISOString() });
    }

    return reply({ ok: false, error: "not found" }, 404);
  },
};
