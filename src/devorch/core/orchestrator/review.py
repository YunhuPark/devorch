"""Provider calls and independent review, shared by the workflow engine and the MCP service."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING

from devorch.audit import AuditLogger
from devorch.core.evaluator.evaluator import parse_findings
from devorch.models import Finding, RoutingStep
from devorch.providers.base import LLMRequest, LLMResponse, ProviderError, ProviderRegistry
from devorch.security.secret_redactor import redact

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


def describe_error(exc: BaseException, limit: int = 300) -> str:
    """One line for a failed provider call: type, message, and — when the SDK exposes them — HTTP status and
    the start of the response body (SDK messages sometimes drop the body, e.g. a bodiless 400). Redacted."""
    text = f"{type(exc).__name__}: {exc}"
    status = getattr(exc, "status_code", None)
    body = ""
    try:
        raw = getattr(getattr(exc, "response", None), "text", None)
        body = raw.strip() if isinstance(raw, str) else ""
    except Exception:  # an unread streaming body, etc. — the description must never fail
        body = ""
    if status is not None and f"{status}" not in text:
        text += f" [HTTP {status}]"
    if body and body[:60] not in text:
        text += f" | 응답 본문: {body[:limit]}"
    return redact(text)


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
    try:
        resp = await provider.generate(request)
    except ProviderError as e:
        # The API answered but the answer is unusable (cut at max_tokens, empty, blocked): the tokens were still billed.
        _account(run, audit, step, "", e.input_tokens, e.output_tokens, e.cost_usd, max_cost_usd, error=str(e)[:200])
        raise
    _account(run, audit, step, resp.model, resp.input_tokens, resp.output_tokens, resp.cost_usd, max_cost_usd, latency_s=resp.latency_s)
    return resp


def _account(
    run: "WorkflowRun", audit: AuditLogger, step: RoutingStep, model: str, in_tok: int, out_tok: int,
    cost_usd: float | None, max_cost_usd: float | None, latency_s: float = 0.0, error: str | None = None,
) -> None:
    """Cost, tokens and the audit record of one call — successful or not."""
    if cost_usd is None:
        run.cost_unknown = True
    else:
        run.total_cost_usd += cost_usd
    if model:
        run.models[step.provider] = model
    u = run.usage.setdefault(step.provider, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
    u["calls"] += 1
    u["input_tokens"] += in_tok
    u["output_tokens"] += out_tok
    if error is not None:
        u["failed_calls"] = u.get("failed_calls", 0) + 1
    record = {
        "role": step.role, "provider": step.provider, "model": model,
        "input_tokens": in_tok, "output_tokens": out_tok,
        "cost_usd": cost_usd, "total_cost_usd": None if run.cost_unknown else round(run.total_cost_usd, 6),
        "max_cost_usd": max_cost_usd, "latency_s": latency_s,
    }
    if error is not None:
        record.update(failed=True, error=error)
    audit.log(run.id, "llm_call", record)
    if max_cost_usd is not None and not run.cost_unknown and run.total_cost_usd > max_cost_usd:
        run.notes.append(f"비용 상한 초과: ${run.total_cost_usd:.4f} > ${max_cost_usd:.4f} (진행 중이던 호출이 끝난 뒤 확인됨)")


async def run_reviews(
    run: "WorkflowRun",
    steps: list[RoutingStep],
    providers: ProviderRegistry,
    audit: AuditLogger,
    label: str = "",
    max_cost_usd: float | None = None,
    max_tokens: int | None = None,
) -> tuple[list[Finding], list[str]]:
    """Run all reviewers concurrently and independently.

    Returns (findings, completed_roles). A reviewer whose call failed or whose
    answer could not be parsed is NOT in ``completed_roles`` — never a silent pass.
    ``max_tokens`` is the per-call output budget (``limits.review_max_tokens``); thinking models spend part of it on thoughts.
    """
    extra = {"max_tokens": max_tokens} if max_tokens else {}
    results = await asyncio.gather(
        *(
            call_provider(
                run, providers, audit, s,
                LLMRequest(role=s.role, system=REVIEWER_SYSTEM, prompt=build_review_prompt(run, s), **extra),
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
            hint = (
                " — 이 호출은 잘렸지만 토큰 비용은 이미 청구됐습니다. 큰 diff에서는 policy.yaml 의 limits.review_max_tokens 를 올리세요(생각 토큰이 출력 한도를 쓰는 모델이 있음)"
                if isinstance(result, ProviderError) and result.truncated
                else ""
            )
            run.notes.append(f"{label}{step.role}({step.provider}) 호출 실패: {describe_error(result)}{hint}")
            audit.log(run.id, "review_failed", {"role": step.role, "error": describe_error(result)})
            continue
        parsed, ok = parse_findings(step.provider, step.role, result.text)
        if not ok:
            run.notes.append(f"{label}{step.role}({step.provider}) 응답을 파싱하지 못해 리뷰 미완료로 처리")
            audit.log(run.id, "review_unparsable", {"role": step.role})
            continue
        completed.append(step.role)
        findings.extend(parsed)
    return findings, completed
