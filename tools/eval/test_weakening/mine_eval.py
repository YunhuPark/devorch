"""Mine the evaluation sets exactly as pre-registered in docs/TEST_WEAKENING_EVAL.md (read-only on the cloned repos; nothing from them is executed)."""
import json
import random
import re
import subprocess
import sys

from devorch.core.analyzer.test_weakening import detect, is_test_file

BASE = r"C:\Users\qkrdb\AppData\Local\Temp\detector-eval"
REPOS = ["black", "click", "marshmallow", "loguru", "poetry", "glances"]
CAP = 1500
SEED = 20261022
K_RE = [
    re.compile(r"\b(skip|skipped|skipping|xfail|disable|disabled|remove|removed|delete|deleted|drop|dropped|ignore|ignored|comment out)\b.{0,40}\b(test|tests|testcase|testcases)\b", re.I),
    re.compile(r"\b(test|tests)\b.{0,40}\b(skip|skipped|xfail|disable|disabled|flaky|removed|deleted)\b", re.I),
]


def git(root, *a):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


out = {"repos": {}, "K_all": [], "F_all": []}
for name in REPOS:
    root = f"{BASE}\\{name}"
    log = git(root, "log", "--no-merges", "--format=@@%H\t%s", "--name-only")
    commits = []
    for block in log.split("@@")[1:]:
        lines = [l for l in block.splitlines()]
        h, _, subject = lines[0].partition("\t")
        files = [l for l in lines[1:] if l.strip()]
        if any(is_test_file(f) for f in files):
            commits.append((h, subject))
        if len(commits) >= CAP:
            break
    examined = flagged = k_n = 0
    for h, subject in commits:
        diff = git(root, "diff", f"{h}~1", h, "--no-color")
        if not diff.strip():
            continue
        examined += 1
        sig = detect(diff)
        is_k = any(r.search(subject) for r in K_RE)
        rec = {"repo": name, "commit": h, "subject": subject[:120], "signals": [s.kind for s in sig]}
        if sig:
            flagged += 1
            out["F_all"].append(rec)
        if is_k:
            k_n += 1
            out["K_all"].append(rec)
    out["repos"][name] = {"test_touching_commits_examined": examined, "flagged": flagged, "K_matches": k_n}
    print(name, out["repos"][name], flush=True)

rng = random.Random(SEED)
K = out["K_all"][:]
F = out["F_all"][:]
rng.shuffle(K)
rng.shuffle(F)
out["K_sample"] = K[:60]
out["F_sample"] = F[:60]
out["totals"] = {"examined": sum(r["test_touching_commits_examined"] for r in out["repos"].values()),
                 "flagged": len(F), "K_matches": len(K), "K_sample": len(out["K_sample"]), "F_sample": len(out["F_sample"])}
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print(json.dumps(out["totals"]))
