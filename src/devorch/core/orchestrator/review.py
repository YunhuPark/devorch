"""Provider calls and independent review, shared by the workflow engine and the MCP service."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING

from devorch.audit import AuditLogger
from devorch.core.evaluator.evaluator import parse_findings
from devorch.models import Finding, RoutingStep
from devorch.providers.base import LLMRequest, LLMResponse, ProviderRegistry

if TYPE_CHECKING:
    from devorch.core.orchestrator.engine import WorkflowRun

REVIEWER_SYSTEM = (
    "You are an independent code reviewer. Reply ONLY with a JSON array of findings: "
    '[{"severity": "info|low|medium|high|critical", "category": "...", "title": "...", "detail": "...", "file": "optional"}]. '
    "Reply [] if you find nothing."
)


def build_context(run: "WorkflowRun") -> str:
    assert run.risk and run.impact and run.analysis
    return (
        f"Task: {run.task_text}\n"
        f"Type: {run.analysis.task_type.value}; areas: {', '.join(a.value for a in run.analysis.areas) or '-'}\n"
        f"Risk: {run.risk.level.value.upper()} (score {run.risk.score})\n"
        f"Directly affected files: {', '.join(run.impact.direct_files[:20]) or '-'}\n"
        f"Possibly affected files: {', '.join(run.impact.indirect_files[:20]) or '-'}\n"
    )


def build_review_prompt(run: "WorkflowRun", step: RoutingStep) -> str:
    # Reviewers only see the task and the implementation — never each other's findings.
    return f"{build_context(run)}\nFocus: {step.purpose}\n\nImplementation under review:\n{run.implementation}\n"


class BudgetExceeded(RuntimeError):
    """The task's spend cap (``limits.max_cost_usd``) is reached, or can't be checked. No further LLM call is made."""


def budget_preflight(providers: ProviderRegistry, names: Iterable[str], max_cost_usd: float | None) -> str | None:
    """Error text if a cap is set but a provider in use has no prices (cap unenforceable) — checked before any token is spent."""
    if max_cost_usd is None:
        return None
    unpriced = providers.unpriced(names)
    if not unpriced:
        return None
    return (
        f"limits.max_cost_usd=${max_cost_usd} 가 설정됐지만 단가가 없는 provider가 있어 상한을 지킬 수 없음: {', '.join(unpriced)} "
        "(config.yaml 의 providers.<name>.input_per_mtok / output_per_mtok 를 설정하거나 상한을 null 로 두세요)"
    )


def _check_budget(run: "WorkflowRun", audit: AuditLogger, step: RoutingStep, max_cost_usd: float | None) -> None:
    if max_cost_usd is None:
        return
    if run.cost_unknown:
        reason = "비용을 알 수 없는 호출이 이미 있어 상한을 지킬 수 없음 (providers.<name>.input_per_mtok/output_per_mtok 필요)"
    elif run.total_cost_usd >= max_cost_usd:
        reason = f"작업당 비용 상한 도달: ${run.total_cost_usd:.4f} >= ${max_cost_usd:.4f}"
    else:
        return
    audit.log(
        run.id, "budget_exceeded",
        {"role": step.role, "provider": step.provider, "spent_usd": round(run.total_cost_usd, 6), "max_cost_usd": max_cost_usd, "reason": reason},
    )
    raise BudgetExceeded(reason)


async def call_provider(
    run: "WorkflowRun",
    providers: ProviderRegistry,
    audit: AuditLogger,
    step: RoutingStep,
    request: LLMRequest,
    max_cost_usd: float | None = None,
) -> LLMResponse:
    """One LLM call. With ``max_cost_usd`` set, refuses to start a call once the task's spend has reached the cap.

    The cap is checked *between* calls: a call already in flight (reviewers run in parallel, so up to one
    batch) can still push the total past it. The overshoot is noted on the run and in the audit log.
    """
    provider = providers.get(step.provider)
    _check_budget(run, audit, step, max_cost_usd)
    resp = await provider.generate(request)
    if resp.cost_usd is None:
        run.cost_unknown = True
    else:
        run.total_cost_usd += resp.cost_usd
    if resp.model:
        run.models[step.provider] = resp.model
    audit.log(
        run.id,
        "llm_call",
        {
            "role": step.role, "provider": step.provider, "model": resp.model,
            "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
            "cost_usd": resp.cost_usd, "total_cost_usd": None if run.cost_unknown else round(run.total_cost_usd, 6),
            "max_cost_usd": max_cost_usd, "latency_s": resp.latency_s,
        },
    )
    if max_cost_usd is not None and not run.cost_unknown and run.total_cost_usd > max_cost_usd:
        run.notes.append(f"비용 상한 초과: ${run.total_cost_usd:.4f} > ${max_cost_usd:.4f} (진행 중이던 호출이 끝난 뒤 확인됨)")
    return resp


async def run_reviews(
    run: "WorkflowRun",
    steps: list[RoutingStep],
    providers: ProviderRegistry,
    audit: AuditLogger,
    label: str = "",
    max_cost_usd: float | None = None,
) -> tuple[list[Finding], list[str]]:
    """Run all reviewers concurrently and independently.

    Returns (findings, completed_roles). A reviewer whose call failed or whose
    answer could not be parsed is NOT in ``completed_roles`` — never a silent pass.
    """
    results = await asyncio.gather(
        *(
            call_provider(
                run, providers, audit, s,
                LLMRequest(role=s.role, system=REVIEWER_SYSTEM, prompt=build_review_prompt(run, s)),
                max_cost_usd=max_cost_usd,
            )
            for s in steps
        ),
        return_exceptions=True,
    )
    findings: list[Finding] = []
    completed: list[str] = []
    for step, result in zip(steps, results):
        if isinstance(result, BaseException):
            run.notes.append(f"{label}{step.role}({step.provider}) 호출 실패: {type(result).__name__}: {result}")
            audit.log(run.id, "review_failed", {"role": step.role, "error": str(result)})
            continue
        parsed, ok = parse_findings(step.provider, step.role, result.text)
        if not ok:
            run.notes.append(f"{label}{step.role}({step.provider}) 응답을 파싱하지 못해 리뷰 미완료로 처리")
            audit.log(run.id, "review_unparsable", {"role": step.role})
            continue
        completed.append(step.role)
        findings.extend(parsed)
    return findings, completed
