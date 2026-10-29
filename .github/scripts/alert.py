"""失敗通知：由 notify.yml 在其他流程結束後執行。只用 GitHub 內建的 Issue 通知，不需要額外的金鑰。"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

REPO = os.environ["GITHUB_REPOSITORY"]
RUN_ID = os.environ["RUN_ID"]
LABEL = "pipeline-failure"
TPE = timezone(timedelta(hours=8))
CORE = {"sina": "新浪陸期報價", "twse_quotes": "證交所上市收盤", "tpex_quotes": "櫃買上櫃收盤",
        "twse_info": "證交所上市產業別", "isin_otc": "ISIN 上櫃產業別", "industry_cache": "產業別快取"}
FAIL = {"failure", "timed_out", "startup_failure"}


DRY = os.environ.get("DRY_RUN") == "1"   # 本機測試：只印出會做的寫入動作


def api(path, method="GET", data=None, ok_fail=False):
    if DRY and method != "GET":
        print(f"[DRY] {method} {path} {json.dumps(data, ensure_ascii=False)[:600]}")
        return {"number": 0}
    cmd = ["gh", "api", "-X", method, path]
    if data is not None:
        cmd += ["--input", "-"]
    r = subprocess.run(cmd, input=json.dumps(data) if data is not None else None, capture_output=True, text=True)
    if r.returncode:
        if ok_fail:
            return None
        raise RuntimeError(f"gh api {method} {path}: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def tpe(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TPE).strftime("%Y-%m-%d %H:%M")


def find_issue(key):
    for it in api(f"repos/{REPO}/issues?labels={LABEL}&state=open&per_page=50"):
        if f"<!-- alert:{key} -->" in (it.get("body") or ""):
            return it
    return None


def raise_alert(key, title, body):
    it = find_issue(key)
    if it:
        api(f"repos/{REPO}/issues/{it['number']}/comments", "POST", {"body": "再次發生：\n\n" + body})
        print(f"已在 #{it['number']} 加留言")
        return
    api(f"repos/{REPO}/labels", "POST", {"name": LABEL, "color": "d73a4a", "description": "自動化流程失敗通知"}, ok_fail=True)
    it = api(f"repos/{REPO}/issues", "POST", {"title": title, "labels": [LABEL],
                                               "body": f"<!-- alert:{key} -->\n{body}\n\n_下次成功執行後會自動關閉。_"})
    print(f"已開 Issue #{it['number']}")


def resolve(key, note):
    it = find_issue(key)
    if it:
        api(f"repos/{REPO}/issues/{it['number']}/comments", "POST", {"body": f"✅ 已恢復：{note}"})
        api(f"repos/{REPO}/issues/{it['number']}", "PATCH", {"state": "closed", "state_reason": "completed"})
        print(f"已關閉 #{it['number']}")


def failure_detail(run):
    lines = []
    for job in api(f"repos/{REPO}/actions/runs/{run['id']}/jobs?per_page=50").get("jobs", []):
        if job.get("conclusion") not in FAIL:
            continue
        steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") in FAIL]
        lines.append(f"- 工作「{job['name']}」失敗於：{'、'.join(steps) or '（未知步驟）'}")
        for a in (api(f"repos/{REPO}/check-runs/{job['id']}/annotations", ok_fail=True) or [])[:6]:
            if a.get("annotation_level") == "failure" or (
                    a.get("annotation_level") == "warning" and "Node.js" not in (a.get("message") or "")):
                msg = (a.get("message") or "").strip().replace("\n", " ")[:300]
                lines.append(f"  - {a.get('title') or a['annotation_level']}：{msg}")
    return "\n".join(lines) or "- （沒有取得失敗步驟，請開執行紀錄查看）"


def check_sources(run):
    """每日更新成功時，檢查這次寫出的 status.json 的核心來源"""
    j = api(f"repos/{REPO}/contents/docs/data/status.json?ref=data", ok_fail=True)
    if not j:
        return
    st = json.loads(base64.b64decode(j["content"]).decode("utf-8"))
    run_at = datetime.fromisoformat(st["run_at"]) if st.get("run_at") else None
    started = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
    if not run_at or not (started - timedelta(minutes=5) <= run_at <= started + timedelta(hours=2)):
        print("這次執行沒有產生新的 status.json（例如盤前版已完成而略過），不檢查來源")
        return
    bad = [f"- {CORE[s['id']]}：{s.get('note') or '失敗'}" for s in st.get("sources", [])
           if s["id"] in CORE and s["status"] == "fail"]
    if bad:
        raise_alert("sources", "⚠ 資料來源異常（核心來源抓取失敗）",
                    f"執行時間：{st['run_at'][:16].replace('T', ' ')}\n執行紀錄：{run['html_url']}\n\n" + "\n".join(bad) +
                    "\n\n儀表板仍會更新，但相關區塊可能沿用舊資料。")
    else:
        resolve("sources", f"{st['run_at'][:16].replace('T', ' ')} 核心來源全部正常")


def main():
    run = api(f"repos/{REPO}/actions/runs/{RUN_ID}")
    name, concl = run["name"], run["conclusion"]
    print(f"{name} #{run['run_number']} {run['event']} {run['head_branch']} → {concl}")
    if run["head_branch"] != "main":
        print("非 main 分支，不通知")
        return
    key = name
    if concl in FAIL:
        raise_alert(key, f"⚠ {name} 執行失敗",
                    f"時間：{tpe(run['run_started_at'])}（台北）\n觸發：{run['event']}\n執行紀錄：{run['html_url']}\n\n"
                    + failure_detail(run))
    elif concl == "success":
        resolve(key, f"{tpe(run['run_started_at'])} 執行成功 {run['html_url']}")
        if name == "Daily update":
            check_sources(run)
    else:
        print(f"結果 {concl}，不處理")


if __name__ == "__main__":
    main()
