"""Evaluator — merges independent review findings into one go / no-go verdict."""

from __future__ import annotations

import json
import re

from devorch.models import Evaluation, Finding, Severity

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_findings(source: str, role: str, text: str) -> tuple[list[Finding], bool]:
    """Parse a reviewer response (a JSON array of findings, optionally fenced).

    Returns (findings, ok). ``ok`` is False when the response could not be
    parsed — the caller must then NOT count that review as completed.
    """
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    data = None
    for cand in candidates:
        cand = cand.strip()
        start, end = cand.find("["), cand.rfind("]")
        if start == -1 or end <= start:
            continue
        try:
            data = json.loads(cand[start : end + 1])
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(data, list):
        return [], False

    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict) or "title" not in item:
            return [], False
        raw_sev = str(item.get("severity", "medium")).lower()
        try:
            severity = Severity(raw_sev)
        except ValueError:
            severity = Severity.MEDIUM  # unknown severity: do not silently downgrade to harmless
        findings.append(
            Finding(
                source=source,
                role=role,
                severity=severity,
                category=str(item.get("category", role)),
                title=str(item["title"]),
                detail=str(item.get("detail", "")),
                file=item.get("file"),
            )
        )
    return findings, True


def _dedupe(findings: list[Finding]) -> list[Finding]:
    merged: dict[tuple[str, str, str | None], Finding] = {}
    for f in findings:
        key = (f.category.lower().strip(), re.sub(r"\s+", " ", f.title.lower().strip()), f.file)
        if key not in merged:
            merged[key] = f.model_copy()
            continue
        cur = merged[key]
        if f.severity.rank > cur.severity.rank:
            cur.severity = f.severity
            cur.detail = f.detail or cur.detail
        sources = set(cur.source.split(",")) | {f.source}
        cur.source = ",".join(sorted(sources))
    return list(merged.values())


def evaluate(
    findings: list[Finding],
    required_reviews: list[str],
    completed_reviews: list[str],
) -> Evaluation:
    """``required_reviews`` / ``completed_reviews`` are role names."""
    missing = [r for r in required_reviews if r not in completed_reviews]
    merged = sorted(_dedupe(findings), key=lambda f: -f.severity.rank)
    blocking = [f for f in merged if f.severity.blocking]
    warnings = [f for f in merged if not f.severity.blocking and f.severity is not Severity.INFO]

    if blocking or missing:
        status = "NOT_READY"
    elif warnings:
        status = "READY_WITH_WARNINGS"
    else:
        status = "READY"

    parts = [f"status={status}", f"blocking={len(blocking)}", f"warnings={len(warnings)}"]
    if missing:
        parts.append("missing_reviews=" + ",".join(missing))
    return Evaluation(
        status=status,
        blocking=blocking,
        warnings=warnings,
        missing_reviews=missing,
        summary=" ".join(parts),
    )
