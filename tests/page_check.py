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
        b.close()
    srv.shutdown()
    if problems:
        for x in problems:
            print("::error title=頁面檢查::" + x)
        sys.exit(1)
    print("頁面檢查通過")


if __name__ == "__main__":
    main()
