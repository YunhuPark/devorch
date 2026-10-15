"""devorch CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import typer

from devorch.cli.app import DevorchTyper

from devorch.audit import AuditLogger, verify_audit
from devorch.config import devorch_dir, init_project, load_config, load_policy
from devorch.core.analyzer.impact_analyzer import analyze_impact
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.execution.sandbox import load_sandbox, sandbox_status
from devorch.core.execution.executor import ExecutionError, Executor
from devorch.core.execution.patch import extract_diff
from devorch.core.orchestrator.engine import WorkflowEngine, WorkflowRun, implementation_prompt_counts
from devorch.core.orchestrator.review import budget_preflight
from devorch.core.router.cost_model import merge_prices, prices_from_config, prices_from_registry
from devorch.core.router.model_router import plan_routing
from devorch.providers.base import ProviderRegistry
from devorch.providers.factory import build_registry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane, ControlPlaneError
from devorch.git.manager import GitError
from devorch.store import Approval, StoreError

app = DevorchTyper(help="DevOrch — AI Development Control Plane", no_args_is_help=True, add_completion=False)

from devorch.cli.bench_cmd import bench_app  # noqa: E402

app.add_typer(bench_app, name="bench")


def _survive_narrow_console() -> None:
    # When stdout/stderr is redirected (`> file`, a pipe, CI, an IDE capture) Python on Windows uses the ANSI code page (cp949 on a
    # Korean PC, cp1252 on a CI runner), which can't encode characters this CLI prints (Korean, an em dash, box lines): a traceback
    # instead of the output. Keep the encoding but replace what it can't represent — a '?' is better than a crash, most of all in
    # `doctor`. Done at import time, not in a Typer callback: `--help` is handled before any callback runs.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


_survive_narrow_console()

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
    cfg = load_config(path)
    envs = [(n, c.get("api_key_env")) for n, c in (cfg.get("providers") or {}).items()]
    typer.echo("\n다음 단계:")
    typer.echo(f"  1) {devorch_dir(path) / 'config.yaml'} 에서 쓸 provider 의 model, input_per_mtok, output_per_mtok 를 채우세요.")
    typer.echo("     (단가를 비워 두면 비용이 '알 수 없음'으로 표시되고 비용 상한을 쓸 수 없습니다. 모델명·단가는 추측하지 않으니 공급사 페이지에서 확인하세요.)")
    typer.echo("  2) API 키는 환경변수로만 넣으세요. 이 터미널 기준 현재 상태:")
    for name, env in envs:
        typer.echo(f"       {name:<8} {env}: {'있음' if env and os.environ.get(env) else '없음'}")
    typer.echo("     PowerShell 예: $env:OPENAI_API_KEY = (Get-Clipboard -Raw).Trim()   (키를 복사한 뒤. Ctrl+V 로 입력창에 붙여넣지 마세요)")
    typer.echo("  3) devorch doctor  →  devorch analyze \"작업 설명\"  →  devorch review --git-range HEAD~1..HEAD --task \"작업 설명\"")


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
        prices = prices_from_config(load_config(path))
    except ValueError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(2)

    impact = analyze_impact(path, analysis)
    risk = assess_risk(analysis, impact)
    # Read-only: the implementation step's input is estimated from the real file contents (HEAD), as `run` does.
    reader = Executor(path, policy, skip_tests=True)
    try:
        reader.ensure_ready()
    except ExecutionError:
        reader = None
    sizing = WorkflowRun(task_text=task, analysis=analysis, impact=impact, risk=risk)
    plan = plan_routing(
        analysis, risk, impact, policy, prices=prices,
        implementation_counts=implementation_prompt_counts(sizing, reader.file_context if reader else None),
    )

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
        f"  - 예상 비용: ~${plan.estimated_cost_usd:.3f}"
        + (f" (범위 ${plan.estimated_cost_low_usd:.3f} ~ ${plan.estimated_cost_high_usd:.3f})" if plan.estimated_cost_high_usd > plan.estimated_cost_low_usd else "")
        + f" / 예상 시간: ~{plan.estimated_latency_s:.0f}s (시간은 기본 추정치)",
    ]
    lines += [
        f"      · {x.role:<14} {x.provider:<7} ${x.usd:.4f}" + (f" (${x.usd_low:.4f}~${x.usd_high:.4f})" if x.usd_high > x.usd_low else "") + f"  [{x.basis}]"
        for x in plan.cost_breakdown
    ]
    lines += [f"  ※ {n}" for n in plan.notes]
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
    cost = "unknown" if run.cost_unknown else f"${run.total_cost_usd:.4f}"
    plan = run.plan
    if plan:
        lo, hi = plan.estimated_cost_low_usd, plan.estimated_cost_high_usd
        est = f"예상 ${plan.estimated_cost_usd:.4f}" + (f" (범위 ${lo:.4f}~${hi:.4f})" if hi > lo else "")
        flat = [f"{x.role}({x.provider})" for x in plan.cost_breakdown if x.basis == "기본 고정값"]
        basis = f"{', '.join(flat)}는 고정값(실측 아님)" if flat else "구현 단계 출력은 가정한 범위"
        typer.echo(f"  Cost: {cost}   [{est}; {basis}이며 수정 라운드(rounds={run.rounds})는 예상에 한 번만 들어 있음]")
    else:
        typer.echo(f"  Cost: {cost}")
    for role, u in run.role_usage.items():
        c = u["cost_usd"]
        label = "구현 단계" if role == "implementation" else f"리뷰 {role}"
        typer.echo(
            f"    - {label}: 호출 {u['calls']}회, 토큰 입력 {u['input_tokens']:,} / 출력 {u['output_tokens']:,}, "
            + ("비용 unknown(단가 없음)" if c is None else f"비용 ${c:.4f}")
        )
    failed = {p: v["failed_calls"] for p, v in run.usage.items() if v.get("failed_calls")}
    if failed:
        typer.echo("    ※ 실패한 호출도 청구됨: " + ", ".join(f"{p} {n}회" for p, n in failed.items()))


@app.command()
def doctor(path: Path = PathOpt, as_json: bool = typer.Option(False, "--json", help="JSON으로 출력")) -> None:
    """이 컴퓨터에서 DevOrch가 동작하지 못하게 막는 것을 점검하고 해결 방법을 알려준다 (네트워크·LLM 호출 없음, 키 값은 출력하지 않음)."""
    from devorch.doctor import render, run_checks

    checks = run_checks(path)
    failed = any(c.status == "fail" for c in checks)
    if as_json:
        typer.echo(json.dumps({"ok": not failed, "checks": [c.__dict__ for c in checks]}, ensure_ascii=False, indent=2))
    else:
        typer.echo(render(checks))
    if failed:
        raise typer.Exit(1)


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

    # Even without --apply the implementer must SEE the files (as of HEAD, read-only) or it can only guess context lines
    # and the diff it writes won't apply. Without --apply nothing is applied, tested or created.
    reader = Executor(path, policy, skip_tests=True)
    try:
        reader.ensure_ready()
    except ExecutionError:
        reader = None

    if mock:
        registry = ProviderRegistry()
        for name in ("claude", "openai", "gemini"):
            registry.register(MockProvider(name, lambda req: "[]" if req.role != "implementation" else "(mock diff)"))
    else:
        registry, skipped = build_registry(config)
        impact = analyze_impact(path, analysis)
        risk = assess_risk(analysis, impact)
        sizing = WorkflowRun(task_text=task, analysis=analysis, impact=impact, risk=risk)
        plan = plan_routing(
            analysis, risk, impact, policy, prices=merge_prices(prices_from_config(config), prices_from_registry(registry)),
            implementation_counts=implementation_prompt_counts(sizing, reader.file_context if reader else None),
        )
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
        bases = sorted({x.basis for x in plan.cost_breakdown})
        rng = f", 범위 ${plan.estimated_cost_low_usd:.3f}~${plan.estimated_cost_high_usd:.3f}" if plan.estimated_cost_high_usd > plan.estimated_cost_low_usd else ""
        typer.echo(
            f"실제 LLM API를 호출합니다 (예상 ~${plan.estimated_cost_usd:.3f}{rng}; 근거: {', '.join(bases)}. "
            "구현 단계의 출력은 작은 수정 1건만 실측한 가정이고, 리뷰할 diff 크기도 가정이며, 수정 라운드마다 반복될 수 있음)."
        )
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
    if reader is None:
        typer.echo("※ git 저장소(최상위, 커밋 있음)가 아니라 구현 모델에 파일 내용을 보내지 못합니다 — 만들어진 diff가 실제 파일과 맞지 않을 수 있습니다.")

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
        context_provider=(executor or reader).file_context if (executor or reader) else None,
    )
    result = asyncio.run(engine.run(task))
    if not apply and result.state.value == "COMPLETED":
        diff = extract_diff(result.implementation)
        if diff:
            out = devorch_dir(path) / "runs" / f"{result.id}.diff"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(diff.encode("utf-8"))  # byte-exact: text mode would turn every LF into CRLF on Windows and `git apply` then fails on LF files
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
    typer.echo(f"DevOrch MCP server - {plane.root} (providers: {', '.join(plane.providers.names()) or '없음'})", err=True)
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
def usage(
    path: Path = PathOpt,
    days: float = typer.Option(None, "--days", min=0.0, help="최근 N일만 집계 (생략하면 전체)"),
    include_mock: bool = typer.Option(False, "--include-mock", help="--mock 호출도 포함"),
    as_json: bool = typer.Option(False, "--json", help="JSON으로 출력"),
) -> None:
    """감사 로그(.devorch/audit.jsonl)에서 작업별·provider별·날짜별 실제 지출을 집계한다 (LLM 호출 없음)."""
    from devorch.usage import load_events, render, summarize

    summary = summarize(load_events(devorch_dir(path) / "audit.jsonl"), days=days, include_mock=include_mock)
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2, default=str) if as_json else render(summary))


@app.command()
def audit(
    path: Path = PathOpt,
    as_json: bool = typer.Option(False, "--json", help="JSON으로 출력"),
) -> None:
    """감사 로그(.devorch/audit.jsonl)의 서명 사슬을 검증한다: 중간 수정·삭제·순서 변경·끝부분 잘림을 찾는다 (LLM 호출 없음). 문제가 있으면 종료 코드 1."""
    rep = verify_audit(devorch_dir(path) / "audit.jsonl")
    if as_json:
        typer.echo(json.dumps({"ok": rep.ok, "total": rep.total, "chained": rep.chained, "legacy": rep.legacy, "problems": rep.problems, "warnings": rep.warnings}, ensure_ascii=False, indent=2))
    else:
        typer.echo(f"감사 로그: {rep.total}건 (서명 사슬 {rep.chained}건, 서명 도입 이전 {rep.legacy}건)")
        for w in rep.warnings:
            typer.echo(f"  [주의] {w}")
        for p in rep.problems:
            typer.echo(f"  [문제] {p}")
        typer.echo("결과: " + ("이상 없음" if rep.ok else "변조 또는 손상이 의심됩니다"))
        typer.echo("  ※ 이 검사는 '조용한 변조를 알아채는' 것이지 막는 것이 아닙니다. 로그 전체를 지우면 검증할 것이 없고, 서명 키를 읽을 수 있는 코드는 위조할 수 있습니다(Docker 샌드박스 권장).")
    if not rep.ok:
        raise typer.Exit(1)


def _fmt_usd(x: float | None) -> str:
    return "알 수 없음(단가 없음)" if x is None else f"${x:.4f}"


@app.command()
def review(
    path: Path = PathOpt,
    task: str = typer.Option(None, "--task", "-t", help="이 변경이 무엇인지 설명(위험도 분석에 쓰임). 생략하면 git 범위의 커밋 제목을 씀"),
    git_range: str = typer.Option(None, "--git-range", help="리뷰할 커밋 범위. 예: HEAD~1..HEAD, main..feature"),
    diff_file: Path = typer.Option(None, "--diff-file", help="리뷰할 unified diff 파일 (git 저장소가 아니어도 됨)"),
    level: str = typer.Option(None, "--level", help="위험 수준을 low|medium|high|critical 로 '높인다'(자동 평가보다 낮추는 것은 거부)"),
    mock: bool = typer.Option(False, "--mock", help="가짜 Provider로 흐름만 시연"),
    yes: bool = typer.Option(False, "--yes", "-y", help="확인 질문 생략"),
    as_json: bool = typer.Option(False, "--json", help="JSON으로 출력(--yes 필요)"),
) -> None:
    """변경(작업 트리 / 커밋 범위 / diff 파일)을 정책이 정한 독립 리뷰어에게 보여 READY·NOT_READY 를 판정한다.

    아무것도 적용하거나 커밋하지 않는다. NOT_READY 면 종료 코드 1 (CI 게이트로 쓸 수 있음).
    비용은 실행 전에 예상해 보여주고, 끝나면 실제 비용과 비교한다. 자격증명이 있거나 너무 큰 diff, 비용 상한을 넘을 것 같은 리뷰는 호출 전에 거부한다."""
    from devorch.git.manager import GitError
    from devorch.service import ControlPlaneError

    def fail(msg: str) -> "typer.Exit":
        typer.echo(f"오류: {msg}", err=True)
        return typer.Exit(2)

    if git_range and diff_file:
        raise fail("--git-range 와 --diff-file 은 함께 쓸 수 없습니다(둘 중 하나, 둘 다 없으면 작업 트리).")
    if as_json and not (yes or mock):
        raise fail("--json 은 비대화형 출력이라 --yes 가 필요합니다.")

    plane = ControlPlane.from_project(path, mock=mock)
    diff: str | None = None
    try:
        if git_range:
            plane.executor.ensure_ready()
            diff = plane.executor.git.range_diff(git_range)
            if not diff.strip():
                raise fail(f"{git_range} 범위에 변경이 없습니다.")
            if task is None:
                task = "; ".join(plane.executor.git.commit_subjects(git_range)) or None
        elif diff_file:
            diff = diff_file.read_text(encoding="utf-8")
    except (GitError, ExecutionError) as e:
        raise fail(str(e))
    except (OSError, UnicodeDecodeError) as e:
        raise fail(f"diff 파일을 읽지 못했습니다: {e}")
    task = task or "코드 변경 리뷰"

    try:
        tid = plane.analyze_task(task, level=level)["task_id"]
        prev = plane.preview_review(tid, diff)
    except ControlPlaneError as e:
        raise fail(str(e))

    if prev["missing_providers"]:
        detail = "; ".join(f"{m}: {plane.provider_notes.get(m, '등록되지 않음')}" for m in prev["missing_providers"])
        raise fail(f"리뷰에 필요한 provider를 사용할 수 없습니다 — {detail}  (`devorch providers`, `devorch doctor` 로 확인)")

    if not as_json:
        typer.echo(f"작업: {task[:80]}")
        typer.echo(f"위험도: {str(prev['risk']).upper()}   대상: {len(prev['files'])}개 파일, diff {prev['diff_bytes']:,} bytes")
        if not prev["reviewers"]:
            typer.echo("이 위험 수준에는 배정된 리뷰어가 없어 리뷰를 하지 않습니다. 리뷰를 받으려면 --level medium|high|critical 로 위험 수준을 높이세요.")
            raise typer.Exit(0)
        typer.echo("리뷰어: " + ", ".join(prev["reviewers"]))
        rng = f", 범위 {_fmt_usd(prev['estimated_low_usd'])} ~ {_fmt_usd(prev['estimated_high_usd'])}" if prev["estimated_usd"] is not None else ""
        typer.echo(
            f"예상 비용: {_fmt_usd(prev['estimated_usd'])}{rng} (입력 ~{prev['input_tokens']:,} 토큰은 ±10% 안팎으로 검증됨, "
            f"출력 ~{prev['output_tokens']:,} 토큰은 편차가 커서 범위로 봐야 함 — 약 1만 2천 토큰보다 큰 diff는 검증되지 않았음)"
        )
        if not (yes or mock) and not typer.confirm("실제 LLM API를 호출합니다. 진행할까요?"):
            raise typer.Exit(1)
    elif not prev["reviewers"]:
        typer.echo(json.dumps({"status": "no_reviewers_required", "risk": prev["risk"], "reviewers": []}, ensure_ascii=False))
        raise typer.Exit(0)

    try:
        res = asyncio.run(plane.request_review(tid, diff=diff))
    except ControlPlaneError as e:
        raise fail(str(e))

    if as_json:
        typer.echo(json.dumps({**res, "preview": prev}, ensure_ascii=False, indent=2, default=str))
    else:
        typer.echo(f"\n결과: {res['status']}")
        if res.get("caveat"):
            typer.echo(f"  ※ {res['caveat']}")
        for f in res["blocking"]:
            typer.echo(f"  [BLOCKING {f['severity'].upper()}] ({f['source']}) {f['title']}" + (f"  — {f['file']}" if f.get("file") else ""))
        for f in res["warnings"]:
            typer.echo(f"  [warning {f['severity']}] ({f['source']}) {f['title']}")
        if res["missing_reviews"]:
            typer.echo(f"  미완료 리뷰: {', '.join(res['missing_reviews'])} (호출·파싱 실패 — 통과로 보지 않음)")
        for n in res["notes"]:
            typer.echo(f"  note: {n}")
        actual, est = res["cost_usd"], res["estimated_review_cost_usd"]
        line = f"실제 비용: {_fmt_usd(actual)}"
        if actual and est:
            lo, hi = res["estimated_review_cost_range_usd"]
            line += f" (예상 {_fmt_usd(est)} 대비 {(est - actual) / actual * 100:+.0f}%, 범위 {'안' if lo <= actual <= hi else '밖'})"
        failed = {p: u["failed_calls"] for p, u in res["usage"].items() if u.get("failed_calls")}
        if failed:
            line += "   ※ 실패한 호출도 청구됨: " + ", ".join(f"{p} {n}회" for p, n in failed.items())
        typer.echo(line + "   토큰: " + (", ".join(f"{p} {u['input_tokens']:,}/{u['output_tokens']:,}" for p, u in res["usage"].items()) or "-"))
    if res["status"] == "NOT_READY":
        raise typer.Exit(1)


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
    if (warning := plane._sandbox_warning(plan)) is not None:  # the human approving should know where the tests ran
        typer.echo(f"  ※ {warning}")
    if show_diff:
        typer.echo("\n" + record.diff)

    approved = typer.confirm("이 diff를 승인할까요? (n = 거부로 기록)")
    plane.store.save_approval(Approval(task_id=task_id, diff_sha=record.diff_sha or "", approved=approved, via="cli"))
    plane.audit.log(task_id, "approval", {"approved": approved, "diff_sha": record.diff_sha, "via": "cli"})
    typer.echo("승인됨. 에이전트가 devorch_apply_changes 를 호출하면 브랜치에 커밋됩니다." if approved else "거부로 기록했습니다.")


@app.command()
def finish(
    task_id: str = typer.Argument(..., help="커밋까지 끝난 task id"),
    path: Path = PathOpt,
    yes: bool = typer.Option(False, "--yes", "-y", help="확인 질문 없이 정리"),
) -> None:
    """커밋이 끝난 뒤 작업 트리에 남은 에이전트의 미커밋 수정을 정리해 `git merge` 가 막히지 않게 한다.

    커밋된 내용과 정확히 같은 파일만 되돌린다. 그 뒤에 직접 더 고친 파일이 하나라도 있으면 아무것도 건드리지 않는다."""
    if not yes and not _is_interactive():
        typer.echo("대화형 터미널이 아니라 확인을 받을 수 없습니다. 직접 터미널에서 실행하거나 --yes 를 붙이세요.", err=True)
        raise typer.Exit(2)
    plane = ControlPlane.from_project(path, mock=True)
    try:
        plan = plane.finish_preview(task_id)
    except (StoreError, GitError, ControlPlaneError) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(2)
    if plan["status"] == "blocked":
        typer.echo(plan["reason"], err=True)
        raise typer.Exit(1)
    if plan["status"] == "nothing_to_clean":
        typer.echo(f"정리할 것이 없습니다. 바로 merge 하세요: {plan['merge_command']}")
        return
    typer.echo(f"브랜치 {plan['branch']} 에 이미 커밋된 내용과 같아서 작업 트리에서 되돌릴 파일:")
    for rel in plan["will_restore"]:
        typer.echo(f"  - {rel}")
    if not yes and not typer.confirm("되돌릴까요?"):
        typer.echo("아무것도 바꾸지 않았습니다.")
        raise typer.Exit(1)
    result = plane.finish(task_id)
    if result["status"] != "cleaned":  # changed between the preview and now
        typer.echo(result.get("reason", "정리하지 못했습니다."), err=True)
        raise typer.Exit(1)
    typer.echo(f"정리했습니다. 이제 merge 할 수 있습니다: {result['merge_command']}")


if __name__ == "__main__":
    app()
