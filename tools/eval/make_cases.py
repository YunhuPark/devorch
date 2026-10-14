"""Build `devorch bench` cases from the verified candidates of docs/EVAL_DESIGN.md (positives) plus size/era-matched clean controls.

    python tools/eval/make_cases.py <work dir> <out dir>

<work dir> holds the clones (`glances/`, `poetry/`) and the pilot results (`*_pilot.json`). Nothing is sent anywhere: this only reads the
clones and calls `gh pr view` for each fix PR's title/body. The output cases contain third-party source: keep them OUT of the repository.

Positives: the commit that introduced the buggy lines (before/after = its parent/itself, all text files). The defect description is hand-written
(tools/eval/stage1_defects.yaml) for the judge ONLY — the reviewers see just the diff and the commit subject.
Scoring is judge-only: `match_any` is a regex that never matches. Controls: non-merge commits with a Python change, 1-20 KB, no
credential-looking text, not a known bug-introducer, nearest in size to a positive and within a year of its date. "Clean" means "no
known bug", not "no bug": false alarms on them still have to be judged by a person.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from devorch.security.secret_redactor import has_secret_literal

# strong labels only; poetry #10975 (the test needs a helper the fix itself adds) stays out until a person has looked at it
INCLUDE = {
    "glances": ["nicolargo/glances", [3674, 3683, 3684, 3685, 3687]],
    "poetry": ["python-poetry/poetry", [10816, 10885, 10941, 10987, 11037, 11022, 11093]],
}
TRUTH = yaml.safe_load((Path(__file__).parent / "stage1_defects.yaml").read_text(encoding="utf-8"))
# Control rules added AFTER stage 1 (2026-10-14), applied to every control alike: a commit that is itself marked work-in-progress is not a
# "clean" change (glances 7acca16 "First part of the implementation..." had real bugs), and a commit younger than ~6 months has not had time
# to be implicated in a fix. The stage-1 results with the old controls are kept as they were.
REFERENCE_DAY = datetime(2026, 10, 14)
MIN_CONTROL_AGE_DAYS = 180
WIP = re.compile(r"\b(first part|part \d|wip|work in progress|partial|draft|initial implementation|start of|beginning of)\b", re.I)
MAX_FILE_BYTES = 600_000  # whole-file before/after are stored; the reviewed DIFF is limited to 20 KB separately


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    for attempt in range(4):  # git.exe is intermittently refused on this PC (WinError 5, see HANDOFF); same retry as git/manager.py
        try:
            return subprocess.run(cmd, capture_output=True)
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.3 * (attempt + 1))
    raise AssertionError("unreachable")


def git(repo: Path, *args: str) -> bytes:
    r = _run(["git", "-C", str(repo), *args])
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:200])
    return r.stdout


def text_at(repo: Path, rev: str, path: str) -> str | None:
    r = _run(["git", "-C", str(repo), "show", f"{rev}:{path}"])
    if r.returncode:
        return None
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None  # binary: not part of a reviewable text diff


def changed_files(repo: Path, rev: str) -> list[str]:
    return [p for p in git(repo, "show", "--name-only", "--format=", rev).decode("utf-8", "replace").split("\n") if p.strip()]


def files_of(repo: Path, rev: str) -> tuple[dict | None, list[str]]:
    """(files, notes). A deleted file becomes after="" (the diff shows the removal); a binary or oversized file is left out and noted."""
    out, notes = {}, []
    for path in changed_files(repo, rev):
        after, before = text_at(repo, rev, path), text_at(repo, f"{rev}^1", path)
        raw_after = _run(["git", "-C", str(repo), "cat-file", "-e", f"{rev}:{path}"]).returncode == 0
        if after is None and raw_after:
            notes.append(f"{path}: binary, left out")
            continue
        if after is None:  # deleted in this commit
            after = ""
        if before is None and _run(["git", "-C", str(repo), "cat-file", "-e", f"{rev}^1:{path}"]).returncode == 0:
            notes.append(f"{path}: binary before, left out")
            continue
        if len(after.encode("utf-8")) > MAX_FILE_BYTES or (before and len(before.encode("utf-8")) > MAX_FILE_BYTES):
            notes.append(f"{path}: over {MAX_FILE_BYTES} bytes, left out")
            continue
        if before == after:
            continue
        out[path] = {"before": before, "after": after}
    return (out or None), notes


def clean_subject(s: str) -> str:
    return re.sub(r"\s*\(#\d+\)\s*$", "", s).strip()


def main() -> None:
    work, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, (slug, prs) in INCLUDE.items():
        repo = work / name
        pil = {r["pr"]: r for r in json.load(open(work / f"{name}_pilot.json", encoding="utf-8")) if "candidates" in r}
        by_intro: dict[str, list[dict]] = {}
        for n in prs:
            r = pil[n]
            by_intro.setdefault(r["candidates"][0]["intro"], []).append(r)
        known_intros = {r["candidates"][0]["intro"] for r in pil.values()}
        pos_meta = []
        for intro, rs in by_intro.items():
            full = git(repo, "rev-parse", intro).decode().strip()
            files, fnotes = files_of(repo, full)
            if not files:
                print(f"  skip {name} {intro}: nothing representable ({'; '.join(fnotes)})")
                continue
            if fnotes:
                print(f"  note {name} {intro}: {'; '.join(fnotes)}")
            defects = []
            for i, r in enumerate(rs, 1):
                desc = TRUTH[name].get(r["pr"])
                if not desc:
                    raise SystemExit(f"no hand-written defect description for {name} PR {r['pr']} in tools/eval/stage1_defects.yaml")
                defects.append({"id": f"d{i}", "category": "logic", "severity": "high", "description": " ".join(desc.split()),
                                "match_any": ["(?!)"]})  # never matches: scored by the judge only
            subject = clean_subject(git(repo, "log", "-1", "--format=%s", full).decode("utf-8", "replace"))
            cid = f"{name}_{'_'.join(str(r['pr']) for r in rs)}"
            case = {"id": cid, "title": subject, "task": subject, "files": files, "defects": defects,
                    "notes": f"{slug} intro {intro[:10]} ({rs[0]['candidates'][0]['date']}); fixed by PR(s) {[r['pr'] for r in rs]} (label: fix tests fail before / pass after)"}
            (out / f"{cid}.yaml").write_text(yaml.safe_dump(case, allow_unicode=True, sort_keys=False), encoding="utf-8")
            size = len(git(repo, "diff", f"{full}^1..{full}"))
            pos_meta.append((cid, full, rs[0]["candidates"][0]["date"], size))
            manifest.append({"id": cid, "kind": "positive", "repo": slug, "commit": full[:10], "date": rs[0]["candidates"][0]["date"], "diff_bytes": size, "defects": len(defects)})
        # controls: nearest size within a year, unique
        log = git(repo, "log", "--no-merges", "--format=%H %cs", "--since=2016-01-01").decode().splitlines()
        cand = []
        for ln in log:
            sha, day = ln.split()
            cand.append((sha, day))
        used: set[str] = set()
        for cid, pfull, pday, psize in pos_meta:
            best = None
            pd = datetime.fromisoformat(pday)
            for sha, day in cand:
                if sha in used or sha[:10] in known_intros or sha == pfull:
                    continue
                if abs((datetime.fromisoformat(day) - pd).days) > 365:
                    continue
                if (REFERENCE_DAY - datetime.fromisoformat(day)).days < MIN_CONTROL_AGE_DAYS:
                    continue
                if WIP.search(git(repo, "log", "-1", "--format=%s", sha).decode("utf-8", "replace")):
                    continue
                d = git(repo, "diff", f"{sha}^1..{sha}").decode("utf-8", "replace") if git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:] else ""
                n = len(d.encode("utf-8"))
                if not (1000 <= n <= 20000) or has_secret_literal(d):
                    continue
                if not any(p.endswith(".py") and "test" not in p.lower() for p in changed_files(repo, sha)):
                    continue
                gap = abs(n - psize)
                if best is None or gap < best[0]:
                    best = (gap, sha, day, n)
                if gap < 300:
                    break
            if not best:
                print(f"  no control found near {cid}")
                continue
            _, sha, day, n = best
            files, _ = files_of(repo, sha)
            if not files:
                continue
            used.add(sha)
            subject = clean_subject(git(repo, "log", "-1", "--format=%s", sha).decode("utf-8", "replace"))
            ccid = f"{name}_ctl_{sha[:7]}"
            case = {"id": ccid, "title": subject, "task": subject, "files": files, "defects": [],
                    "notes": f"{slug} control {sha[:10]} ({day}); no known bug (matched to {cid} by size/era)"}
            (out / f"{ccid}.yaml").write_text(yaml.safe_dump(case, allow_unicode=True, sort_keys=False), encoding="utf-8")
            manifest.append({"id": ccid, "kind": "control", "repo": slug, "commit": sha[:10], "date": day, "diff_bytes": n, "matched_to": cid})
    json.dump(manifest, open(out.parent / "manifest.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for m in manifest:
        print(f'{m["kind"]:<9} {m["id"]:<28} {m["date"]} {m["diff_bytes"]:>6}B')


if __name__ == "__main__":
    main()
