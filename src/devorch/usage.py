"""Spend report from the audit log (``.devorch/audit.jsonl``): what each task actually cost, per provider and per day.

Only reads what DevOrch itself logged — its own LLM calls. The client agent's work in the MCP flow (Claude Code etc.) is not
visible here. A call without a price has an unknown cost: it is counted and flagged, never summed as 0. Mock calls are left out
(unless asked) so a demo run doesn't pollute the numbers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_TASK_EVENTS = ("task_created", "mcp:analyze_task")


def load_events(path: Path) -> list[dict[str, Any]]:
    """Parsed audit records; a missing file is an empty log and unreadable lines are skipped (an append-only log can be cut mid-write)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("data"), dict) and rec.get("run_id") and rec.get("event"):
            events.append(rec)
    return events


def _when(rec: dict[str, Any]) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(rec.get("ts")))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def summarize(events: list[dict[str, Any]], days: float | None = None, include_mock: bool = False, now: datetime | None = None) -> dict[str, Any]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days) if days is not None else None
    runs: dict[str, dict[str, Any]] = {}
    for rec in events:
        when = _when(rec)
        if when is None or (cutoff is not None and when < cutoff):
            continue
        data, kind = rec["data"], rec["event"]
        run = runs.setdefault(rec["run_id"], {
            "id": rec["run_id"], "first_ts": when, "task": "", "risk": None, "calls": 0, "failed_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "unknown_calls": 0,
            "review_cost_usd": 0.0, "review_unknown": False, "est_low": 0.0, "est_high": 0.0, "est_known": False, "est_unknown": False,
            "providers": {},
        })
        run["first_ts"] = min(run["first_ts"], when)
        if kind in _TASK_EVENTS:
            run["task"] = run["task"] or str(data.get("task", ""))
            run["risk"] = run["risk"] or data.get("risk")
        elif kind == "state" and data.get("to") == "PLANNED":
            run["risk"] = run["risk"] or data.get("risk")  # `devorch run` records the risk here, not on the task_created event
        elif kind == "mcp:review_estimate":
            if data.get("estimated_low_usd") is None or data.get("estimated_high_usd") is None:
                run["est_unknown"] = True
            else:
                run["est_known"] = True
                run["est_low"] += float(data["estimated_low_usd"])
                run["est_high"] += float(data["estimated_high_usd"])
        elif kind == "llm_call":
            if data.get("model") == "mock" and not include_mock:
                continue
            run["calls"] += 1
            run["failed_calls"] += 1 if data.get("failed") else 0
            run["input_tokens"] += int(data.get("input_tokens") or 0)
            run["output_tokens"] += int(data.get("output_tokens") or 0)
            cost = data.get("cost_usd")
            is_review = data.get("role") != "implementation"
            if cost is None:
                run["unknown_calls"] += 1
                run["review_unknown"] = run["review_unknown"] or is_review
            else:
                run["cost_usd"] += float(cost)
                if is_review:
                    run["review_cost_usd"] += float(cost)
            p = run["providers"].setdefault(str(data.get("provider")), {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "unknown_calls": 0})
            p["calls"] += 1
            p["input_tokens"] += int(data.get("input_tokens") or 0)
            p["output_tokens"] += int(data.get("output_tokens") or 0)
            if cost is None:
                p["unknown_calls"] += 1
            else:
                p["cost_usd"] += float(cost)
            run.setdefault("days", {}).setdefault(when.astimezone().date().isoformat(), 0.0)
            if cost is not None:
                run["days"][when.astimezone().date().isoformat()] += float(cost)

    rows = sorted((r for r in runs.values() if r["calls"]), key=lambda r: r["first_ts"])
    by_provider: dict[str, dict[str, Any]] = {}
    by_day: dict[str, float] = {}
    for r in rows:
        for name, p in r["providers"].items():
            t = by_provider.setdefault(name, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "unknown_calls": 0})
            for k in t:
                t[k] += p[k]
        for day, usd in r.get("days", {}).items():
            by_day[day] = by_day.get(day, 0.0) + usd
    # Estimate vs actual, reviews only (the estimate covers reviews): only runs where both are known.
    comparable = [r for r in rows if r["est_known"] and not r["est_unknown"] and not r["review_unknown"] and r["review_cost_usd"] > 0]
    inside = [r for r in comparable if r["est_low"] <= r["review_cost_usd"] <= r["est_high"]]
    return {
        "runs": [{**r, "first_ts": r["first_ts"].isoformat(timespec="seconds"), "days": r.get("days", {})} for r in rows],
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "unknown_calls": sum(r["unknown_calls"] for r in rows),
        "calls": sum(r["calls"] for r in rows),
        "failed_calls": sum(r["failed_calls"] for r in rows),
        "by_provider": by_provider,
        "by_day": dict(sorted(by_day.items())),
        "estimate_check": {"comparable_runs": len(comparable), "inside_range": len(inside)},
    }


def render(summary: dict[str, Any]) -> str:
    runs = summary["runs"]
    if not runs:
        return "기록된 LLM 호출이 없습니다. (mock 호출은 제외됩니다. --include-mock 으로 포함)"
    lines = ["작업별 지출 (DevOrch 가 직접 호출한 LLM 만; 클라이언트 에이전트의 작업은 포함되지 않음)"]
    for r in runs:
        cost = f"${r['cost_usd']:.4f}" + (f" + 단가 없는 호출 {r['unknown_calls']}건(비용 알 수 없음)" if r["unknown_calls"] else "")
        est = ""
        if r["est_known"] and not r["est_unknown"]:
            est = f"  리뷰 예상 ${r['est_low']:.4f}~${r['est_high']:.4f}, 실제 ${r['review_cost_usd']:.4f}" + ("" if r["review_unknown"] else (" (범위 안)" if r["est_low"] <= r["review_cost_usd"] <= r["est_high"] else " (범위 밖)"))
        fail = f"  실패 {r['failed_calls']}건" if r["failed_calls"] else ""
        lines.append(f"  {r['first_ts'][:16]}  {r['id']}  {str(r['risk'] or '-').upper():<8} {cost}  호출 {r['calls']}회{fail}  {r['task'][:40]}{est}")
    lines.append("")
    lines.append("provider 별:")
    for name, p in sorted(summary["by_provider"].items()):
        unk = f" (+ 비용 모르는 호출 {p['unknown_calls']}건)" if p["unknown_calls"] else ""
        lines.append(f"  {name:<8} 호출 {p['calls']}회  토큰 입력 {p['input_tokens']:,} / 출력 {p['output_tokens']:,}  ${p['cost_usd']:.4f}{unk}")
    if len(summary["by_day"]) > 1:
        lines.append("날짜별: " + ", ".join(f"{d} ${usd:.4f}" for d, usd in summary["by_day"].items()))
    total = f"합계: ${summary['total_cost_usd']:.4f}  (작업 {len(runs)}건, 호출 {summary['calls']}회, 실패 {summary['failed_calls']}회)"
    if summary["unknown_calls"]:
        total += f"\n  ※ 단가가 없어 비용을 알 수 없는 호출 {summary['unknown_calls']}건은 합계에 들어 있지 않습니다 — 실제 지출은 이보다 큽니다."
    lines.append(total)
    chk = summary["estimate_check"]
    if chk["comparable_runs"]:
        lines.append(f"리뷰 예상 범위 적중: {chk['inside_range']}/{chk['comparable_runs']}건 (표본이 작으면 의미가 약함)")
    return "\n".join(lines)
