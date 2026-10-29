"""資料分支：每日／校準產生的資料存在 `data` 分支，`main` 只放程式碼。

  python pipeline/datastore.py restore          # CI 開始時：把 data 分支的資料放回工作目錄
  python pipeline/datastore.py save "訊息"      # CI 結束（或校準中途存檔）：把資料推回 data 分支

data 分支只包含 DATA_PATHS 這幾個路徑。存檔時一律以「這次執行的工作目錄」為準覆蓋到最新的 data 分支上：
daily／calibrate 共用同一個 concurrency group，不會同時執行，所以不用合併，推送失敗就重新抓最新分支再覆蓋一次。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRANCH = "data"
DATA_PATHS = ["docs/data", "pipeline/cache", "pipeline/industry_cache.json"]
WT = ROOT / ".datawt"


def sh(*cmd, cwd=ROOT, check=False):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)} → {out[-400:]}")
    return r.returncode, out


def remote_exists():
    code, out = sh("git", "ls-remote", "--heads", "origin", BRANCH)
    return code == 0 and bool(out.strip())


def restore():
    if not remote_exists():
        print("data 分支不存在，從空白開始")
        return
    sh("git", "fetch", "--no-tags", "--depth=1", "origin", BRANCH, check=True)
    # 只解出檔案（git archive | tar），不動 main 的 index 與其他檔案；data 路徑在 main 被 .gitignore 忽略
    arc = subprocess.run(["git", "archive", "--format=tar", "FETCH_HEAD"], cwd=ROOT, capture_output=True)
    if arc.returncode:
        raise RuntimeError(arc.stderr.decode()[-400:])
    subprocess.run(["tar", "-x", "-C", str(ROOT)], input=arc.stdout, check=True)
    n = sum(1 for p in DATA_PATHS for f in ((ROOT / p).rglob("*") if (ROOT / p).is_dir() else [ROOT / p]) if f.is_file())
    print(f"已從 data 分支還原 {n} 個資料檔")


def _copy_into(wt: Path):
    for p in DATA_PATHS:
        src, dst = ROOT / p, wt / p
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".gitkeep"))
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _prepare_worktree():
    if WT.exists():
        sh("git", "worktree", "remove", "--force", str(WT))
        shutil.rmtree(WT, ignore_errors=True)
    sh("git", "worktree", "prune")
    if remote_exists():
        sh("git", "fetch", "--no-tags", "--depth=1", "origin", BRANCH, check=True)
        sh("git", "worktree", "add", "--force", "--detach", str(WT), "FETCH_HEAD", check=True)
    else:
        sh("git", "worktree", "add", "--force", "--detach", str(WT), check=True)
        sh("git", "checkout", "--orphan", "tmp-data", cwd=WT, check=True)
        sh("git", "rm", "-rfq", ".", cwd=WT)


def save(msg, attempts=5):
    """把 DATA_PATHS 推到 data 分支；回傳 True／False（失敗時印 ::error annotation）"""
    last = ""
    for i in range(attempts):
        try:
            _prepare_worktree()
            _copy_into(WT)
            sh("git", "add", "-A", "--", *DATA_PATHS, cwd=WT, check=True)
            if sh("git", "diff", "--cached", "--quiet", cwd=WT)[0] == 0:
                print("資料沒有變動，不需推送")
                return True
            sh("git", "commit", "-qm", msg, cwd=WT, check=True)
            code, out = sh("git", "push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=WT)
            if code == 0:
                print(f"✓ 已推到 data 分支：{msg}")
                return True
            last = out
        except RuntimeError as e:
            last = str(e)
        print(f"⚠ 推送 data 分支失敗（{i + 1}/{attempts}）：{last[-300:]}")
        time.sleep(5 * (i + 1))
    print(f"::error title=data branch push::{last[-300:].replace(chr(10), ' ')}")
    return False


def cleanup():
    sh("git", "worktree", "remove", "--force", str(WT))
    shutil.rmtree(WT, ignore_errors=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "restore":
        restore()
    elif cmd == "save":
        ok = save(sys.argv[2] if len(sys.argv) > 2 else "data")
        cleanup()
        sys.exit(0 if ok else 1)
    else:
        print(__doc__)
        sys.exit(2)
