"""Run `devorch review` with REAL reviewers over a fixed list of commits and collect the cost (estimate vs actual) and the findings.

One command for the person who holds the API key, so the numbers come from many calls instead of one:

    $env:OPENAI_API_KEY = (Get-Clipboard -Raw).Trim()          # PowerShell; the key stays in this window's environment only
    python tools\\eval\\real_review_batch.py --project <repo> --commits 2f3e523,3bc10a9 --max-total-usd 0.40 --out results.json

The key is read by `devorch review` from the environment; this script never reads, prints or stores it. Reviews stop before the next call would pass
the total cap. Add --mock for a free dry run (no key, no cost). Commits are reviewed at --level medium (one reviewer: OpenAI). Diffs that contain
credential-looking strings are refused by devorch itself and show up as failures here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def parse_json(text: str) -> dict | None:
    i = text.find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except ValueError:
        return None


def _item(f: dict) -> dict:
    """What a finding says (so a human can check it against the code): title, severity, file, which reviewer. Never any key or prompt text."""
    return {k: f.get(k) for k in ("severity", "title", "file", "source", "category") if f.get(k) is not None}


def review_one(project: str, commit: str, level: str, mock: bool, python: str, task: str) -> dict:
    cmd = [python, "-m", "devorch", "review", "-p", project, "--git-range", f"{commit}~1..{commit}", "--task", task, "--level", level, "--yes", "--json"]
    if mock:
        cmd.append("--mock")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    data = parse_json(proc.stdout or "")
    row: dict = {"commit": commit, "seconds": round(time.time() - t0, 1), "exit_code": proc.returncode}
    if not data or "preview" not in data:
        row["failed"] = True
        row["detail"] = ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[-1][:200]
        return row
    prev = data["preview"]
    usage = data.get("usage") or {}
    inp = sum(int(u.get("input_tokens", 0)) for u in usage.values() if isinstance(u, dict))
    out = sum(int(u.get("output_tokens", 0)) for u in usage.values() if isinstance(u, dict))
    row.update(
        status=data.get("status"), risk=prev.get("risk"), diff_bytes=prev.get("diff_bytes"), files=len(prev.get("files") or []),
        est_usd=prev.get("estimated_usd"), est_low=prev.get("estimated_low_usd"), est_high=prev.get("estimated_high_usd"),
        est_in_tokens=prev.get("input_tokens"), est_out_tokens=prev.get("output_tokens"),
        actual_usd=data.get("cost_usd"), in_tokens=inp, out_tokens=out,
        blocking=len(data.get("blocking") or []), warnings=[w.get("title") for w in (data.get("warnings") or [])],
        blocking_items=[_item(f) for f in (data.get("blocking") or [])], warning_items=[_item(f) for f in (data.get("warnings") or [])],
    )
    return row


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("failed") and r.get("actual_usd") is not None]
    real = [r for r in ok if (r.get("actual_usd") or 0) > 0]
    s: dict = {"calls": len(rows), "failed": sum(1 for r in rows if r.get("failed")), "total_actual_usd": round(sum(r["actual_usd"] for r in ok), 4)}
    if real:
        ratios = [r["actual_usd"] / r["est_usd"] for r in real if r.get("est_usd")]
        s["total_estimated_usd"] = round(sum(r["est_usd"] for r in real if r.get("est_usd")), 4)
        s["actual_over_estimate"] = {"min": round(min(ratios), 2), "max": round(max(ratios), 2), "mean": round(sum(ratios) / len(ratios), 2)} if ratios else None
        s["in_estimated_range"] = f"{sum(1 for r in real if r.get('est_low') is not None and r['est_low'] <= r['actual_usd'] <= r['est_high'])}/{len(real)}"
        s["input_token_error_pct"] = [round(100 * (r["in_tokens"] - r["est_in_tokens"]) / r["est_in_tokens"], 1) for r in real if r.get("est_in_tokens")]
        s["output_token_ratio"] = [round(r["out_tokens"] / r["est_out_tokens"], 2) for r in real if r.get("est_out_tokens")]
        s["with_warnings"] = sum(1 for r in real if r.get("warnings"))
        s["with_blocking"] = sum(1 for r in real if r.get("blocking"))
    return s


def run_batch(commits: list[str], runner, max_total_usd: float) -> list[dict]:
    """Review commits one by one; stop before a call that could pass the total cap (the next call is assumed to cost about the largest so far)."""
    rows: list[dict] = []
    spent = 0.0
    worst = 0.0
    for c in commits:
        if spent + worst > max_total_usd:
            rows.append({"commit": c, "skipped": True, "detail": f"총상한 ${max_total_usd} 에 닿을 수 있어 건너뜀(지금까지 ${spent:.4f})"})
            continue
        row = runner(c)
        rows.append(row)
        cost = row.get("actual_usd") or 0.0
        spent += cost
        worst = max(worst, cost)
    return rows


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="DevOrch 가 설정된 git 저장소(정책 승인이 끝나 있어야 함)")
    ap.add_argument("--commits", required=True, help="쉼표로 구분한 커밋 해시(같은 해시를 반복해도 됨)")
    ap.add_argument("--level", default="medium")
    ap.add_argument("--task", default="코드 변경 리뷰", help="모든 커밋에 같은 중립적인 작업 문장을 쓴다(커밋 제목의 키워드가 위험도를 바꾸지 않도록)")
    ap.add_argument("--max-total-usd", type=float, default=0.40)
    ap.add_argument("--out", default="real_review_results.json")
    ap.add_argument("--mock", action="store_true", help="가짜 리뷰어로 흐름만 확인(키·비용 없음)")
    a = ap.parse_args(argv)
    commits = [c.strip() for c in a.commits.split(",") if c.strip()]
    rows = run_batch(commits, lambda c: review_one(a.project, c, a.level, a.mock, sys.executable, a.task), a.max_total_usd)
    summary = summarize(rows)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"project": a.project, "level": a.level, "mock": a.mock, "max_total_usd": a.max_total_usd, "rows": rows, "summary": summary}, fh, ensure_ascii=False, indent=1)
    print(f"{'commit':<10} {'bytes':>6} {'예상':>8} {'실제':>8} {'입력토큰(오차)':>16} {'출력토큰(배)':>14}  결과")
    for r in rows:
        if r.get("skipped") or r.get("failed"):
            print(f"{r['commit']:<10} {'-':>6} {'-':>8} {'-':>8} {'-':>16} {'-':>14}  {'건너뜀' if r.get('skipped') else '실패'}: {r.get('detail', '')}")
            continue
        e_in = f"{r['in_tokens']:,}({100 * (r['in_tokens'] - r['est_in_tokens']) / r['est_in_tokens']:+.0f}%)" if r.get("est_in_tokens") else "-"
        e_out = f"{r['out_tokens']:,}({r['out_tokens'] / r['est_out_tokens']:.1f}x)" if r.get("est_out_tokens") else "-"
        print(f"{r['commit']:<10} {r['diff_bytes']:>6} ${r['est_usd'] or 0:>7.4f} ${r['actual_usd'] or 0:>7.4f} {e_in:>16} {e_out:>14}  {r['status']} (경고 {len(r['warnings'])}, 차단 {r['blocking']})")
    print("\n요약:", json.dumps(summary, ensure_ascii=False))
    print(f"결과 파일: {os.path.abspath(a.out)}")
    return 1 if summary["failed"] == summary["calls"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
