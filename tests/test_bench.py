import json

import pytest
from gitutil import run_git
from typer.testing import CliRunner

from devorch.bench.cases import Case, Defect, FileChange, Trap, build_diff, load_cases, validate_cases
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
    assert len(CASES) == 16
    assert sum(len(c.defects) for c in CASES) == 13
    assert sum(c.clean for c in CASES) == 4


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_diff_applies_cleanly_to_before_state(case, tmp_path):
    run_git(tmp_path, "init", "-q")
    for path, ch in case.files.items():
        if ch.before is not None:
            p = tmp_path / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(ch.before.encode("utf-8"))  # bytes: keep LF (write_text would turn it into CRLF on Windows)
    (tmp_path / "case.diff").write_bytes(case.diff.encode("utf-8"))
    r = run_git(tmp_path, "apply", "--check", "case.diff", check=False)
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
    assert expected_calls(CASES, POLICY, ["single", "multi"], 2) == (1 + 3) * 16 * 2


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
    assert s.detected == s.defect_runs == 13
    assert s.false_positives == 0 and s.incomplete == 0

    res = await run_benchmark(CASES, POLICY, registry(lambda n: MockProvider(n, ["[]"])), ["single", "multi"])
    for m in ("single", "multi"):
        s = summarize(CASES, res, m)
        assert s.detected == 0 and s.false_positives == 0 and s.unmatched == 0
    p = paired_comparison(CASES, res, "single", "multi")
    assert p["only_a"] == p["only_b"] == 0 and p["neither"] == 13 and p["p_value"] == 1.0


async def test_failing_or_garbage_reviewer_is_incomplete_not_pass():
    class Boom(MockProvider):
        async def generate(self, request):
            raise RuntimeError("api down")

    res = await run_benchmark(CASES, POLICY, registry(lambda n: Boom(n)), ["single"])
    assert all(not r.completed for r in res)
    s = summarize(CASES, res, "single")
    assert s.incomplete == 16 and s.detected == 0
    assert s.clean_runs_completed == 0 and s.fp_rate == 0.0  # incomplete clean runs are not "clean passes"

    res = await run_benchmark(CASES, POLICY, registry(lambda n: MockProvider(n, ["not json at all"])), ["single"])
    assert all(not r.completed for r in res)


async def test_false_positive_counted_on_clean_cases_only():
    always = json.dumps([{"severity": "high", "category": "security", "title": "scary", "detail": "x"}])
    res = await run_benchmark(CASES, POLICY, registry(lambda n: MockProvider(n, [always])), ["single"])
    s = summarize(CASES, res, "single")
    assert s.false_positives == 4 == s.clean_runs_completed and s.fp_rate == 1.0
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
    assert len(a) == 32
    key = lambda rs: sorted((r.case_id, r.repeat, tuple(sorted(r.detected))) for r in rs)
    assert key(a) == key(b)
    assert summarize(CASES, a, "multi").defect_runs == 26


def test_cli_mock_run_writes_files_with_banner(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["init", "-p", str(tmp_path)]).exit_code == 0
    r = runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--mock", "--cases", "c01_sql_injection,c09_clean_refactor"])
    assert r.exit_code == 0, r.output
    dirs = list((tmp_path / ".devorch" / "bench").iterdir())
    assert len(dirs) == 1
    report = (dirs[0] / "report.md").read_text(encoding="utf-8")
    assert "MOCK — harness check only" in report and "## 한계" in report
    data = json.loads((dirs[0] / "results.json").read_text(encoding="utf-8"))
    assert all("usage" in r for r in data["results"]) and "input_tokens" in data["summaries"]["single"]
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


# ---- trap-based false alarms (decided before the full run; the strict rule is still reported) ----------
C11 = next(c for c in CASES if c.id == "c11_clean_safe_like")


