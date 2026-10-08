"""Provider calls and independent review, shared by the workflow engine and the MCP service."""

from __future__ import annotations

import asyncio
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


async def call_provider(
    run: "WorkflowRun", providers: ProviderRegistry, audit: AuditLogger, step: RoutingStep, request: LLMRequest
) -> LLMResponse:
    provider = providers.get(step.provider)
    resp = await provider.generate(request)
    if resp.cost_usd is None:
        run.cost_unknown = True
    else:
        run.total_cost_usd += resp.cost_usd
    audit.log(
        run.id,
        "llm_call",
        {"role": step.role, "provider": step.provider, "model": resp.model, "cost_usd": resp.cost_usd, "latency_s": resp.latency_s},
    )
    return resp


async def run_reviews(
    run: "WorkflowRun",
    steps: list[RoutingStep],
    providers: ProviderRegistry,
    audit: AuditLogger,
    label: str = "",
) -> tuple[list[Finding], list[str]]:
    """Run all reviewers concurrently and independently.

    Returns (findings, completed_roles). A reviewer whose call failed or whose
    answer could not be parsed is NOT in ``completed_roles`` — never a silent pass.
    """
    results = await asyncio.gather(
        *(
            call_provider(run, providers, audit, s, LLMRequest(role=s.role, system=REVIEWER_SYSTEM, prompt=build_review_prompt(run, s)))
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
