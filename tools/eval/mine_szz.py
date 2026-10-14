"""SZZ-lite on this repo: fix commits -> the commits that introduced the lines the fix removed/changed. Read-only git, no network, no LLM."""
import collections
import json
import re
import subprocess
import sys

from devorch.security.secret_redactor import has_secret_literal

REPO = sys.argv[1] if len(sys.argv) > 1 else "."


def git(*args, check=True):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "replace"))
    return r.stdout.decode("utf-8", "replace")


def is_test(path):
    return path.startswith("tests/") or "/tests/" in path or path.endswith(("_test.py", "test_.py")) or path.split("/")[-1].startswith("test_")


def is_src(path):
    return path.endswith(".py") and not is_test(path)


log = git("log", "--format=%H\x1f%h\x1f%s\x1f%cI", "--reverse").strip().splitlines()
commits = [l.split("\x1f") for l in log]
info = {h: (s, d) for _, h, s, d in commits}
full = {h: f for f, h, _, _ in commits}
print(f"{len(commits)} commits, first {commits[0][1]} {commits[0][2][:50]!r}")

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
intro = collections.defaultdict(lambda: {"fixes": collections.defaultdict(int)})

for f_full, f_short, subject, _ in commits:
    if not re.match(r"^(fix|bugfix|hotfix)\b", subject, re.I):
        continue
    parent = git("rev-parse", f"{f_full}^", check=False).strip()
    if not parent:
        continue
    diff = git("diff", "-U0", f"{parent}..{f_full}", "--", "*.py")
    path = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            path = line[4:].removeprefix("a/")
        elif line.startswith("+++ ") or line.startswith("diff "):
            continue
        else:
            m = HUNK.match(line)
            if not m or path in (None, "/dev/null") or not is_src(path):
                continue
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            if count == 0:
                continue  # pure addition: nothing removed to blame
            for n in range(start, start + count):
                out = git("blame", "-w", "-M", "-C", "--porcelain", "-L", f"{n},{n}", parent, "--", path, check=False)
                head = out.split("\n", 1)[0].split(" ")[0] if out else ""
                if len(head) == 40 and head != "0" * 40:
                    short = head[:7]
                    if short != f_short:
                        intro[short]["fixes"][f_short] += 1

rows = []
for short, d in intro.items():
    if short not in info:
        continue
    fix_total = sum(d["fixes"].values())
    names = git("show", "--name-only", "--format=", short).split()
    src_files = [n for n in names if is_src(n)]
    diff = git("diff", f"{short}~1..{short}", check=False)
    size = len(diff.encode("utf-8"))
    rows.append({
        "intro": short, "intro_subject": info[short][0][:70], "date": info[short][1][:10],
        "fixes": dict(d["fixes"]), "lines_fixed": fix_total, "src_files": len(src_files), "files": len(names),
        "diff_bytes": size, "secret_literal": has_secret_literal(diff),
        "in_size_range": 1000 <= size <= 20000,
    })
rows.sort(key=lambda r: (-r["lines_fixed"], r["intro"]))
json.dump(rows, open(sys.argv[2] if len(sys.argv) > 2 else "szz_candidates.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"{len(rows)} candidate introducing commits")
SP_OUT = (sys.argv[2] if len(sys.argv) > 2 else "szz_candidates.json").replace(".json", ".txt")
with open(SP_OUT, "w", encoding="utf-8") as _o:
    for r in rows:
        flag = ("" if r["in_size_range"] else " [size]") + (" [SECRET]" if r["secret_literal"] else "")
        fixes = ",".join(f"{k}x{v}" for k, v in r["fixes"].items())
        _o.write(f'{r["intro"]} {r["date"]} {r["diff_bytes"]:>6}B src={r["src_files"]}/{r["files"]} fixed_lines={r["lines_fixed"]:>3} by [{fixes}]{flag}  {r["intro_subject"]}\n')
print("report written to", SP_OUT)
