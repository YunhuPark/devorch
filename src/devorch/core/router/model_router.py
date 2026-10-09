"""Agent / Model Router — maps a risk level (plus policy rules) to a verification plan."""

from __future__ import annotations

from devorch.config import Policy
from devorch.core.policy.approval_policy import approval_required
from devorch.core.router.cost_model import DEFAULT_DIFF_CHARS_PER_FILE, Prices, assumed_prompt_chars, estimate_call
from devorch.models import (
    CostLine,
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


def _estimate(
    policy: Policy,
    steps: list[RoutingStep],
    task_text: str = "",
    n_direct_files: int = 1,
    prices: Prices | None = None,
) -> tuple[float, float, list[CostLine], list[str]]:
    """Cost = reviewer calls by tokens x price where a price is configured, flat fallback otherwise (and always for the
    implementation step, which has never been measured). Returns (cost, latency, per-step breakdown, notes)."""
    cost_table = policy.estimates.get("cost_usd", {})
    lat_table = policy.estimates.get("latency_s", {})
    chars_per_file = policy.estimates.get("assumed", {}).get("diff_chars_per_file", DEFAULT_DIFF_CHARS_PER_FILE)
    prompt_chars = assumed_prompt_chars(task_text, n_direct_files, chars_per_file)
    lines: list[CostLine] = []
    unmeasured: set[str] = set()
    for s in steps:
        flat = CostLine(role=s.role, provider=s.provider, usd=cost_table.get(s.provider, 0.0), basis="기본 고정값")
        if s.role == "implementation":
            lines.append(flat)
            continue
        e = estimate_call(s.provider, prompt_chars, prices)
        if e.usd is None:
            lines.append(flat)
            continue
        if not e.measured:
            unmeasured.add(s.provider)
        basis = "토큰×단가" if e.measured else "토큰×단가(미측정 가정)"
        lines.append(CostLine(role=s.role, provider=s.provider, usd=round(e.usd, 6), basis=basis, input_tokens=e.input_tokens, output_tokens=e.output_tokens))
    impl = next((s for s in steps if s.role == "implementation"), None)
    impl_lat = lat_table.get(impl.provider, 0.0) if impl else 0.0
    # reviewers run in parallel
    review_lat = max((lat_table.get(s.provider, 0.0) for s in steps if s.role != "implementation"), default=0.0)

    notes: list[str] = []
    token_based = [x for x in lines if x.basis.startswith("토큰")]
    if token_based:
        notes.append(
            "비용 추정 근거: 리뷰어 호출은 토큰×단가(openai/gemini는 벤치마크 실측으로 맞춘 계수, 작은 diff 기준 합계 오차 +2~11%), "
            "구현 단계와 단가가 없는 provider는 기본 고정값(실측 아님). "
            f"diff 크기는 아직 없어서 직접 영향 파일 {max(1, n_direct_files)}개 × {chars_per_file:.0f}자로 가정함 — 실제 diff가 크면 달라지며 큰 diff는 검증되지 않았음. "
            "호출 하나는 편차가 크고(출력 토큰이 수십 배 차이) 합계에 가깝게 맞음."
        )
        if unmeasured:
            notes.append(f"{', '.join(sorted(unmeasured))}: 토큰 계수를 측정한 적이 없어 가정값을 씀(미측정).")
    else:
        notes.append("비용/시간은 policy.estimates의 기본 고정값 기반(실측 아님, 작은 diff의 리뷰 호출 기준으로는 약 7~8배 큼). 단가를 설정하면 토큰×단가로 계산함")
    return round(sum(x.usd for x in lines), 6), round(impl_lat + review_lat, 1), lines, notes


def plan_routing(
    analysis: TaskAnalysis,
    risk: RiskAssessment,
    impact: ImpactReport | None,
    policy: Policy,
    prices: Prices | None = None,
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

    cost, latency, breakdown, cost_notes = _estimate(
        policy, steps, task_text=analysis.text, n_direct_files=len(impact.direct_files) if impact else 1, prices=prices
    )
    notes += cost_notes

    return RoutingPlan(
        level=level,
        steps=steps,
        requires_human_approval=needs_approval,
        approval_reasons=reasons,
        run_tests=analysis.task_type is not TaskType.DOCS,
        verification_level=verification,
        estimated_cost_usd=cost,
        estimated_latency_s=latency,
        cost_breakdown=breakdown,
        notes=notes,
    )
