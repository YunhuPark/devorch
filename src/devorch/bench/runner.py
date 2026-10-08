"""Runs benchmark cases through DevOrch's real review path (``run_reviews``)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from devorch.audit import AuditLogger
from devorch.bench.cases import Case
from devorch.bench.metrics import RunResult, score_run
from devorch.config import Policy
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.orchestrator.engine import WorkflowRun
from devorch.core.orchestrator.review import run_reviews
from devorch.models import ImpactReport, RoutingStep
from devorch.providers.base import ProviderRegistry

MODES = ("single", "multi")


def review_steps(policy: Policy, mode: str) -> list[RoutingStep]:
    """single: one general reviewer. multi: the policy's HIGH-level specialist reviewers + a general one."""
    if mode == "single":
        roles = {"review": policy.default_roles.get("review", "openai")}
    elif mode == "multi":
        roles = {r: p for r, p in policy.rules["high"].items() if r != "implementation"}
        roles.setdefault("review", policy.default_roles.get("review", "openai"))
    else:
        raise ValueError(f"unknown mode {mode!r} (choose from {', '.join(MODES)})")
    return [RoutingStep(role=r, provider=p, purpose=r) for r, p in roles.items()]


def expected_calls(cases: list[Case], policy: Policy, modes: list[str], repeats: int) -> int:
    return sum(len(review_steps(policy, m)) for m in modes) * len(cases) * repeats


async def run_case(case: Case, mode: str, repeat: int, policy: Policy, providers: ProviderRegistry) -> RunResult:
    analysis = analyze_task(case.task)
    impact = ImpactReport(direct_files=list(case.files))
    risk = assess_risk(analysis, impact)
    run = WorkflowRun(task_text=case.task, analysis=analysis, impact=impact, risk=risk, implementation=case.diff)
    steps = review_steps(policy, mode)
    t0 = time.monotonic()
    try:
        findings, completed_roles = await run_reviews(
            run, steps, providers, AuditLogger(None), label=f"[{case.id}] ", max_cost_usd=policy.limits.max_cost_usd
        )
    except Exception as e:  # e.g. unregistered provider — recorded, never a pass
        findings, completed_roles = [], []
        run.notes.append(f"{type(e).__name__}: {e}")
    latency = time.monotonic() - t0
    complete = {s.role for s in steps} <= set(completed_roles)
    return score_run(
        case, mode, repeat, findings, complete,
        cost_usd=run.total_cost_usd, cost_unknown=run.cost_unknown, latency_s=latency, notes=run.notes,
        models=dict(run.models), usage={p: dict(u) for p, u in run.usage.items()},
    )


async def run_benchmark(
    cases: list[Case],
    policy: Policy,
    providers: ProviderRegistry,
    modes: list[str],
    repeats: int = 1,
    concurrency: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[RunResult]:
    sem = asyncio.Semaphore(max(1, concurrency))
    jobs = [(c, m, r) for m in modes for c in cases for r in range(repeats)]
    done = 0

    async def one(c: Case, m: str, r: int) -> RunResult:
        nonlocal done
        async with sem:
            res = await run_case(c, m, r, policy, providers)
        done += 1
        if on_progress:
            on_progress(done, len(jobs))
        return res

    return list(await asyncio.gather(*(one(*j) for j in jobs)))
