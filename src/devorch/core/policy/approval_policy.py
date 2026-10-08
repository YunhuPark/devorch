"""Decides whether a human must approve a change before it is applied."""

from __future__ import annotations

from devorch.config import Policy
from devorch.models import ImpactReport, RiskLevel, TaskAnalysis


def approval_required(
    level: RiskLevel,
    analysis: TaskAnalysis,
    impact: ImpactReport | None,
    policy: Policy,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    threshold = RiskLevel(policy.approval.require_human_approval_at)
    if level >= threshold:
        reasons.append(f"위험도 {level.value.upper()} ≥ 승인 기준 {threshold.value.upper()}")

    flagged = set(policy.approval.require_for_areas)
    for area in analysis.areas:
        if area.value in flagged:
            reasons.append(f"{area.value} 영역은 정책상 사람 승인 필요")
    if impact is not None and impact.database_impact and "database" in flagged:
        reasons.append("영향 범위에 DB 관련 파일 포함 → 사람 승인 필요")

    # de-duplicate, keep order
    seen: set[str] = set()
    unique = [r for r in reasons if not (r in seen or seen.add(r))]
    return bool(unique), unique
