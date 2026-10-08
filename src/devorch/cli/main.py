"""devorch CLI."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer

from devorch.audit import AuditLogger
from devorch.config import devorch_dir, init_project, load_config, load_policy
from devorch.core.analyzer.impact_analyzer import analyze_impact
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.execution.sandbox import load_sandbox, sandbox_status
from devorch.core.execution.executor import ExecutionError, Executor
from devorch.core.execution.patch import extract_diff
from devorch.core.orchestrator.engine import WorkflowEngine, WorkflowRun
from devorch.core.orchestrator.review import budget_preflight
from devorch.core.router.model_router import plan_routing
from devorch.providers.base import ProviderRegistry
from devorch.providers.factory import build_registry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane
from devorch.store import Approval, StoreError

app = typer.Typer(help="DevOrch — AI Development Control Plane", no_args_is_help=True, add_completion=False)

from devorch.cli.bench_cmd import bench_app  # noqa: E402

app.add_typer(bench_app, name="bench")

PathOpt = typer.Option(Path("."), "--path", "-p", help="프로젝트 루트", file_okay=False)


def _bullets(items: list[str], indent: str = "  ") -> list[str]:
    return [f"{indent}- {i}" for i in items] if items else [f"{indent}- (없음)"]


def _short(files: list[str], limit: int = 8) -> list[str]:
    if len(files) <= limit:
        return files
    return files[:limit] + [f"... 외 {len(files) - limit}개"]


@app.command()
def init(path: Path = PathOpt, force: bool = typer.Option(False, "--force", help="기존 설정 덮어쓰기")) -> None:
    """프로젝트에 .devorch/ (config.yaml, policy.yaml)를 만든다."""
    created = init_project(path, force=force)
    if not created:
        typer.echo(f"이미 초기화되어 있습니다: {devorch_dir(path)}  (덮어쓰려면 --force)")
        return
    typer.echo("생성됨:")
    for p in created:
        typer.echo(f"  {p}")


@app.command()
def analyze(
    task: str = typer.Argument(..., help="자연어 작업 설명"),
    path: Path = PathOpt,
    as_json: bool = typer.Option(False, "--json", help="JSON으로 출력"),
) -> None:
    """작업을 분석해 유형·위험도·영향 범위·라우팅 계획을 보여준다 (LLM 호출 없음)."""
    try:
        analysis = analyze_task(task)
        policy = load_policy(path)
    except ValueError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(2)

    impact = analyze_impact(path, analysis)
    risk = assess_risk(analysis, impact)
    plan = plan_routing(analysis, risk, impact, policy)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "analysis": analysis.model_dump(mode="json"),
                    "risk": risk.model_dump(mode="json"),
                    "impact": impact.model_dump(mode="json"),
                    "plan": plan.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    lines = [
        f"Task Type: {analysis.task_type.value.upper()}",
        f"Scope: {analysis.scope}",
        f"Risk: {risk.level.value.upper()} (score {risk.score})",
    ]
    if risk.floor_reason:
        lines.append(f"  * {risk.floor_reason}")
    lines.append("  근거:")
    lines += [f"    {f.points:+d}  {f.reason}" for f in risk.factors] or ["    (가산 요인 없음)"]

    lines += ["", "Affected Areas:"] + _bullets([a.value for a in analysis.areas])

    lines += ["", f"Impact (스캔 {impact.files_scanned}개 파일)"]
    lines.append(f"  직접 변경 후보: {len(impact.direct_files)}개")
    lines += _bullets(_short(impact.direct_files), "    ")
    lines.append(f"  간접 영향 가능: {len(impact.indirect_files)}개")
    lines += _bullets([f"{f} (depth {impact.indirect_depth[f]})" for f in _short(impact.indirect_files) if f in impact.indirect_depth] or _short(impact.indirect_files), "    ")
    lines.append(f"  보안 영역 영향: {'YES' if impact.security_impact else 'NO'}")
    lines.append(f"  DB 영향: {'YES' if impact.database_impact else 'NO'}")
    lines += [f"  ※ {n}" for n in impact.notes]

    lines += ["", "Routing:"]
    lines += [f"  {s.role:<15} → {s.provider}  ({s.purpose})" for s in plan.steps]
    lines += [
        "",
        "Required:",
        f"  - 테스트 실행: {'YES' if plan.run_tests else 'NO'}",
        f"  - 사람 승인: {'YES' if plan.requires_human_approval else 'NO'}",
    ]
    lines += [f"      · {r}" for r in plan.approval_reasons]
    lines += [
        f"  - 검증 수준: {plan.verification_level}",
        f"  - 예상 비용: ~${plan.estimated_cost_usd:.2f} / 예상 시간: ~{plan.estimated_latency_s:.0f}s (기본 추정치)",
    ]
    lines += [f"  ※ {n}" for n in plan.notes if "추정치" not in n]
    typer.echo("\n".join(lines))


def _print_run(run: WorkflowRun) -> None:
    typer.echo(f"\nRun {run.id}: {run.state.value}")
    if run.risk:
        typer.echo(f"  Risk: {run.risk.level.value.upper()}  Rounds: {run.rounds}")
    if run.evaluation:
        typer.echo(f"  Evaluation: {run.evaluation.summary}")
        for f in run.evaluation.blocking:
            typer.echo(f"    [BLOCKING {f.severity.value.upper()}] ({f.source}) {f.title}")
        for f in run.evaluation.warnings:
            typer.echo(f"    [warning {f.severity.value}] ({f.source}) {f.title}")
    if run.failure_reason:
        typer.echo(f"  Failure: {run.failure_reason}")
    for n in run.notes:
        typer.echo(f"  note: {n}")
    if run.test_summary:
        typer.echo(f"  Tests: {run.test_summary}")
    if run.commit:
        typer.echo(f"  Branch: {run.branch}  Commit: {run.commit}")
        typer.echo("  (현재 브랜치는 건드리지 않았습니다. 검토 후 직접 merge 하거나 브랜치를 삭제하세요.)")
    elif run.worktree:
        typer.echo(f"  Worktree 보존: {run.worktree}  (branch {run.branch})")
    cost = "unknown" if run.cost_unknown else f"${run.total_cost_usd:.2f}"
    typer.echo(f"  Cost: {cost}")


@app.command()
def providers(path: Path = PathOpt) -> None:
    """설정된 LLM provider의 사용 가능 여부를 보여준다 (네트워크 호출 없음)."""
    registry, skipped = build_registry(load_config(path))
    for name in ("claude", "openai", "gemini"):
        if name in registry.names():
            typer.echo(f"  OK       {name}")
        else:
            typer.echo(f"  사용불가  {name}: {skipped.get(name, '?')}")


@app.command()
def run(
    task: str = typer.Argument(..., help="자연어 작업 설명"),
    path: Path = PathOpt,
    mock: bool = typer.Option(False, "--mock", help="가짜 Provider로 워크플로 전체를 시연"),
    yes: bool = typer.Option(False, "--yes", "-y", help="승인 프롬프트를 자동 승인"),
    apply: bool = typer.Option(False, "--apply", help="승인된 diff를 임시 git worktree에 적용·테스트·커밋 (기본: 제안만 저장)"),
    no_tests: bool = typer.Option(False, "--no-tests", help="--apply 시 테스트 단계를 명시적으로 생략"),
) -> None:
    """분석 → 구현 → 독립 리뷰 → 평가 → 승인까지 워크플로를 실행한다."""
    try:
        policy = load_policy(path)
        config = load_config(path)
        analysis = analyze_task(task)
    except ValueError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(2)

    if mock:
        registry = ProviderRegistry()
        for name in ("claude", "openai", "gemini"):
            registry.register(MockProvider(name, lambda req: "[]" if req.role != "implementation" else "(mock diff)"))
    else:
        registry, skipped = build_registry(config)
        impact = analyze_impact(path, analysis)
        plan = plan_routing(analysis, assess_risk(analysis, impact), impact, policy)
        needed = sorted({s.provider for s in plan.steps})
        missing = [n for n in needed if n not in registry.names()]
        if missing:
            typer.echo("이 작업에 필요한 provider를 사용할 수 없습니다:", err=True)
            for n in missing:
                typer.echo(f"  - {n}: {skipped.get(n, '알 수 없음')}", err=True)
            typer.echo("`devorch providers` 로 설정 상태를 확인하세요.", err=True)
            raise typer.Exit(2)
        if (problem := budget_preflight(registry, needed, policy.limits.max_cost_usd)) is not None:
            typer.echo(f"오류: {problem}", err=True)
            raise typer.Exit(2)
        typer.echo(f"위험도 {plan.level.value.upper()} — " + ", ".join(f"{s.role}={s.provider}" for s in plan.steps))
        typer.echo(f"실제 LLM API를 호출합니다 (예상 ~${plan.estimated_cost_usd:.2f}, 기본 추정치 · 수정 라운드마다 반복될 수 있음).")
        if policy.limits.max_cost_usd is not None:
            typer.echo(f"작업당 비용 상한: ${policy.limits.max_cost_usd} (호출 사이에서 검사 — 병렬로 진행 중인 호출만큼은 넘을 수 있음).")
        if not yes and not typer.confirm("진행할까요?"):
            raise typer.Exit(1)

    executor = None
    if apply:
        executor = Executor(
            path,
            policy,
            test_command=(config.get("project") or {}).get("test_command"),
            skip_tests=no_tests,
            blocked_env={c.get("api_key_env") for c in (config.get("providers") or {}).values() if c.get("api_key_env")},
            sandbox=load_sandbox(config),
        )
        try:
            executor.ensure_ready()
        except ExecutionError as e:
            typer.echo(f"--apply 불가: {e}", err=True)
            raise typer.Exit(2)
        if executor.git.is_dirty():
            typer.echo("※ 작업 트리에 커밋되지 않은 변경이 있습니다. AI는 HEAD 기준으로 작업하며 그 변경은 보이지 않습니다.")
        typer.echo("※ 테스트는 샌드박스 없이 이 컴퓨터에서 실행됩니다(키/토큰 환경변수는 제거, allowlist 명령만 허용).")

    audit = AuditLogger(devorch_dir(path) / "audit.jsonl")

    def approver(r: WorkflowRun) -> bool:
        if yes:
            return True
        return typer.confirm(f"위험도 {r.risk.level.value.upper()} 변경을 승인할까요?")  # type: ignore[union-attr]

    engine = WorkflowEngine(
        path, policy, registry, audit=audit, approver=approver,
        apply_hook=executor.apply if executor else None,
        test_hook=executor.test if executor else None,
        commit_hook=executor.commit if executor else None,
        context_provider=executor.file_context if executor else None,
    )
    result = asyncio.run(engine.run(task))
    if not apply and result.state.value == "COMPLETED":
        diff = extract_diff(result.implementation)
        if diff:
            out = devorch_dir(path) / "runs" / f"{result.id}.diff"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(diff, encoding="utf-8")
            result.notes.append(f"제안된 diff 저장: {out} (적용하려면 --apply)")
    _print_run(result)
    if result.state.value != "COMPLETED":
        raise typer.Exit(1)


@app.command("mcp")
def mcp_cmd(
    path: Path = PathOpt,
    mock: bool = typer.Option(False, "--mock", help="가짜 Provider 사용 (데모/연결 테스트용)"),
    no_tests: bool = typer.Option(False, "--no-tests", help="테스트 단계를 명시적으로 생략 (테스트가 없는 프로젝트용)"),
) -> None:
    """MCP 서버(stdio)를 시작한다. Claude Code: `claude mcp add devorch -- devorch mcp -p <프로젝트 경로>`"""
    from devorch.mcp_server import build_server

    plane = ControlPlane.from_project(path, mock=mock, skip_tests=no_tests)
    typer.echo(f"DevOrch MCP server — {plane.root} (providers: {', '.join(plane.providers.names()) or '없음'})", err=True)
    build_server(plane).run("stdio")


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@app.command()
def sandbox(path: Path = PathOpt) -> None:
    """docker 샌드박스 설정/사용 가능 여부를 점검한다 (이미지 안의 코드는 실행하지 않음)."""
    cfg = load_sandbox(load_config(path))
    if not cfg.enabled:
        typer.echo("sandbox.mode = none — 테스트가 호스트에서 직접 실행됩니다 (config.yaml 의 project.sandbox 로 docker 활성화).")
        return
    typer.echo(f"sandbox.mode = docker  image={cfg.image}  network={cfg.network}  memory={cfg.memory}  cpus={cfg.cpus}")
    results = asyncio.run(sandbox_status(cfg))
    for name, ok, detail in results:
        typer.echo(f"  {'OK  ' if ok else '실패'}  {name}  {detail}")
    if not all(ok for _, ok, _ in results):
        raise typer.Exit(1)


@app.command()
def dashboard(
    path: Path = PathOpt,
    port: int = typer.Option(8765, "--port", help="0 = 빈 포트 자동 선택"),
    host: str = typer.Option("127.0.0.1", "--host", help="기본은 loopback 전용. 외부에 노출하지 마세요(인증 없음)"),
) -> None:
    """작업·게이트·감사 로그·벤치마크를 보여주는 읽기 전용 로컬 대시보드."""
    from devorch.dashboard.server import make_server

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo("경고: loopback 이 아닌 주소에 바인딩합니다. 대시보드에는 인증이 없고 diff·감사 로그가 노출됩니다.", err=True)
    try:
        srv = make_server(path, host, port)
    except OSError as e:
        typer.echo(f"서버를 시작하지 못했습니다: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(f"http://127.0.0.1:{srv.server_address[1]}  (Ctrl+C 로 종료, 읽기 전용)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


@app.command()
def tasks(path: Path = PathOpt) -> None:
    """MCP로 만들어진 작업 목록과 게이트 상태를 보여준다."""
    plane = ControlPlane.from_project(path, mock=True)
    ids = plane.store.list_ids()
    if not ids:
        typer.echo("작업이 없습니다.")
    for tid in ids:
        s = plane.task_summary(tid)
        g = s["gates"]
        typer.echo(f"{tid}  {str(s['risk']).upper():<8} review={g['evaluation'] or '-':<20} tests={g['tests']:<12} approval={g['approval']:<12} {s['task'][:40]}")


@app.command()
def approve(
    task_id: str = typer.Argument(..., help="devorch tasks 로 확인한 task id"),
    path: Path = PathOpt,
    show_diff: bool = typer.Option(False, "--diff", help="승인 전에 전체 diff 출력"),
) -> None:
    """사람이 직접 변경을 승인/거부한다. 승인은 '지금 보고 있는 diff'에만 유효하다."""
    if not _is_interactive():
        typer.echo("승인은 사람이 터미널에서 직접 해야 합니다(비대화형 실행 거부).", err=True)
        raise typer.Exit(2)
    plane = ControlPlane.from_project(path, mock=True)
    try:
        record = plane.store.load(task_id)
    except StoreError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(2)

    run, plan = record.run, record.run.plan
    gates = plane.task_summary(task_id)["gates"]
    if not plan or not plan.requires_human_approval:
        typer.echo("이 작업은 사람 승인이 필요하지 않습니다.")
        raise typer.Exit(0)
    if not gates["reviewed"] or gates["evaluation"] == "NOT_READY" or gates["tests"] == "not_run":
        typer.echo(f"아직 승인 단계가 아닙니다 (review={gates['evaluation']}, tests={gates['tests']}).", err=True)
        raise typer.Exit(1)

    from devorch.core.execution.patch import validate_diff

    typer.echo(f"Task {task_id}: {run.task_text}")
    typer.echo(f"Risk: {run.risk.level.value.upper()}  ({'; '.join(plan.approval_reasons)})")  # type: ignore[union-attr]
    typer.echo(f"Diff {record.diff_sha}: " + ", ".join(validate_diff(record.diff)))
    typer.echo(f"Review: {run.evaluation.summary if run.evaluation else '-'}")
    for f in (run.evaluation.warnings if run.evaluation else []):
        typer.echo(f"  [warning {f.severity.value}] ({f.source}) {f.title}")
    typer.echo(f"Tests: {gates['tests']}  {run.test_summary}")
    if show_diff:
        typer.echo("\n" + record.diff)

    approved = typer.confirm("이 diff를 승인할까요? (n = 거부로 기록)")
    plane.store.save_approval(Approval(task_id=task_id, diff_sha=record.diff_sha or "", approved=approved, via="cli"))
    plane.audit.log(task_id, "approval", {"approved": approved, "diff_sha": record.diff_sha, "via": "cli"})
    typer.echo("승인됨. 에이전트가 devorch_apply_changes 를 호출하면 브랜치에 커밋됩니다." if approved else "거부로 기록했습니다.")


if __name__ == "__main__":
    app()
