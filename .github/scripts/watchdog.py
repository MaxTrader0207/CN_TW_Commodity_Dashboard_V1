"""停擺監控：超過 STALE_HOURS 小時沒有成功更新，而且其間有應該執行的排程被錯過 → 開 Issue；恢復後自動關閉。

不依賴每日更新本身（每日更新完全沒跑時，失敗通知不會觸發，靠這裡補上）。
由 watchdog.yml 執行：GitHub 每小時排程＋Cloudflare Worker 每 4 小時觸發，兩邊互為備援。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alert as A  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TPE = timezone(timedelta(hours=8))
STALE_HOURS = 30
GRACE = timedelta(hours=2)          # 排程時間過後給 2 小時完成
SLOTS = [(6, 17, "盤前版"), (17, 10, "日盤版")]   # 台北時間，台股交易日


def trading_day(d: date, holidays) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays


def missed_slots(last_ok: datetime, now: datetime, holidays):
    """last_ok 之後、now 之前（含 2 小時緩衝）應該執行卻沒有成功更新的排程"""
    out = []
    d = last_ok.astimezone(TPE).date()
    while d <= now.astimezone(TPE).date():
        if trading_day(d, holidays):
            for h, m, name in SLOTS:
                t = datetime(d.year, d.month, d.day, h, m, tzinfo=TPE)
                if last_ok < t and t + GRACE <= now:
                    out.append((t, name))
        d += timedelta(days=1)
    return out


def check(last_ok: datetime, now: datetime, holidays):
    """回傳 (是否停擺, 經過小時, 錯過的排程)"""
    age = (now - last_ok).total_seconds() / 3600
    missed = missed_slots(last_ok, now, holidays)
    return age > STALE_HOURS and bool(missed), age, missed


def main():
    latest = ROOT / "docs" / "data" / "latest.json"
    hol_f = ROOT / "pipeline" / "cache" / "tw_holidays.json"
    holidays = json.loads(hol_f.read_text(encoding="utf-8")) if hol_f.exists() else {}
    now = datetime.now(TPE)
    if not latest.exists():
        A.raise_alert("stale", "⚠ 儀表板沒有資料", "data 分支找不到 docs/data/latest.json，請檢查每日更新流程。")
        return
    last_ok = datetime.fromisoformat(json.loads(latest.read_text(encoding="utf-8"))["generated_at"])
    stale, age, missed = check(last_ok, now, holidays)
    print(f"最後成功更新 {last_ok:%Y-%m-%d %H:%M}（{age:.1f} 小時前），錯過排程 {len(missed)} 個 → {'停擺' if stale else '正常'}")
    if stale:
        lst = "\n".join(f"- {t:%m/%d %H:%M} {n}" for t, n in missed[-8:])
        A.raise_alert("stale", f"⚠ 儀表板已 {age:.0f} 小時沒有更新",
                      f"最後成功更新：{last_ok:%Y-%m-%d %H:%M}（台北），已經 {age:.0f} 小時。\n\n錯過的排程：\n{lst}\n\n"
                      "可能原因：Cloudflare Worker 定時觸發失效、GH_TOKEN 過期、GitHub Actions 停用或排程被略過。\n"
                      "處理：到 Actions 頁面手動執行 Daily update，並檢查 Worker 的 Cron triggers 與 GH_TOKEN。")
    else:
        A.resolve("stale", f"{last_ok:%Y-%m-%d %H:%M} 已有成功更新")


if __name__ == "__main__":
    main()
