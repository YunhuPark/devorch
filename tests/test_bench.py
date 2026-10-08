import json
import subprocess

import pytest
from typer.testing import CliRunner

from devorch.bench.cases import Case, Defect, FileChange, build_diff, load_cases, validate_cases
from devorch.bench.metrics import judge, paired_comparison, score_run, sign_test, summarize, wilson
from devorch.bench.mock import heuristic_provider
from devorch.bench.runner import expected_calls, review_steps, run_benchmark
from devorch.cli.main import app
from devorch.config import Policy
from devorch.models import Finding, Severity
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider

CASES = load_cases()
POLICY = Policy()


def registry(factory):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(factory(n))
    return reg


def f(sev="high", title="t", detail="", cat="security"):
    return Finding(source="x", role="review", severity=Severity(sev), category=cat, title=title, detail=detail)


def test_builtin_cases_are_valid():
    assert validate_cases(CASES) == []
    assert len(CASES) == 11
    assert sum(len(c.defects) for c in CASES) == 9
    assert sum(c.clean for c in CASES) == 3


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_diff_applies_cleanly_to_before_state(case, tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for path, ch in case.files.items():
        if ch.before is not None:
            p = tmp_path / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(ch.before)
    (tmp_path / "case.diff").write_text(case.diff)
    r = subprocess.run(["git", "apply", "--check", "case.diff"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_validate_catches_problems():
    ch = {"a.py": FileChange(before="x\n", after="y\n")}
    bad = Case(id="a", title="", task="", files=ch, defects=[Defect(id="d", category="c", description="", match_any=["("], file="zz.py")])
    probs = validate_cases([bad, bad])
    text = " ".join(probs)
    assert "중복된 case id" in text and "잘못된 정규식" in text and "케이스에 없음" in text and "대조군" in text
    with pytest.raises(ValueError):
        build_diff({"a.py": FileChange(before="x\n", after="x\n")})


def test_judge_matches_and_counts_unmatched():
    case = next(c for c in CASES if c.id == "c01_sql_injection")
    detected, unmatched = judge(case, [f(title="SQL Injection in search"), f("low", title="naming nit"), f("info", title="fyi")])
    assert detected == {"sqli"} and unmatched == 1  # info is not counted


def test_wilson_and_sign_test_values():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)
    assert sign_test(0, 0) == 1.0
    assert sign_test(0, 5) == pytest.approx(0.0625)
    assert sign_test(3, 3) == 1.0
    assert sign_test(1, 9) == pytest.approx(0.021484375)


def test_review_steps_modes():
    assert [s.role for s in review_steps(POLICY, "single")] == ["review"]
    assert {s.role for s in review_steps(POLICY, "multi")} == {"security", "architecture", "review"}
    with pytest.raises(ValueError):
        review_steps(POLICY, "x")
    assert expected_calls(CASES, POLICY, ["single", "multi"], 2) == (1 + 3) * 11 * 2


async def test_oracle_detects_everything_null_detects_nothing():
    def oracle(req):
        # reviewer that "knows" the answers: reports every defect's first regex-friendly phrase
        out = []
        for c in CASES:
            if c.defects and c.diff in req.prompt:  # the exact diff identifies the case
                out += [{"severity": "high", "category": d.category, "title": d.description, "detail": d.description} for d in c.defects]
        return json.dumps(out)

    res = await run_benchmark(CASES, POLICY, registry(lambda n: MockProvider(n, oracle)), ["single"])
    s = summarize(CASES, res, "single")
    assert s.detected == s.defect_runs == 9
    assert s.false_positives == 0 and s.incomplete == 0

    res = await run_benchmark(CASES, POLICY, registry(lambda n: MockProvider(n, ["[]"])), ["single", "multi"])
    for m in ("single", "multi"):
        s = summarize(CASES, res, m)
        assert s.detected == 0 and s.false_positives == 0 and s.unmatched == 0
    p = paired_comparison(CASES, res, "single", "multi")
    assert p["only_a"] == p["only_b"] == 0 and p["neither"] == 9 and p["p_value"] == 1.0


async def test_failing_or_garbage_reviewer_is_incomplete_not_pass():
    class Boom(MockProvider):
        async def generate(self, request):
            raise RuntimeError("api down")

    res = await run_benchmark(CASES, POLICY, registry(lambda n: Boom(n)), ["single"])
    assert all(not r.completed for r in res)
    s = summarize(CASES, res, "single")
    assert s.incomplete == 11 and s.detected == 0
    assert s.clean_runs_completed == 0 and s.fp_rate == 0.0  # incomplete clean runs are not "clean passes"

    res = await run_benchmark(CASES, POLICY, registry(lambda n: MockProvider(n, ["not json at all"])), ["single"])
    assert all(not r.completed for r in res)


async def test_false_positive_counted_on_clean_cases_only():
    always = json.dumps([{"severity": "high", "category": "security", "title": "scary", "detail": "x"}])
    res = await run_benchmark(CASES, POLICY, registry(lambda n: MockProvider(n, [always])), ["single"])
    s = summarize(CASES, res, "single")
    assert s.false_positives == 3 == s.clean_runs_completed and s.fp_rate == 1.0
    low = json.dumps([{"severity": "low", "category": "style", "title": "nit", "detail": ""}])
    res = await run_benchmark(CASES, POLICY, registry(lambda n: MockProvider(n, [low])), ["single"])
    assert summarize(CASES, res, "single").false_positives == 0  # below the FP severity threshold


async def test_partial_findings_of_incomplete_run_are_ignored():
    case = next(c for c in CASES if c.id == "c01_sql_injection")
    r = score_run(case, "multi", 0, [f(title="SQL injection")], completed=False)
    assert r.detected == set()


async def test_heuristic_mock_is_deterministic_and_repeats_multiply():
    reg = registry(heuristic_provider)
    a = await run_benchmark(CASES, POLICY, reg, ["multi"], repeats=2)
    b = await run_benchmark(CASES, POLICY, reg, ["multi"], repeats=2)
    assert len(a) == 22
    key = lambda rs: sorted((r.case_id, r.repeat, tuple(sorted(r.detected))) for r in rs)
    assert key(a) == key(b)
    assert summarize(CASES, a, "multi").defect_runs == 18


def test_cli_mock_run_writes_files_with_banner(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["init", "-p", str(tmp_path)]).exit_code == 0
    r = runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--mock", "--cases", "c01_sql_injection,c09_clean_refactor"])
    assert r.exit_code == 0, r.output
    dirs = list((tmp_path / ".devorch" / "bench").iterdir())
    assert len(dirs) == 1
    report = (dirs[0] / "report.md").read_text()
    assert "MOCK — harness check only" in report and "## 한계" in report
    data = json.loads((dirs[0] / "results.json").read_text())
    assert data["mock"] is True and len(data["results"]) == 4


def test_cli_real_run_without_providers_fails_before_any_call(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    runner = CliRunner()
    runner.invoke(app, ["init", "-p", str(tmp_path)])
    r = runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--yes"])
    assert r.exit_code == 2 and "사용할 수 없습니다" in r.output
    assert not (tmp_path / ".devorch" / "bench").exists()


def test_cli_unknown_case_and_mode(tmp_path):
    runner = CliRunner()
    runner.invoke(app, ["init", "-p", str(tmp_path)])
    assert runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--mock", "--cases", "nope"]).exit_code == 2
    assert runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--mock", "--mode", "x"]).exit_code == 2
    assert runner.invoke(app, ["bench", "cases"]).exit_code == 0


def test_cli_claude_only_needs_just_the_claude_key(tmp_path, monkeypatch):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    runner = CliRunner()
    runner.invoke(app, ["init", "-p", str(tmp_path)])
    # without the flag: openai/gemini are required → refused before any call
    assert runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--yes"]).exit_code == 2
    # with the flag: only claude is needed, so the provider check passes; decline at the confirm prompt
    r = runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--claude-only"], input="n\n")
    assert r.exit_code == 1 and "provider: claude)" in r.output and "사용할 수 없습니다" not in r.output
