"""Agent / Model Router — maps a risk level (plus policy rules) to a verification plan."""

from __future__ import annotations

from devorch.config import Policy
from devorch.core.policy.approval_policy import approval_required
from devorch.models import (
    ImpactReport,
    RiskAssessment,
    RiskLevel,
    RoutingPlan,
    RoutingStep,
    TaskAnalysis,
    TaskType,
)

PURPOSES = {
    "implementation": "코드 구현 / 최종 통합",
    "review": "코드 리뷰",
    "security": "보안 리뷰",
    "architecture": "아키텍처 리뷰",
}

VERIFICATION_LEVELS = ["BASIC", "STANDARD", "STRICT", "STRICT+HUMAN"]


def _estimate(policy: Policy, steps: list[RoutingStep]) -> tuple[float, float]:
    cost_table = policy.estimates.get("cost_usd", {})
    lat_table = policy.estimates.get("latency_s", {})
    cost = sum(cost_table.get(s.provider, 0.0) for s in steps)
    impl = next((s for s in steps if s.role == "implementation"), None)
    impl_lat = lat_table.get(impl.provider, 0.0) if impl else 0.0
    # reviewers run in parallel
    review_lat = max((lat_table.get(s.provider, 0.0) for s in steps if s.role != "implementation"), default=0.0)
    return round(cost, 4), round(impl_lat + review_lat, 1)


def plan_routing(
    analysis: TaskAnalysis,
    risk: RiskAssessment,
    impact: ImpactReport | None,
    policy: Policy,
) -> RoutingPlan:
    level = risk.level
    roles = dict(policy.rules[level.value])
    notes: list[str] = []

    implementer = roles["implementation"]

    # Policy: security-sensitive work needs an external security review.
    sec_areas = set(policy.security.security_areas)
    touches_security = any(a.value in sec_areas for a in analysis.areas) or bool(impact and impact.security_impact)
    if policy.security.require_external_review and touches_security and "security" not in roles:
        provider = policy.default_roles.get("security", "openai")
        roles["security"] = provider
        notes.append("보안 민감 작업 → 외부 보안 리뷰 추가(policy.security.require_external_review)")

    # Policy: critical work needs N independent reviewers (distinct providers, not the implementer).
    if level is RiskLevel.CRITICAL:
        needed = policy.critical.required_reviewers

        def independent() -> set[str]:
            return {p for r, p in roles.items() if r != "implementation" and p != implementer}

        for role in ("security", "architecture", "review"):
            if len(independent()) >= needed:
                break
            if role not in roles:
                roles[role] = policy.default_roles.get(role, "openai")
        if len(independent()) < needed:
            notes.append(
                f"critical은 독립 리뷰어 {needed}명이 필요하지만 정책상 {len(independent())}명만 구성 가능"
                " — policy.yaml의 rules/default_roles를 확인하세요"
            )

    order = ["implementation", "review", "security", "architecture"]
    ordered = sorted(roles.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order))
    steps = [RoutingStep(role=r, provider=p, purpose=PURPOSES.get(r, r)) for r, p in ordered]

    needs_approval, reasons = approval_required(level, analysis, impact, policy)

    reviewers = [s for s in steps if s.role != "implementation"]
    if not reviewers:
        verification = VERIFICATION_LEVELS[0]
    elif len({s.provider for s in reviewers}) == 1 and len(reviewers) == 1:
        verification = VERIFICATION_LEVELS[1]
    else:
        verification = VERIFICATION_LEVELS[2]
    if needs_approval:
        verification = VERIFICATION_LEVELS[3] if reviewers else "BASIC+HUMAN"

    cost, latency = _estimate(policy, steps)
    notes.append("비용/시간은 policy.estimates의 기본 추정치 기반(실측 아님)")

    return RoutingPlan(
        level=level,
        steps=steps,
        requires_human_approval=needs_approval,
        approval_reasons=reasons,
        run_tests=analysis.task_type is not TaskType.DOCS,
        verification_level=verification,
        estimated_cost_usd=cost,
        estimated_latency_s=latency,
        notes=notes,
    )
