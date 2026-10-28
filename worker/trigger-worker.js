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
 */
const WORKFLOW = "daily.yml";
const COOLDOWN_MS = 3 * 60 * 1000;

export default {
  async fetch(req, env) {
    const repo = env.REPO || "MaxTrader0207/CN_TW_Commodity_Dashboard_V1";
    const origin = env.ALLOW_ORIGIN || "https://maxtrader0207.github.io";
    const cors = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Content-Type": "application/json; charset=utf-8",
    };
    const reply = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: cors });

    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    const reqOrigin = req.headers.get("Origin");
    if (reqOrigin && reqOrigin !== origin) return reply({ ok: false, error: "來源網域不允許" }, 403);
    if (!env.GH_TOKEN) return reply({ ok: false, error: "Worker 尚未設定 GH_TOKEN" }, 500);

    const gh = (path, init = {}) =>
      fetch(`https://api.github.com/repos/${repo}${path}`, {
        ...init,
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "cn-tw-dashboard-trigger",
          "X-GitHub-Api-Version": "2022-11-28",
          ...(init.headers || {}),
        },
      });

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
