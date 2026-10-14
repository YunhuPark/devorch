"""Pilot step for docs/EVAL_DESIGN.md: for merged fix PRs, trace the commit that introduced the buggy lines (SZZ) and report whether it is
a reviewable diff. Read-only on the clone (no checkout), no network except `gh`, no LLM.

    python tools/eval/pilot_pr.py <clone dir> <owner/repo> <out.json> <PR number> [<PR number> ...]

Label verification (does the fix PR's test fail before the fix?) is a separate, manual-ish step: see ``verify_label.ps1`` notes in the
design document. Nothing here proves a label; it only proposes candidates.
"""

from __future__ import annotations

import collections
import json
import re
import subprocess
import sys

from devorch.security.secret_redactor import has_secret_literal

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, cwd=cwd)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {r.stderr.decode('utf-8', 'replace')[:300]}")
    return r.stdout.decode("utf-8", "replace")


def is_test(path: str) -> bool:
    name = path.split("/")[-1]
    return "tests/" in path or "/test" in path or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def is_src(path: str) -> bool:
    return path.endswith(".py") and not is_test(path)


def analyse_pr(repo: str, slug: str, number: int) -> dict:
    g = lambda *a, **k: run(["git", "-C", repo, *a], **k)  # noqa: E731
    meta = json.loads(run(["gh", "pr", "view", str(number), "--repo", slug, "--json", "mergeCommit,mergedAt,title"]))
    merge = (meta.get("mergeCommit") or {}).get("oid")
    out: dict = {"pr": number, "title": meta["title"], "merged": meta["mergedAt"][:10], "merge_commit": (merge or "")[:10]}
    if not merge:
        return {**out, "skip": "no merge commit"}
    parent = g("rev-parse", f"{merge}^1", check=False).strip()
    if not parent:
        return {**out, "skip": "merge commit has no parent"}
    diff = g("diff", "-U0", f"{parent}..{merge}")
    counts: dict[str, int] = collections.Counter()
    path = None
    removed_total = 0
    for line in diff.splitlines():
        if line.startswith("--- "):
            path = line[4:].removeprefix("a/")
        elif line.startswith(("+++ ", "diff ")):
            continue
        else:
            m = HUNK.match(line)
            if not m or not path or path == "/dev/null" or not is_src(path):
                continue
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            for n in range(start, start + count):
                removed_total += 1
                blame = g("blame", "-w", "-M", "-C", "--porcelain", "-L", f"{n},{n}", parent, "--", path, check=False)
                head = blame.split("\n", 1)[0].split(" ")[0] if blame else ""
                if len(head) == 40 and set(head) != {"0"}:
                    counts[head] += 1
    out["removed_lines"] = removed_total
    if not counts:
        return {**out, "skip": "the fix only adds lines (nothing to trace)"}
    cands = []
    for sha, n in counts.most_common(3):
        parents = g("rev-list", "--parents", "-n", "1", sha).split()[1:]
        d = g("diff", f"{sha}^1..{sha}", check=False) if parents else ""
        files = g("show", "--name-only", "--format=", sha).split()
        cands.append({
            "intro": sha[:10], "lines": n, "date": g("log", "-1", "--format=%cs", sha).strip(), "subject": g("log", "-1", "--format=%s", sha).strip()[:80],
            "is_merge": len(parents) > 1, "root": not parents, "diff_bytes": len(d.encode("utf-8")), "files": len(files),
            "py_files": len([f for f in files if f.endswith(".py")]), "secret": has_secret_literal(d),
            "days_before_fix": (int(g("log", "-1", "--format=%ct", merge)) - int(g("log", "-1", "--format=%ct", sha))) // 86400,
        })
    top = cands[0]
    top["reviewable"] = (not top["is_merge"] and not top["root"] and 1000 <= top["diff_bytes"] <= 20000 and not top["secret"])
    return {**out, "candidates": cands, "intro_share": round(cands[0]["lines"] / removed_total, 2)}


def main() -> None:
    repo, slug, out_path, numbers = sys.argv[1], sys.argv[2], sys.argv[3], [int(x) for x in sys.argv[4:]]
    results = []
    for n in numbers:
        try:
            results.append(analyse_pr(repo, slug, n))
        except Exception as e:  # keep going: one odd PR must not hide the rest
            results.append({"pr": n, "skip": f"error: {e}"})
    json.dump(results, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    lines = []
    for r in results:
        if "skip" in r:
            lines.append(f'#{r["pr"]} SKIP {r["skip"]}  {r.get("title", "")[:50]}')
            continue
        c = r["candidates"][0]
        flag = "REVIEWABLE" if c["reviewable"] else "not-reviewable"
        lines.append(
            f'#{r["pr"]} {r["merged"]} fixed={r["removed_lines"]}L intro={c["intro"]} share={r["intro_share"]} '
            f'{c["date"]} ({c["days_before_fix"]}d earlier) {c["diff_bytes"]}B files={c["files"]} merge={c["is_merge"]} {flag}  {r["title"][:48]}'
        )
    with open(out_path.replace(".json", ".txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote", out_path.replace(".json", ".txt"))


if __name__ == "__main__":
    main()
