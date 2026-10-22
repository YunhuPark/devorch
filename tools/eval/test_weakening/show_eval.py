"""Compact review packets for hand-labelling (read-only on the cloned repos): usage  show_eval.py <set: K|F> <start> <end>"""
import json
import re
import subprocess
import sys

from devorch.core.analyzer.test_weakening import detect, is_test_file

SP = r"C:\Users\qkrdb\AppData\Local\Temp\claude\c--Users-qkrdb-Documents-devorch\1460a6b3-e1d3-4abc-82c8-e5a37ae6d9e5\scratchpad"
BASE = r"C:\Users\qkrdb\AppData\Local\Temp\detector-eval"
data = json.load(open(SP + r"\eval_sets.json", encoding="utf-8"))
rows = data["K_sample"] if sys.argv[1] == "K" else data["F_sample"]
start, end = int(sys.argv[2]), int(sys.argv[3])
KEY = re.compile(r"def test|@pytest|pytest\.(skip|xfail|raises|mark)|\bassert|skip|xfail|\.raises|unittest|fixture", re.I)


def git(root, *a):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


for i in range(start, min(end, len(rows))):
    r = rows[i]
    root = f"{BASE}\\{r['repo']}"
    diff = git(root, "diff", f"{r['commit']}~1", r["commit"], "--no-color")
    sig = [s.detail for s in detect(diff)]
    files, path, cur = {}, "", None
    rem, add = [], []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            path = m.group(2) if m else ""
            files[path] = [0, 0]
            continue
        if line.startswith(("+++", "---", "index ", "@@")):
            continue
        if line[:1] in "+-" and path in files:
            files[path][0 if line[0] == "+" else 1] += 1
            if is_test_file(path) and KEY.search(line):
                (add if line[0] == "+" else rem).append(line[1:].strip()[:110])
    tf = [f"{p}(+{a}/-{d})" for p, (a, d) in files.items() if is_test_file(p)]
    sf = [f"{p}(+{a}/-{d})" for p, (a, d) in files.items() if not is_test_file(p)]
    print(f"### {sys.argv[1]}{i} [{r['repo']} {r['commit'][:8]}] {r['subject']}")
    print("  signals:", sig)
    print("  test files:", ", ".join(tf[:4]) + (f" +{len(tf)-4}" if len(tf) > 4 else ""))
    print("  src files :", ", ".join(sf[:4]) + (f" +{len(sf)-4}" if len(sf) > 4 else "") if sf else "  src files : (none)")
    print("  REMOVED:", rem[:9])
    print("  ADDED  :", add[:9])
