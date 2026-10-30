"""頁面檢查：用無頭瀏覽器開 docs/index.html，確認沒有 JS 錯誤、主要區塊有畫出來、桌機與手機都沒有橫向破版。

  python tests/page_check.py [docs 資料夾] [標籤]
"""
import functools
import http.server
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs").resolve()
LABEL = sys.argv[2] if len(sys.argv) > 2 else ""


def serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    h = functools.partial(Quiet, directory=str(DOCS))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    srv = serve()
    url = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    problems = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for w, h, name in ((1280, 900, "桌機"), (390, 844, "手機")):
            page = b.new_page(viewport={"width": w, "height": h})
            errs = []
            page.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            page.on("console", lambda m, errs=errs: m.type == "error" and "Failed to load resource" not in m.text
                    and errs.append(m.text))
            page.goto(url)
            page.wait_for_timeout(2500)
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
            page.wait_for_timeout(300)
            r = page.evaluate("""() => ({
              sw: document.documentElement.scrollWidth, cw: innerWidth,
              cards: document.querySelectorAll('#bull .card, #bear .card').length,
              tiles: document.querySelectorAll('#board .tile').length,
              bars: document.querySelectorAll('#bars .bar-track').length,
              time: document.querySelector('#m-time').textContent })""")
            # 詳細面板：點第一個商品格，要能打開且不破版
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = false)")
            page.click("#board .tile")
            page.wait_for_timeout(1200)
            r["detail"] = page.evaluate("""() => ({ on: document.querySelector('#modal').classList.contains('on'),
              title: document.querySelector('#d-title')?.textContent, sw: document.documentElement.scrollWidth })""")
            if not r["detail"]["on"] or not r["detail"]["title"]:
                problems.append(f"{name}：點商品格沒有打開詳細面板")
            if r["detail"]["sw"] > r["cw"] + 1:
                problems.append(f"{name}：詳細面板超出畫面 {r['detail']['sw']}px")
            page.keyboard.press("Escape")
            # 每個項目的詳細面板都要畫得出走勢圖（有 klines.json 時）
            if w == 1280 and (DOCS / "data" / "klines.json").exists():
                miss = page.evaluate("""async () => { const out = [];
                  for (const code of Object.keys(BOARD)) {
                    if (BOARD[code].status !== 'ok') continue;
                    await openDetail(code);
                    if (!document.querySelector('#d-body svg.chart')) out.push(code);
                  }
                  closeDetail(); return out; }""")
                r["no_chart"] = miss
                if miss:
                    problems.append(f"詳細面板沒有走勢圖：{miss}")
            print(f"[{LABEL}{name} {w}px] {r}")
            if errs:
                problems.append(f"{name}：JS 錯誤 {errs[:3]}")
            if r["sw"] > r["cw"] + 1:
                wide = page.evaluate("""() => [...document.querySelectorAll('body *')].filter(e => {
                    if (e.getBoundingClientRect().right <= innerWidth + 1 && e.scrollWidth <= e.clientWidth + 1) return false;
                    for (let a = e.parentElement; a; a = a.parentElement)
                      if (['auto', 'hidden', 'scroll'].includes(getComputedStyle(a).overflowX)) return false;
                    return true; }).slice(0, 5).map(e => e.tagName + '.' + e.className + ' ' + e.textContent.slice(0, 30))""")
                problems.append(f"{name}：頁面寬 {r['sw']}px 超出畫面 {r['cw']}px，可能是 {wide}")
            if r["time"] in ("—", ""):
                problems.append(f"{name}：資料沒有載入（更新時間空白）")
            if not r["tiles"] or not r["bars"]:
                problems.append(f"{name}：商品看板或類股分數沒有畫出來 {r}")
        # 手機版（模擬 iPhone）：自動轉址、每個分頁不破版、詳細頁可開關
        ctx = b.new_context(**p.devices["iPhone 13"])
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(url)
        page.wait_for_url("**/m/**", timeout=5000)
        page.wait_for_selector("#sectors .card, #sectors .empty", timeout=10000)
        res = {}
        for v in ("home", "board", "verify", "codes", "more"):
            page.click(f'#nav button[data-v="{v}"]')
            page.wait_for_timeout(1200 if v == "verify" else 300)
            res[v] = page.evaluate("document.documentElement.scrollWidth - innerWidth")
        page.click('#nav button[data-v="board"]')
        page.click("#board .row")
        page.wait_for_timeout(1000)
        opened = page.evaluate("document.querySelector('#sheet').classList.contains('on')")
        page.go_back()
        page.wait_for_timeout(400)
        closed = page.evaluate("!document.querySelector('#sheet').classList.contains('on')")
        print(f"[{LABEL}手機版 iPhone] 各分頁超出寬度 {res}，詳細頁開 {opened} 關 {closed}")
        if any(x > 1 for x in res.values()):
            problems.append(f"手機版：分頁超出畫面 {res}")
        if not (opened and closed):
            problems.append("手機版：詳細頁無法開啟或返回鍵無法關閉")
        if errs:
            problems.append(f"手機版：JS 錯誤 {errs[:3]}")
        ctx.close()
        b.close()
    srv.shutdown()
    if problems:
        for x in problems:
            print("::error title=頁面檢查::" + x)
        sys.exit(1)
    print("頁面檢查通過")


if __name__ == "__main__":
    main()
