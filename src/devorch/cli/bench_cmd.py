"""`devorch bench ...` — seeded-defect benchmark commands."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from pathlib import Path

import typer

from devorch.cli.app import DevorchTyper

from devorch.bench.cases import load_cases, validate_cases
from devorch.bench.judge import count_judge_calls, judge_results, outcome_to_json, render_judged_report
from devorch.bench.mock import heuristic_provider
from devorch.bench.metrics import summarize
from devorch.bench.report import render_report
from devorch.bench.runner import MODES, expected_calls, is_valid_mode, review_steps, run_benchmark
from devorch.config import devorch_dir, load_config, load_policy
from devorch.core.orchestrator.review import budget_preflight
from devorch.providers.base import ProviderRegistry
from devorch.providers.factory import build_registry

bench_app = DevorchTyper(help="시드 결함 벤치마크 (리뷰어가 심어둔 결함을 찾는지 측정)", no_args_is_help=True)


def _finite_positive(value: float | None) -> float | None:
    """A spend cap must be a finite number > 0: NaN compares False with everything, so `spent >= nan` would never stop anything."""
    if value is not None and not (math.isfinite(value) and value > 0):
        raise typer.BadParameter("0보다 큰 유한한 숫자여야 합니다 (nan, inf, 0, 음수는 비용 상한이 될 수 없음)")
    return value


def _load(cases_dir: Path | None, only: str | None):
    cases = load_cases(cases_dir)
    problems = validate_cases(cases)
    if problems:
        for p in problems:
            typer.echo(f"케이스 오류: {p}", err=True)
        raise typer.Exit(2)
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        unknown = wanted - {c.id for c in cases}
        if unknown:
            typer.echo(f"알 수 없는 케이스: {', '.join(sorted(unknown))}", err=True)
            raise typer.Exit(2)
        cases = [c for c in cases if c.id in wanted]
    return cases


@bench_app.command("cases")
def cases_cmd(cases_dir: Path = typer.Option(None, "--cases-dir", help="케이스 YAML 디렉터리 (기본: 내장)")) -> None:
    """케이스 목록과 심어둔 결함을 보여준다."""
    cases = _load(cases_dir, None)
    for c in cases:
        typer.echo(f"{c.id}  {'[대조군]' if c.clean else ''}  {c.title}")
        for d in c.defects:
            typer.echo(f"    - {d.id} ({d.category}/{d.severity}): {d.description}")


@bench_app.command("run")
def run_cmd(
    path: Path = typer.Option(Path("."), "--path", "-p", file_okay=False, help="프로젝트 루트(.devorch 설정 위치)"),
    mode: str = typer.Option("both", "--mode", help="single | multi | both | solo:<provider>[:<role>] — 쉼표로 여러 개(예: single,solo:gemini:review)"),
    repeats: int = typer.Option(1, "--repeats", min=1, help="반복 횟수"),
    mock: bool = typer.Option(False, "--mock", help="가짜 리뷰어(하네스 점검 전용 — 실제 성능 아님)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="호출 확인 생략"),
    only: str = typer.Option(None, "--cases", help="쉼표로 구분한 case id"),
    cases_dir: Path = typer.Option(None, "--cases-dir"),
    out: Path = typer.Option(None, "--out", help="결과 디렉터리 (기본: .devorch/bench/<시각>)"),
    claude_only: bool = typer.Option(False, "--claude-only", help="모든 리뷰어 역할을 claude 로 배정 (키 1개만 필요. 다른 회사 모델의 교차 검증은 빠짐)"),
    max_total_usd: float = typer.Option(None, "--max-total-usd", callback=_finite_positive, help="벤치마크 전체 비용 합계 상한(USD). 도달하면 새 케이스를 시작하지 않음. 모든 provider 단가 필요"),
) -> None:
    """벤치마크를 실행해 results.json 과 report.md 를 쓴다."""
    modes = list(MODES) if mode == "both" else [m.strip() for m in mode.split(",") if m.strip()]
    if not modes or any(not is_valid_mode(m) for m in modes) or len(set(modes)) != len(modes):
        typer.echo("--mode 는 single | multi | both | solo:<claude|openai|gemini>[:<review|security|architecture>] 이거나, 쉼표로 구분한 중복 없는 목록이어야 합니다.", err=True)
        raise typer.Exit(2)
    cases = _load(cases_dir, only)
    try:
        policy = load_policy(path)
    except ValueError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(2)

    if claude_only:
        policy = policy.model_copy(deep=True)
        policy.default_roles = {r: "claude" for r in policy.default_roles}
        policy.rules = {lvl: {r: "claude" for r in roles} for lvl, roles in policy.rules.items()}
        typer.echo("--claude-only: 모든 리뷰어 역할을 claude 로 배정합니다 (서로 다른 회사 모델의 교차 검증이 아님).")

    if mock:
        registry = ProviderRegistry()
        for name in ("claude", "openai", "gemini"):
            registry.register(heuristic_provider(name))
        typer.echo("MOCK 모드: 가짜 리뷰어를 사용합니다. 결과는 실제 LLM 성능이 아닙니다.")
    else:
        registry, skipped = build_registry(load_config(path))
        needed = sorted({s.provider for m in modes for s in review_steps(policy, m)})
        missing = [n for n in needed if n not in registry.names()]
        if missing:
            typer.echo("필요한 provider를 사용할 수 없습니다:", err=True)
            for n in missing:
                typer.echo(f"  - {n}: {skipped.get(n, '알 수 없음')}", err=True)
            raise typer.Exit(2)
        for cap in (policy.limits.max_cost_usd, max_total_usd):
            if (problem := budget_preflight(registry, needed, cap)) is not None:
                typer.echo(f"오류: {problem}", err=True)
                raise typer.Exit(2)
        calls = expected_calls(cases, policy, modes, repeats)
        typer.echo(f"실제 LLM API를 총 {calls}회 호출합니다 (provider: {', '.join(needed)}). 비용은 호출 후에야 알 수 있고, 단가를 설정하지 않으면 unknown 입니다.")
        if policy.limits.max_cost_usd is not None:
            typer.echo(f"케이스(실행)당 비용 상한: ${policy.limits.max_cost_usd} — 한 케이스의 리뷰어는 동시에 출발하므로 전체 지출을 막지는 못합니다.")
        if max_total_usd is not None:
            typer.echo(f"벤치마크 전체 비용 합계 상한: ${max_total_usd} — 도달하면 새 케이스를 시작하지 않습니다(진행 중인 케이스는 끝나므로 조금 넘을 수 있음).")
        else:
            typer.echo("전체 비용 합계 상한이 없습니다. --max-total-usd 로 정할 수 있습니다.")
        if not yes and not typer.confirm("진행할까요?"):
            raise typer.Exit(1)

    results = asyncio.run(run_benchmark(cases, policy, registry, modes, repeats, max_total_cost_usd=max_total_usd))

    out_dir = out or (devorch_dir(path) / "bench" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report = render_report(cases, results, modes, mock=mock, repeats=repeats)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    payload = {
        "mock": mock, "modes": modes, "repeats": repeats,
        "summaries": {m: _summary_dict(summarize(cases, results, m)) for m in modes},
        "results": [r.to_dict() for r in results],
    }
    (out_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(report)
    typer.echo(f"\n저장: {out_dir}")


@bench_app.command("judge")
def judge_cmd(
    results_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="`bench run` 이 저장한 결과 디렉터리 (results.json 이 있는 곳)"),
    path: Path = typer.Option(Path("."), "--path", "-p", file_okay=False, help="프로젝트 루트(.devorch 설정 위치)"),
    judge: str = typer.Option("claude", "--judge", help="판정에 쓸 provider: claude | openai | gemini (리뷰어와 다른 회사를 권장)"),
    cases_dir: Path = typer.Option(None, "--cases-dir"),
    yes: bool = typer.Option(False, "--yes", "-y", help="호출 확인 생략"),
    max_total_usd: float = typer.Option(None, "--max-total-usd", callback=_finite_positive, help="판정 비용 합계 상한(USD). 판정자 단가 필요. 병렬 호출 때문에 진행 중이던 호출분만큼 넘을 수 있음"),
) -> None:
    """저장된 벤치마크 결과를 LLM 판정기로 다시 판정한다 (리뷰어는 다시 호출하지 않음). 정규식 판정과 나란히 보고한다."""
    src = results_dir / "results.json"
    if not src.exists():
        typer.echo(f"오류: {src} 가 없습니다.", err=True)
        raise typer.Exit(2)
    payload = json.loads(src.read_text(encoding="utf-8"))
    if payload.get("mock"):
        typer.echo("오류: --mock 으로 만든 결과는 판정하지 않습니다(가짜 리뷰어의 결과).", err=True)
        raise typer.Exit(2)
    if judge not in ("claude", "openai", "gemini"):
        typer.echo("--judge 는 claude | openai | gemini 중 하나여야 합니다.", err=True)
        raise typer.Exit(2)
    cases = _load(cases_dir, None)
    unknown = sorted({r["case_id"] for r in payload["results"]} - {c.id for c in cases})
    if unknown:
        typer.echo(f"오류: 결과에 이 케이스 집합에 없는 케이스가 있습니다: {', '.join(unknown)} (--cases-dir 확인)", err=True)
        raise typer.Exit(2)

    registry, skipped = build_registry(load_config(path))
    if judge not in registry.names():
        typer.echo(f"판정기 provider를 사용할 수 없습니다: {judge}: {skipped.get(judge, '알 수 없음')}", err=True)
        raise typer.Exit(2)
    if (problem := budget_preflight(registry, [judge], max_total_usd)) is not None:
        typer.echo(f"오류: {problem}", err=True)
        raise typer.Exit(2)
    calls = count_judge_calls(cases, payload)
    reviewers = sorted({p for r in payload["results"] for p in (r.get("models") or {})})
    typer.echo(f"판정 LLM API를 총 {calls}회 호출합니다 (판정기: {judge}). 리뷰어는 다시 호출하지 않습니다. 비용은 호출 후에야 알 수 있습니다.")
    if judge in reviewers:
        typer.echo(f"경고: 판정기({judge})가 리뷰어와 같은 회사 모델입니다(자기 선호 편향 가능).")
    if max_total_usd is not None:
        typer.echo(f"판정 비용 합계 상한: ${max_total_usd} — 도달하면 나머지는 미판정으로 남깁니다.")
    if not yes and not typer.confirm("진행할까요?"):
        raise typer.Exit(1)

    outcome = asyncio.run(judge_results(cases, payload, registry, judge, max_total_cost_usd=max_total_usd))
    report = render_judged_report(cases, payload, outcome, judge)
    (results_dir / "judged.md").write_text(report, encoding="utf-8")
    (results_dir / "judged.json").write_text(json.dumps(outcome_to_json(outcome), ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(report)
    typer.echo(f"\n저장: {results_dir / 'judged.md'}, {results_dir / 'judged.json'}")


def _summary_dict(s) -> dict:
    return {
        "defect_runs": s.defect_runs, "detected": s.detected, "detection_rate": s.detection_rate,
        "detection_ci": s.detection_ci, "clean_runs_completed": s.clean_runs_completed,
        "false_positives": s.false_positives, "fp_rate": s.fp_rate, "fp_ci": s.fp_ci,
        "trap_runs_completed": s.trap_runs_completed, "trap_alarms": s.trap_alarms, "trap_rate": s.trap_rate, "trap_ci": s.trap_ci,
        "defects_total": s.defects_total, "defects_detected": s.defects_detected, "defects_unstable": s.defects_unstable, "defect_ci": s.defect_ci,
        "incomplete": s.incomplete, "unmatched": s.unmatched, "cost_usd": s.cost_usd,
        "cost_unknown": s.cost_unknown, "mean_latency_s": s.mean_latency_s,
        "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
    }
