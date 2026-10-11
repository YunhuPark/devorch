"""Workflow engine: analyze → plan → implement → independent review → evaluate → approve.

Scope of this version: implementation output is *text* returned by the
implementer provider. Applying patches to a git worktree, running tests and
committing are exposed as hooks (``apply_hook`` / ``test_hook``) and are
skipped (and recorded as skipped) when no hook is supplied.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, Field

from devorch.audit import AuditLogger
from devorch.config import Policy
from devorch.core.analyzer.impact_analyzer import analyze_impact
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.evaluator.evaluator import evaluate
from devorch.core.orchestrator.review import budget_preflight, build_context, call_provider, run_reviews
from devorch.core.orchestrator.state import State, check_transition
from devorch.core.router.cost_model import prices_from_registry
from devorch.core.router.model_router import plan_routing
from devorch.models import (
    Evaluation,
    Finding,
    ImpactReport,
    RiskAssessment,
    RoutingPlan,
    TaskAnalysis,
)
from devorch.providers.base import LLMRequest, ProviderRegistry

Approver = Callable[["WorkflowRun"], Awaitable[bool] | bool]
ApplyHook = Callable[["WorkflowRun"], Awaitable[None] | None]
TestHook = Callable[["WorkflowRun"], Awaitable[bool] | bool]
CommitHook = Callable[["WorkflowRun"], Awaitable[None] | None]
ContextProvider = Callable[["WorkflowRun"], str]

IMPLEMENTER_SYSTEM = (
    "You are the implementation agent. Produce the change as ONE unified diff in a ```diff fenced block, "
    "followed by a short rationale. The diff must apply with `git apply` from the repository root: use "
    "`diff --git a/<path> b/<path>` headers, repo-relative paths, and context lines copied exactly from the "
    "file contents provided. Do not touch .git/ or .devorch/, do not add binary files or symlinks."
)
class WorkflowRun(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    task_text: str
    state: State = State.CREATED
    analysis: TaskAnalysis | None = None
    impact: ImpactReport | None = None
    risk: RiskAssessment | None = None
    plan: RoutingPlan | None = None
    implementation: str = ""
    findings: list[Finding] = Field(default_factory=list)
    evaluation: Evaluation | None = None
    rounds: int = 0
    approved: bool | None = None
    failure_reason: str | None = None
    notes: list[str] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    cost_unknown: bool = False
    models: dict[str, str] = Field(default_factory=dict)  # provider -> model id actually used
    # provider -> {"calls", "input_tokens", "output_tokens"}; kept even when no prices are configured,
    # so a cost can be computed later from the tokens.
    usage: dict[str, dict[str, int]] = Field(default_factory=dict)
    branch: str | None = None
    worktree: str | None = None
    commit: str | None = None
    test_summary: str = ""
    test_output: str = ""


class WorkflowEngine:
    def __init__(
        self,
        project_root: Path,
        policy: Policy,
        providers: ProviderRegistry,
        audit: AuditLogger | None = None,
        approver: Approver | None = None,
        apply_hook: ApplyHook | None = None,
        test_hook: TestHook | None = None,
        commit_hook: CommitHook | None = None,
        context_provider: ContextProvider | None = None,
    ) -> None:
        self.root = Path(project_root)
        self.policy = policy
        self.providers = providers
        self.audit = audit or AuditLogger(None)
        self.approver = approver
        self.apply_hook = apply_hook
        self.test_hook = test_hook
        self.commit_hook = commit_hook
        self.context_provider = context_provider

    # ------------------------------------------------------------------ helpers
    def _move(self, run: WorkflowRun, new: State, **data: object) -> None:
        check_transition(run.state, new)
        self.audit.log(run.id, "state", {"from": run.state.value, "to": new.value, **data})
        run.state = new

    def _fail(self, run: WorkflowRun, reason: str) -> WorkflowRun:
        run.failure_reason = reason
        if run.state not in (State.FAILED, State.COMPLETED, State.REJECTED):
            self._move(run, State.FAILED, reason=reason)
        return run

    # ---------------------------------------------------------------- prompts
    def _implementation_prompt(self, run: WorkflowRun, blocking: list[Finding] | None) -> str:
        prompt = build_context(run)
        files = self.context_provider(run) if self.context_provider else ""
        if files:
            prompt += f"\nCurrent file contents (HEAD):\n{files}\n"
        if blocking:
            issues = "\n".join(f"- [{f.severity.value}] ({f.source}) {f.title}: {f.detail}" for f in blocking)
            prompt += f"\nPrevious implementation:\n{run.implementation}\n\nFix these blocking review findings:\n{issues}\n"
        return prompt

    # -------------------------------------------------------------------- run
    async def run(self, task_text: str) -> WorkflowRun:
        run = WorkflowRun(task_text=task_text)
        self.audit.log(run.id, "task_created", {"task": task_text})
        try:
            return await self._run(run)
        except Exception as exc:  # any unexpected error ends the run in FAILED, never half-applied
            return self._fail(run, f"{type(exc).__name__}: {exc}")

    async def _run(self, run: WorkflowRun) -> WorkflowRun:
        # 1. analyze
        self._move(run, State.ANALYZING)
        run.analysis = analyze_task(run.task_text)
        run.impact = analyze_impact(self.root, run.analysis)
        run.risk = assess_risk(run.analysis, run.impact)
        run.plan = plan_routing(run.analysis, run.risk, run.impact, self.policy, prices=prices_from_registry(self.providers))
        self._move(run, State.PLANNED, risk=run.risk.level.value, steps=[(s.role, s.provider) for s in run.plan.steps])

        plan = run.plan
        unregistered = sorted({s.provider for s in plan.steps} - set(self.providers.names()))
        if unregistered:
            return self._fail(run, f"등록되지 않은 provider: {', '.join(unregistered)} (등록됨: {', '.join(self.providers.names()) or '없음'})")
        cap = self.policy.limits.max_cost_usd
        if (problem := budget_preflight(self.providers, {s.provider for s in plan.steps}, cap)) is not None:
            return self._fail(run, problem)
        impl_step = plan.implementation_step
        review_steps = plan.review_steps
        required = [s.role for s in review_steps]
        max_rounds = self.policy.limits.max_retries + 1

        # 2. implement → review → evaluate (with bounded revision rounds)
        blocking: list[Finding] | None = None
        while True:
            run.rounds += 1
            self._move(run, State.DISPATCHING, round=run.rounds)
            resp = await call_provider(run, self.providers, self.audit, impl_step, LLMRequest(role="implementation", system=IMPLEMENTER_SYSTEM, prompt=self._implementation_prompt(run, blocking)), max_cost_usd=cap)
            run.implementation = resp.text

            findings: list[Finding] = []
            completed: list[str] = []
            if review_steps:
                self._move(run, State.REVIEWING)
                findings, completed = await run_reviews(run, review_steps, self.providers, self.audit, label=f"round {run.rounds}: ", max_cost_usd=cap, max_tokens=self.policy.limits.review_max_tokens)

            run.findings = findings
            self._move(run, State.EVALUATING)
            run.evaluation = evaluate(findings, required, completed)
            self.audit.log(run.id, "evaluation", {"summary": run.evaluation.summary, "round": run.rounds})

            if run.evaluation.status != "NOT_READY":
                break
            if not run.evaluation.blocking:
                # NOT_READY only because a review is missing: retrying the implementation won't fix that.
                return self._fail(run, f"필수 리뷰 미완료: {', '.join(run.evaluation.missing_reviews)}")
            if run.rounds >= max_rounds:
                return self._fail(run, f"{run.rounds}회 수정 후에도 차단 이슈 {len(run.evaluation.blocking)}건이 남아 있음")
            blocking = run.evaluation.blocking

        # 3. human approval
        if plan.requires_human_approval:
            self._move(run, State.WAITING_APPROVAL, reasons=plan.approval_reasons)
            if self.approver is None:
                return self._fail(run, "사람 승인이 필요하지만 approver가 제공되지 않음")
            decision = self.approver(run)
            if asyncio.iscoroutine(decision) or isinstance(decision, asyncio.Future):
                decision = await decision
            run.approved = bool(decision)
            self.audit.log(run.id, "approval", {"approved": run.approved})
            if not run.approved:
                self._move(run, State.REJECTED)
                return run
        else:
            run.approved = None  # not required

        # 4. apply + test (hooks)
        self._move(run, State.EXECUTING)
        if self.apply_hook:
            res = self.apply_hook(run)
            if asyncio.iscoroutine(res):
                await res
        else:
            run.notes.append("apply 단계 생략: 코드 적용기(git worktree)는 아직 연결되지 않음")

        if plan.run_tests:
            self._move(run, State.TESTING)
            if self.test_hook:
                ok = self.test_hook(run)
                if asyncio.iscoroutine(ok):
                    ok = await ok
                if not ok:
                    return self._fail(run, f"테스트 실패: {run.test_summary}" if run.test_summary else "테스트 실패")
            else:
                run.notes.append("test 단계 생략: 테스트 러너는 아직 연결되지 않음")

        if self.commit_hook:
            res = self.commit_hook(run)
            if asyncio.iscoroutine(res):
                await res

        self._move(run, State.COMPLETED)
        return run
