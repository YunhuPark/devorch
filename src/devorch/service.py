"""ControlPlane: the stateful service behind the MCP tools.

Flow (the client agent writes the code in its own working tree; DevOrch controls what may land):

    analyze_task → [client edits files] → request_review → run_tests → (human approve) → apply_changes

Every gate is bound to the SHA of the exact diff that was reviewed. If the diff changes, the review,
test result and approval for the old diff stop counting.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from devorch.audit import AuditLogger
from devorch.config import Policy, devorch_dir, load_config, load_policy
from devorch.core.analyzer.impact_analyzer import analyze_impact
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.evaluator.evaluator import evaluate
from devorch.core.execution.sandbox import load_sandbox
from devorch.core.execution.executor import ExecutionError, Executor
from devorch.core.execution.patch import PatchError, validate_diff
from devorch.core.orchestrator.engine import WorkflowRun
from devorch.core.orchestrator.review import REVIEWER_SYSTEM, budget_preflight, build_review_prompt, run_reviews
from devorch.core.orchestrator.state import State
from devorch.core.router.cost_model import estimate_call, merge_prices, prices_from_config, prices_from_registry
from devorch.core.router.model_router import plan_routing
from devorch.git.manager import GitError
from devorch.models import Finding, RiskLevel
from devorch.providers.base import ProviderRegistry
from devorch.providers.factory import build_registry
from devorch.providers.mock import MockProvider
from devorch.security.secret_redactor import has_secret_literal, redact
from devorch.store import TaskRecord, TaskStore

# READY must never read as "safe": it only says the configured reviewers found nothing that blocks. How well AI reviewers find real bugs
# has only been measured on seeded defects, not on real code (HANDOFF.md).
READY_CAVEAT = (
    "READY 는 '지정된 리뷰어가 차단할 문제를 찾지 못했다'는 뜻이지 안전 보증이 아닙니다. AI 리뷰어가 실제 코드의 버그를 얼마나 찾는지는 "
    "아직 검증되지 않았으니(인위적으로 심은 결함으로만 측정), 중요한 변경은 사람이 직접 확인하세요."
)
MAX_REVIEW_DIFF_BYTES = 200_000
MAX_TEST_OUTPUT = 2000


class ControlPlaneError(RuntimeError):
    """Bad input / unusable state. Surfaced to the MCP client as a tool error."""


def sha_of(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16]


def _finding(f: Finding) -> dict[str, Any]:
    return {"severity": f.severity.value, "source": f.source, "category": f.category, "title": f.title, "detail": f.detail, "file": f.file}


class ControlPlane:
    def __init__(
        self,
        root: Path,
        policy: Policy,
        config: dict[str, Any],
        providers: ProviderRegistry,
        executor: Executor,
        store: TaskStore | None = None,
        audit: AuditLogger | None = None,
        provider_notes: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy, self.config, self.providers, self.executor = policy, config, providers, executor
        self.store = store or TaskStore(self.root)
        self.audit = audit or AuditLogger(devorch_dir(self.root) / "audit.jsonl")
        self.provider_notes = provider_notes or {}

    @classmethod
    def from_project(cls, root: Path, mock: bool = False, skip_tests: bool = False) -> "ControlPlane":
        root = Path(root).resolve()
        policy, config = load_policy(root), load_config(root)
        notes: dict[str, str] = {}
        if mock:
            providers = ProviderRegistry()
            for name in ("claude", "openai", "gemini"):
                providers.register(MockProvider(name, ["[]"]))
        else:
            providers, notes = build_registry(config)
        executor = Executor(
            root,
            policy,
            test_command=(config.get("project") or {}).get("test_command"),
            skip_tests=skip_tests,
            blocked_env={c.get("api_key_env") for c in (config.get("providers") or {}).values() if c.get("api_key_env")},
            sandbox=load_sandbox(config),
        )
        return cls(root, policy, config, providers, executor, provider_notes=notes)

    # ---------------------------------------------------------------- helpers
    def _prices(self) -> dict[str, dict[str, float]]:
        """Configured per-Mtok prices; a registered adapter's own prices take precedence over config.yaml."""
        return merge_prices(prices_from_config(self.config), prices_from_registry(self.providers))

    def _estimate_review(self, record: TaskRecord, steps, log: bool = True) -> dict[str, Any]:
        """Cost of the reviews about to run, from the REAL prompts (so the actual diff size is counted). usd is None if any
        reviewer has no price — unknown, never 0. Logged so estimates can later be compared with the actual cost."""
        prices = self._prices()
        budget = self.policy.limits.review_max_tokens
        ests = [estimate_call(s.provider, REVIEWER_SYSTEM + build_review_prompt(record.run, s), prices, max_output_tokens=budget) for s in steps]
        known = not any(e.usd is None for e in ests)
        info = {
            "estimated_usd": round(sum(e.usd for e in ests), 6) if known else None,
            "estimated_low_usd": round(sum(e.usd_low for e in ests), 6) if known else None,
            "estimated_high_usd": round(sum(e.usd_high for e in ests), 6) if known else None,
            "input_tokens": sum(e.input_tokens for e in ests), "output_tokens": sum(e.output_tokens for e in ests), "calls": len(ests),
        }
        if log:
            self._log(record, "review_estimate", **info)
        return info

    def _load(self, task_id: str) -> TaskRecord:
        try:
            return self.store.load(task_id)
        except ValueError as e:
            raise ControlPlaneError(str(e)) from e

    def _log(self, record: TaskRecord, event: str, **data: Any) -> None:
        self.audit.log(record.id, f"mcp:{event}", data)

    def _require_git(self) -> None:
        try:
            self.executor.ensure_ready()
        except ExecutionError as e:
            raise ControlPlaneError(str(e)) from e

    def _capture_diff(self, record: TaskRecord, diff: str | None = None) -> str:
        """Snapshot the diff into the record (the working tree vs HEAD, or ``diff`` if one is handed in — the CLI's
        ``review --git-range/--diff-file``). Same validation either way. Returns the diff SHA."""
        if diff is None:
            self._require_git()
            try:
                diff = self.executor.git.working_diff()
            except GitError as e:
                raise ControlPlaneError(f"diff를 만들지 못함: {e}") from e
        if not diff.strip():
            raise ControlPlaneError("변경 사항이 없습니다. 먼저 작업 트리의 코드를 수정하세요.")
        try:
            validate_diff(diff)
        except PatchError as e:
            raise ControlPlaneError(f"diff 검증 실패: {e}") from e
        if has_secret_literal(diff):
            raise ControlPlaneError("diff에 credential 형태의 문자열이 있어 외부 LLM으로 보내지 않습니다. 제거 후 다시 시도하세요.")
        if len(diff.encode("utf-8")) > MAX_REVIEW_DIFF_BYTES:
            raise ControlPlaneError(f"diff가 너무 큽니다(>{MAX_REVIEW_DIFF_BYTES // 1000}KB). 작업을 나눠서 진행하세요.")

        sha = sha_of(diff)
        if sha != record.diff_sha:
            # a different diff invalidates everything derived from the old one
            if record.run.commit:
                record.run.branch = record.run.commit = None  # keep the committed branch for the user
                record.run.state = State.PLANNED
            elif record.run.worktree or record.run.branch:
                self.executor.discard(record.run)
            record.reviewed_sha = record.tested_sha = None
            record.run.evaluation, record.run.findings = None, []
        record.diff, record.diff_sha = diff, sha
        record.run.implementation = diff
        return sha

    def _gate_status(self, record: TaskRecord) -> dict[str, Any]:
        run, plan = record.run, record.run.plan
        assert plan
        ev = run.evaluation
        return {
            "reviewed": record.reviewed_sha is not None and record.reviewed_sha == record.diff_sha,
            "evaluation": ev.status if ev else None,
            "tests": "passed" if record.tested_sha == record.diff_sha and record.diff_sha else ("not_required" if not plan.run_tests else "not_run"),
            "approval": self._approval_state(record),
        }

    def _approval_state(self, record: TaskRecord) -> str:
        plan = record.run.plan
        if plan is None or not plan.requires_human_approval:
            return "not_required"
        ap = self.store.load_approval(record.id)
        if ap is None or ap.diff_sha != record.diff_sha:
            return "pending" if ap is None else "stale"  # stale = approval was for a different diff
        return "approved" if ap.approved else "rejected"

    # ------------------------------------------------------------------ tools
    def inspect_project(self) -> dict[str, Any]:
        git: dict[str, Any]
        try:
            self.executor.git.ensure_ready()
            git = {"ready": True, "head": self.executor.git.head(), "dirty": self.executor.git.is_dirty()}
        except GitError as e:
            git = {"ready": False, "problem": str(e)}
        p = self.policy
        return {
            "root": str(self.root),
            "git": git,
            "providers": {"available": self.providers.names(), "unavailable": self.provider_notes},
            "policy": {
                "rules": p.rules,
                "human_approval_at": p.approval.require_human_approval_at,
                "approval_areas": p.approval.require_for_areas,
                "critical_required_reviewers": p.critical.required_reviewers,
                "max_review_rounds": p.limits.max_retries + 1,
            },
            "test_command": self.executor.test_command or "(자동 감지)",
        }

    def analyze_task(self, task: str, level: str | None = None) -> dict[str, Any]:
        """``level`` (CLI only, never an MCP argument) can RAISE the automatically assessed risk level, never lower it."""
        try:
            analysis = analyze_task(task)
        except ValueError as e:
            raise ControlPlaneError(str(e)) from e
        impact = analyze_impact(self.root, analysis)
        risk = assess_risk(analysis, impact)
        if level is not None:
            try:
                wanted = RiskLevel(level.strip().lower())
            except ValueError as e:
                raise ControlPlaneError(f"level 은 {' | '.join(l.value for l in RiskLevel)} 중 하나여야 합니다.") from e
            if wanted.rank < risk.level.rank:
                raise ControlPlaneError(f"자동 평가({risk.level.value})보다 위험 수준을 낮출 수 없습니다. 높이는 것만 가능합니다.")
            if wanted is not risk.level:
                risk = risk.model_copy(update={"level": wanted, "floor_reason": f"사용자 지정 위험 수준({wanted.value}) — 자동 평가는 {risk.level.value}"})
        plan = plan_routing(analysis, risk, impact, self.policy, prices=self._prices())
        run = WorkflowRun(task_text=task.strip(), state=State.PLANNED, analysis=analysis, impact=impact, risk=risk, plan=plan)
        record = TaskRecord(run=run)
        self.store.save(record)
        self._log(record, "analyze_task", task=run.task_text, risk=risk.level.value)

        reviewers = [{"role": s.role, "provider": s.provider} for s in plan.review_steps]
        # Here the client agent writes the code in its own tree; DevOrch's own LLM calls are the reviews only (and the cost cap counts
        # only those). The plan's implementation step is not DevOrch spend, so it stays out of the totals instead of inflating them.
        spend = [line for line in plan.cost_breakdown if line.role != "implementation"]
        return {
            "task_id": run.id,
            "task_type": analysis.task_type.value,
            "areas": [a.value for a in analysis.areas],
            "risk": {"level": risk.level.value, "score": risk.score, "floor_reason": risk.floor_reason},
            "impact": {
                "direct_files": impact.direct_files[:20],
                "indirect_files": impact.indirect_files[:20],
                "security_impact": impact.security_impact,
                "database_impact": impact.database_impact,
                "notes": impact.notes,
            },
            "required_reviews": reviewers,
            "run_tests": plan.run_tests,
            "requires_human_approval": plan.requires_human_approval,
            "approval_reasons": plan.approval_reasons,
            "verification_level": plan.verification_level,
            "estimated_cost_usd": round(sum(x.usd for x in spend), 6),
            "estimated_cost_range_usd": [round(sum(x.usd_low for x in spend), 6), round(sum(x.usd_high for x in spend), 6)],
            "cost_breakdown": [line.model_dump() for line in spend],
            "cost_notes": [n for n in plan.notes if n.startswith("비용")]
            + ["DevOrch가 호출하는 리뷰어 비용만 합산합니다. 구현은 클라이언트(에이전트)가 하므로 그 비용은 포함되지 않습니다(DevOrch가 측정하지 않음)."],
            "next_step": "작업 트리에서 코드를 구현한 뒤 devorch_request_review 를 호출하세요.",
        }

    def get_risk(self, task_id: str) -> dict[str, Any]:
        run = self._load(task_id).run
        assert run.risk
        return {
            "task_id": run.id,
            "level": run.risk.level.value,
            "score": run.risk.score,
            "floor_reason": run.risk.floor_reason,
            "factors": [{"name": f.name, "points": f.points, "reason": f.reason} for f in run.risk.factors],
        }

    def get_impact(self, task_id: str) -> dict[str, Any]:
        run = self._load(task_id).run
        assert run.impact
        i = run.impact
        return {
            "task_id": run.id,
            "files_scanned": i.files_scanned,
            "direct_files": i.direct_files,
            "indirect_files": [{"file": f, "depth": i.indirect_depth.get(f)} for f in i.indirect_files],
            "security_impact": i.security_impact,
            "database_impact": i.database_impact,
            "notes": i.notes,
        }

    def get_diff(self, task_id: str) -> dict[str, Any]:
        record = self._load(task_id)
        sha = self._capture_diff(record)
        self.store.save(record)
        self._log(record, "get_diff", diff_sha=sha)
        return {"task_id": record.id, "diff_sha": sha, "files": validate_diff(record.diff), "diff": record.diff}

    def preview_review(self, task_id: str, diff: str | None = None) -> dict[str, Any]:
        """What a review would involve, with NO LLM call: the diff (validated), the reviewers, and the cost estimate."""
        record = self._load(task_id)
        plan = record.run.plan
        assert plan
        sha = self._capture_diff(record, diff)
        self.store.save(record)
        steps = plan.review_steps
        est = self._estimate_review(record, steps, log=False)
        return {
            "task_id": record.id,
            "diff_sha": sha,
            "diff_bytes": len(record.diff.encode("utf-8")),
            "files": validate_diff(record.diff),
            "risk": record.run.risk.level.value if record.run.risk else None,
            "reviewers": [f"{s.role}:{s.provider}" for s in steps],
            "missing_providers": sorted({s.provider for s in steps} - set(self.providers.names())),
            **est,
        }

    async def request_review(self, task_id: str, diff: str | None = None) -> dict[str, Any]:
        record = self._load(task_id)
        run, plan = record.run, record.run.plan
        assert plan
        steps = plan.review_steps
        max_rounds = self.policy.limits.max_retries + 1

        missing = sorted({s.provider for s in steps} - set(self.providers.names()))
        if missing:
            detail = "; ".join(f"{m}: {self.provider_notes.get(m, '등록되지 않음')}" for m in missing)
            raise ControlPlaneError(f"리뷰에 필요한 provider를 사용할 수 없습니다 — {detail}")
        cap = self.policy.limits.max_cost_usd
        if (problem := budget_preflight(self.providers, {s.provider for s in steps}, cap)) is not None:
            raise ControlPlaneError(problem)

        sha = self._capture_diff(record, diff)
        ev = run.evaluation
        if record.reviewed_sha == sha and ev is not None and not ev.missing_reviews:
            self.store.save(record)
            return self._review_result(record, cached=True)

        if record.review_rounds >= max_rounds:
            return {
                "task_id": record.id,
                "status": "blocked",
                "reason": f"리뷰 라운드 한도({max_rounds}회)에 도달했습니다. 사람이 직접 검토하거나 작업을 다시 시작하세요.",
            }

        estimate = self._estimate_review(record, steps)
        if cap is not None and estimate["estimated_usd"] is not None and run.total_cost_usd + estimate["estimated_usd"] > cap:
            raise ControlPlaneError(
                f"예상 리뷰 비용 ${estimate['estimated_usd']:.4f} (범위 ${estimate['estimated_low_usd']:.4f}~${estimate['estimated_high_usd']:.4f}) 가 "
                f"남은 비용 상한 ${max(0.0, cap - run.total_cost_usd):.4f} (limits.max_cost_usd=${cap})을 넘을 것으로 보여 호출하지 않았습니다. 상한을 올리거나 변경을 나누세요."
            )
        record.review_rounds += 1
        required = [s.role for s in steps]
        findings: list[Finding] = []
        completed: list[str] = []
        if steps:
            findings, completed = await run_reviews(run, steps, self.providers, self.audit, label=f"review {record.review_rounds}: ", max_cost_usd=cap, max_tokens=self.policy.limits.review_max_tokens)
        run.findings = findings
        run.evaluation = evaluate(findings, required, completed)
        record.reviewed_sha = sha
        self.store.save(record)
        self._log(record, "request_review", diff_sha=sha, round=record.review_rounds, summary=run.evaluation.summary)
        return self._review_result(record, cached=False, estimate=estimate)

    def _review_result(self, record: TaskRecord, cached: bool, estimate: dict[str, Any] | None = None) -> dict[str, Any]:
        run = record.run
        ev = run.evaluation
        assert ev
        if ev.status == "NOT_READY" and ev.blocking:
            nxt = "차단 이슈를 수정한 뒤 devorch_request_review 를 다시 호출하세요."
        elif ev.status == "NOT_READY":
            nxt = "필수 리뷰가 완료되지 않았습니다(호출 실패 또는 응답 파싱 실패). 같은 diff로 devorch_request_review 를 다시 호출하세요."
        elif run.plan and run.plan.run_tests:
            nxt = "devorch_run_tests 를 호출하세요."
        else:
            nxt = "devorch_apply_changes 를 호출하세요."
        return {
            "task_id": record.id,
            "diff_sha": record.diff_sha,
            "status": ev.status,
            "caveat": READY_CAVEAT if ev.status in ("READY", "READY_WITH_WARNINGS") else None,
            "cached": cached,
            "blocking": [_finding(f) for f in ev.blocking],
            "warnings": [_finding(f) for f in ev.warnings],
            "missing_reviews": ev.missing_reviews,
            "rounds_used": record.review_rounds,
            "rounds_max": self.policy.limits.max_retries + 1,
            "cost_usd": None if run.cost_unknown else round(run.total_cost_usd, 4),
            # estimated BEFORE the calls from the real prompt size and the configured prices (None = a reviewer has no price)
            "estimated_review_cost_usd": estimate["estimated_usd"] if estimate else None,
            "estimated_review_cost_range_usd": [estimate["estimated_low_usd"], estimate["estimated_high_usd"]] if estimate and estimate["estimated_usd"] is not None else None,
            "usage": run.usage,  # tokens per provider, cumulative over this task's LLM calls
            "reviewers": [f"{s.role}:{s.provider}" for s in (run.plan.review_steps if run.plan else [])],
            "notes": run.notes[-5:],
            "next_step": nxt,
        }

    def _check_ready_for_tests(self, record: TaskRecord) -> dict[str, Any] | None:
        ev = record.run.evaluation
        if not record.diff_sha:
            return {"status": "blocked", "reason": "diff가 없습니다. devorch_request_review 를 먼저 호출하세요."}
        if record.reviewed_sha != record.diff_sha or ev is None:
            return {"status": "blocked", "reason": "현재 diff에 대한 리뷰가 없습니다. devorch_request_review 를 먼저 호출하세요."}
        if ev.status == "NOT_READY":
            return {"status": "blocked", "reason": "리뷰가 NOT_READY 입니다. 차단 이슈를 해결하고 다시 리뷰받기 전에는 코드를 실행하지 않습니다."}
        return None

    async def run_tests(self, task_id: str) -> dict[str, Any]:
        record = self._load(task_id)
        run, plan = record.run, record.run.plan
        assert plan
        if run.commit:
            return {"task_id": record.id, "status": "already_committed", "branch": run.branch, "commit": run.commit}
        if (blocked := self._check_ready_for_tests(record)) is not None:
            return {"task_id": record.id, **blocked}
        if not plan.run_tests:
            return {"task_id": record.id, "status": "skipped", "reason": "이 작업 유형은 테스트가 필요하지 않습니다."}

        self.executor.discard(run)  # always test from a clean worktree
        record.tested_sha = None
        try:
            await self.executor.apply(run)
        except ExecutionError as e:
            self.store.save(record)
            self._log(record, "run_tests", status="apply_failed", reason=str(e))
            return {"task_id": record.id, "status": "apply_failed", "reason": str(e)}

        ok = await self.executor.test(run)
        record.tested_sha = record.diff_sha if ok else None
        self.store.save(record)
        self._log(record, "run_tests", status="passed" if ok else "failed", summary=run.test_summary)
        return {
            "task_id": record.id,
            "status": "passed" if ok else "failed",
            "summary": run.test_summary or "(테스트 생략)",
            "output_tail": redact(run.test_output)[-MAX_TEST_OUTPUT:],
            "next_step": "devorch_apply_changes 를 호출하세요." if ok else "실패 원인을 수정한 뒤 devorch_request_review → devorch_run_tests 를 다시 진행하세요.",
        }

    async def apply_changes(self, task_id: str) -> dict[str, Any]:
        record = self._load(task_id)
        run, plan = record.run, record.run.plan
        assert plan
        if run.commit and run.state is State.COMPLETED:
            return {"task_id": record.id, "status": "already_committed", "branch": run.branch, "commit": run.commit}
        if (blocked := self._check_ready_for_tests(record)) is not None:
            return {"task_id": record.id, **blocked}

        if plan.run_tests:
            if record.tested_sha != record.diff_sha:
                return {"task_id": record.id, "status": "blocked", "reason": "현재 diff에 대해 통과한 테스트가 없습니다. devorch_run_tests 를 먼저 호출하세요."}
            if not (run.worktree and Path(run.worktree).is_dir()):
                return {"task_id": record.id, "status": "blocked", "reason": "테스트한 worktree가 사라졌습니다. devorch_run_tests 를 다시 호출하세요."}

        approval = self._approval_state(record)
        if approval != "not_required":
            if approval == "rejected":
                self.executor.discard(run)  # drop the tested-but-rejected worktree and its empty branch
                record.tested_sha = None
                self.store.save(record)
                self._log(record, "apply_changes", status="rejected_by_human")
                return {"task_id": record.id, "status": "blocked", "reason": "사람이 이 변경을 거부했습니다."}
            if approval != "approved":
                return {
                    "task_id": record.id,
                    "status": "approval_required",
                    "reason": "stale: 이전 diff에 대한 승인이라 무효입니다." if approval == "stale" else "이 변경은 사람의 승인이 필요합니다.",
                    "approval_reasons": plan.approval_reasons,
                    "how_to_approve": f"사용자가 직접 터미널에서 `devorch approve {record.id}` 를 실행해야 합니다. 에이전트는 승인할 수 없습니다.",
                }

        try:
            if not plan.run_tests:
                self.executor.discard(run)
                await self.executor.apply(run)
            run.approved = True if plan.requires_human_approval else None
            await self.executor.commit(run)
        except ExecutionError as e:
            self.store.save(record)
            self._log(record, "apply_changes", status="failed", reason=str(e))
            return {"task_id": record.id, "status": "failed", "reason": str(e)}

        run.state = State.COMPLETED
        self.store.save(record)
        self._log(record, "apply_changes", status="committed", branch=run.branch, commit=run.commit, diff_sha=record.diff_sha)
        return {
            "task_id": record.id,
            "status": "committed",
            "branch": run.branch,
            "commit": run.commit,
            "note": "검토·테스트된 diff가 별도 브랜치에 커밋되었습니다. 현재 브랜치와 작업 트리는 변경되지 않았습니다. "
            "사용자가 브랜치를 확인한 뒤 직접 merge 하세요(작업 트리의 미커밋 수정은 필요하면 되돌리세요).",
        }

    # ---------------------------------------------------------------- resources
    def policy_summary(self) -> dict[str, Any]:
        return self.inspect_project()["policy"]

    def task_summary(self, task_id: str) -> dict[str, Any]:
        record = self._load(task_id)
        run = record.run
        return {
            "task_id": record.id,
            "task": run.task_text,
            "risk": run.risk.level.value if run.risk else None,
            "diff_sha": record.diff_sha,
            "rounds": record.review_rounds,
            "gates": self._gate_status(record),
            "branch": run.branch,
            "commit": run.commit,
        }
