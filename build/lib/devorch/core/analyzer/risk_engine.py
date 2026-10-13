"""Risk Engine — rule-based risk score with minimum-level floors.

score = sum(area weights) + task type + scope + impact size
level thresholds: <20 LOW, <40 MEDIUM, <60 HIGH, otherwise CRITICAL.
Some areas (payment, permission, auth, database) force a minimum level no
matter what the score says.
"""

from __future__ import annotations

from devorch.core.analyzer.keywords import AREA_FLOORS, AREA_WEIGHTS
from devorch.models import (
    ImpactReport,
    RiskAssessment,
    RiskFactor,
    RiskLevel,
    TaskAnalysis,
    TaskType,
)

TYPE_POINTS = {
    TaskType.FEATURE: 5,
    TaskType.CHANGE: 5,
    TaskType.REFACTOR: 5,
    TaskType.BUGFIX: 0,
    TaskType.TEST: 0,
    TaskType.DOCS: -10,
}
SCOPE_POINTS = {"small": 0, "medium": 5, "large": 10}


def level_for_score(score: int) -> RiskLevel:
    if score < 20:
        return RiskLevel.LOW
    if score < 40:
        return RiskLevel.MEDIUM
    if score < 60:
        return RiskLevel.HIGH
    return RiskLevel.CRITICAL


def assess_risk(analysis: TaskAnalysis, impact: ImpactReport | None = None) -> RiskAssessment:
    factors: list[RiskFactor] = []

    for area in analysis.areas:
        weight = AREA_WEIGHTS.get(area, 0)
        if weight:
            factors.append(RiskFactor(name=f"area:{area.value}", points=weight, reason=f"{area.value} 영역을 건드리는 작업"))

    type_pts = TYPE_POINTS[analysis.task_type]
    if type_pts:
        factors.append(RiskFactor(name=f"type:{analysis.task_type.value}", points=type_pts, reason="작업 유형 가중치"))

    scope_pts = SCOPE_POINTS[analysis.scope]
    if scope_pts:
        factors.append(RiskFactor(name=f"scope:{analysis.scope}", points=scope_pts, reason="작업 범위"))

    if impact is not None:
        impact_pts = min(20, len(impact.direct_files) * 2 + len(impact.indirect_files))
        if impact_pts:
            factors.append(
                RiskFactor(
                    name="impact:files",
                    points=impact_pts,
                    reason=f"직접 {len(impact.direct_files)}개 + 간접 {len(impact.indirect_files)}개 파일 영향",
                )
            )
        if impact.security_impact and not any(f.name in ("area:auth", "area:security", "area:permission", "area:payment") for f in factors):
            factors.append(RiskFactor(name="impact:security", points=10, reason="영향 범위에 보안 민감 파일 포함"))
        if impact.database_impact and not any(f.name == "area:database" for f in factors):
            factors.append(RiskFactor(name="impact:database", points=10, reason="영향 범위에 DB 관련 파일 포함"))

    score = max(0, sum(f.points for f in factors))
    level = level_for_score(score)

    floor_reason = None
    for area in analysis.areas:
        floor = AREA_FLOORS.get(area)
        if floor and RiskLevel(floor) > level:
            level = RiskLevel(floor)
            floor_reason = f"{area.value} 영역은 최소 {floor.upper()}로 고정"

    return RiskAssessment(score=score, level=level, factors=factors, floor_reason=floor_reason)
