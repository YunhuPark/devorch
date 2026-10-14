"""Label check for docs/EVAL_DESIGN.md: does the fix PR's own test fail before the fix and pass after it?

    python tools/eval/verify_label.py <clone dir> <test python> <work dir> <merge commit> [<intro commit>]

Runs the test files the fix changed, in throw-away worktrees under <work dir>, at: (1) the fix's parent (bug present) + the PR's tests,
(2) the merge commit (fixed), (3) optionally the introducing commit + the PR's tests. It runs a third party's tests with the given
interpreter, with credential-looking environment variables removed; it is NOT a sandbox. Prints a one-line verdict per state and writes
the pytest tail of each run next to <work dir> so a human can read WHY it failed (an ImportError is not a detected bug).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SENSITIVE = re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|credential|authorization)")


def git(repo: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:300])
    return r.stdout.decode("utf-8", "replace")


def is_test(p: str) -> bool:
    n = p.split("/")[-1]
    return p.endswith(".py") and ("tests/" in p or n.startswith("test_") or n.endswith("_test.py"))


def run_state(repo: str, python: str, work: Path, label: str, rev: str, tests_from: str, tests: list[str]) -> dict:
    wt = work / label
    if wt.exists():
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
    git(repo, "worktree", "add", "--detach", "--force", str(wt), rev)
    try:
        if tests_from != rev:
            git(str(wt), "checkout", tests_from, "--", *tests)  # the PR's tests on top of this state's source
        env = {k: v for k, v in os.environ.items() if not SENSITIVE.search(k)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # the worktree's own source must win over any installed copy of the project (src layout or flat layout)
        env["PYTHONPATH"] = os.pathsep.join([p for p in (str(wt / "src"), str(wt)) if Path(p).is_dir()])
        r = subprocess.run(
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "-x", "--tb=short", *tests],
            cwd=wt, capture_output=True, env=env, timeout=300,
        )
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        (work / f"{label}.log").write_text(out[-6000:], encoding="utf-8")
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1] if out.strip() else "(no output)"
        kind = "pass" if r.returncode == 0 else ("collect-error" if re.search(r"error(s)? during collection|ImportError|ModuleNotFoundError", out) else "fail")
        return {"label": label, "rc": r.returncode, "kind": kind, "summary": tail[:140]}
    finally:
        git(repo, "worktree", "remove", "--force", str(wt), check=False)


def main() -> None:
    repo, python, work, merge = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]
    intro = sys.argv[5] if len(sys.argv) > 5 else None
    work.mkdir(parents=True, exist_ok=True)
    parent = git(repo, "rev-parse", f"{merge}^1").strip()
    tests = [p for p in git(repo, "diff", "--name-only", f"{parent}..{merge}").split() if is_test(p)]
    if not tests:
        print("no test files in the fix"); return
    print(f"tests: {tests}")
    res = [
        run_state(repo, python, work, f"{merge[:7]}_before_fix", parent, merge, tests),
        run_state(repo, python, work, f"{merge[:7]}_after_fix", merge, merge, tests),
    ]
    if intro:
        try:  # the old state may not even check out/run; that must not hide the two results above
            res.append(run_state(repo, python, work, f"{merge[:7]}_at_intro_{intro[:7]}", intro.strip(), merge, tests))
        except Exception as e:  # noqa: BLE001
            res.append({"label": f"{merge[:7]}_at_intro_{intro[:7]}", "rc": -1, "kind": "error", "summary": str(e)[:120]})
    for r in res:
        print(f'  {r["label"]:<40} {r["kind"]:<13} rc={r["rc"]}  {r["summary"]}')
    ok = res[0]["kind"] == "fail" and res[1]["kind"] == "pass"
    print("VERDICT:", "label supported (fails before, passes after)" if ok else "NOT supported - read the logs")


if __name__ == "__main__":
    main()