def test_only_c11_declares_a_trap_and_traps_live_on_clean_cases_only():
    assert [c.id for c in CASES if c.traps] == ["c11_clean_safe_like"]
    assert all(c.clean for c in CASES if c.traps)
    bad = Case(id="x", title="", task="", files={"a.py": FileChange(before="x\n", after="y\n")},
               defects=[Defect(id="d", category="c", description="", match_any=["x"])],
               traps=[Trap(id="t", description="", match_any=["y"])])
    assert any("함정" in p and "결함" in p for p in validate_cases([bad]))
    broken = Case(id="y", title="", task="", files={"a.py": FileChange(before="x\n", after="y\n")}, traps=[Trap(id="t", description="", match_any=["("])])
    assert any("잘못된 정규식" in p for p in validate_cases([broken]))


@pytest.mark.parametrize(
    "title,detail",
    [
        ("SQL 인젝션 취약점", "name 이 쿼리에 직접 삽입됨"),
        ("SQL injection via LIKE clause", ""),
        ("f-string 으로 쿼리를 구성함", "값이 검증 없이 들어감"),
        ("문자열 연결로 쿼리를 만들고 있음", ""),
        ("query built with string interpolation", ""),
    ],
)
def test_c11_trap_catches_the_intended_false_alarm(title, detail):
    r = score_run(C11, "single", 0, [f("high", title, detail)], True)
    assert r.trap_alarm and r.false_positive


@pytest.mark.parametrize(
    "title",
    [
        "이름의 LIKE 와일드카드가 필터를 무력화할 수 있음",  # the real finding that tripped the strict rule on 2026-10-08
        "이름에 포함된 LIKE 와일드카드가 필터 조건을 바꿉니다",
        "새 기능에 대한 테스트 부재",
    ],
)
def test_legitimate_nits_are_not_trap_alarms_even_at_medium_or_high(title):
    r = score_run(C11, "single", 0, [f("medium", title), f("high", title)], True)
    assert not r.trap_alarm and r.false_positive  # still a strict false positive, never hidden


def test_trap_needs_medium_or_higher_and_a_completed_review():
    sqli = "SQL 인젝션 취약점"
    assert not score_run(C11, "single", 0, [f("low", sqli), f("info", sqli)], True).trap_alarm
    assert not score_run(C11, "single", 0, [f("critical", sqli)], False).trap_alarm  # incomplete review: nothing counted
    assert score_run(C11, "single", 0, [f("medium", sqli)], True).trap_alarm


def test_cases_without_traps_never_raise_trap_alarms_and_are_excluded_from_the_trap_rate():
    c09 = next(c for c in CASES if c.id == "c09_clean_refactor")
    r = score_run(c09, "single", 0, [f("critical", "SQL 인젝션 취약점")], True)
    assert r.false_positive and not r.trap_alarm
    s = summarize(CASES, [r, score_run(C11, "single", 0, [f("medium", "SQL 인젝션 취약점")], True)], "single")
    assert (s.trap_alarms, s.trap_runs_completed) == (1, 1) and s.clean_runs_completed == 2  # c09 is not a trap run
    empty = summarize(CASES, [r], "single")
    assert empty.trap_runs_completed == 0 and empty.trap_rate == 0.0


def test_report_shows_both_rates_and_labels_each_clean_case():
    from devorch.bench.report import render_report

    res = [
        score_run(C11, "single", 0, [f("medium", "이름의 LIKE 와일드카드가 필터를 무력화할 수 있음")], True),
        score_run(C11, "multi", 0, [f("high", "SQL 인젝션 취약점")], True),
        score_run(next(c for c in CASES if c.id == "c10_clean_tests_docs"), "single", 0, [], True),
    ]
    text = render_report(CASES, res, ["single", "multi"], mock=False, repeats=1)
    assert "오탐률(엄격)" in text and "오탐률(함정)" in text
    assert "엄격 오탐" in text and "엄격 오탐, 함정 오탐" in text and "정상" in text


def test_report_says_n_a_when_no_trap_case_completed():
    from devorch.bench.report import render_report

    res = [score_run(next(c for c in CASES if c.id == "c10_clean_tests_docs"), "single", 0, [], True)]
    text = render_report(CASES, res, ["single"], mock=False, repeats=1)
    assert "오탐률(함정)" in text and "n/a" in text
