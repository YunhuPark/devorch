"""Defect-level statistics (repeats are not independent samples) and the solo:<provider>[:<role>] benchmark modes."""

import json

import pytest
from typer.testing import CliRunner

from devorch.bench.cases import load_cases
from devorch.bench.metrics import paired_comparison, score_run, summarize
from devorch.bench.report import render_report
from devorch.bench.runner import MODES, expected_calls, is_valid_mode, review_steps
from devorch.cli.main import app
from devorch.config import Policy
from devorch.models import Finding, Severity

CASES = load_cases()
C01 = next(c for c in CASES if c.id == "c01_sql_injection")
C08 = next(c for c in CASES if c.id == "c08_layering")
POLICY = Policy()


def f(title, sev="high"):
    return Finding(source="x", role="review", severity=Severity(sev), category="security", title=title, detail="")


def run(case, mode, rep, hit):
    findings = [f("SQL Injection in search" if case is C01 else "계층 위반: 뷰가 DB를 직접 조회(layering)")] if hit else []
    return score_run(case, mode, rep, findings, True)


# ---- repeats are one sample, not three -------------------------------------------------------------------
def test_defect_level_summary_counts_a_defect_once_however_many_repeats():
    res = [run(C01, "single", i, hit) for i, hit in enumerate([True, True, False])]
    s = summarize([C01, next(c for c in CASES if c.clean)], res, "single")
    assert (s.detected, s.defect_runs) == (2, 3)  # run level, unchanged
    assert (s.defects_detected, s.defects_total, s.defects_unstable) == (1, 1, 1)  # 2 of 3 repeats -> detected, flagged unstable


def test_a_defect_found_in_only_a_minority_or_half_of_repeats_is_not_counted_as_detected():
    for hits, expected in (([True, False, False], 0), ([True, False], 0), ([True, True, False, False], 0), ([True, True, True, False], 1)):
        res = [run(C01, "m", i, h) for i, h in enumerate(hits)]
        assert summarize([C01, next(c for c in CASES if c.clean)], res, "m").defects_detected == expected


def test_sign_test_unit_is_the_defect_not_the_repeat():
    # multi finds c08's defect in all 3 repeats, single never: that is ONE discordant defect, not three.
    res = [run(C08, "single", i, False) for i in range(3)] + [run(C08, "multi", i, True) for i in range(3)]
    p = paired_comparison([C08, next(c for c in CASES if c.clean)], res, "single", "multi")
    assert (p["only_a"], p["only_b"], p["both"], p["neither"]) == (0, 1, 0, 0)
    assert p["p_value"] == 1.0  # with one discordant defect nothing can be significant (it was 0.25 when counted 3x)


def test_defect_level_interval_is_wider_than_the_run_level_one_when_repeats_are_correlated():
    res = [run(C01, "single", i, True) for i in range(3)]
    s = summarize([C01, next(c for c in CASES if c.clean)], res, "single")
    run_lo, run_hi = s.detection_ci
    d_lo, d_hi = s.defect_ci
    assert (d_hi - d_lo) > (run_hi - run_lo)  # 1/1 defect says much less than 3/3 runs pretended to


def test_report_explains_the_two_units_and_compares_each_mode_to_the_first():
    res = [run(C08, m, i, m != "single") for m in ("single", "multi", "solo:gemini:review") for i in range(3)]
    res += [score_run(next(c for c in CASES if c.id == "c10_clean_tests_docs"), m, i, [], True) for m in ("single", "multi", "solo:gemini:review") for i in range(3)]
    text = render_report(CASES, res, ["single", "multi", "solo:gemini:review"], mock=False, repeats=3)
    assert "탐지(실행)" in text and "탐지(결함 단위)" in text
    assert "독립이 아닙니다" in text
    assert text.count("만 탐지") >= 4  # two pairs (multi vs single, solo vs single), each lists both sides
    assert "결함 단위" in text and "single 대 multi" not in text  # generic wording, not hard-coded to two modes


# ---- solo:<provider>[:<role>] modes ----------------------------------------------------------------------
def test_solo_modes_build_one_reviewer_with_the_given_provider_and_role():
    assert [(s.role, s.provider) for s in review_steps(POLICY, "solo:gemini")] == [("review", "gemini")]
    assert [(s.role, s.provider) for s in review_steps(POLICY, "solo:openai:architecture")] == [("architecture", "openai")]
    assert [(s.role, s.provider) for s in review_steps(POLICY, "solo:claude:security")] == [("security", "claude")]
    assert expected_calls(CASES, POLICY, ["solo:gemini", "solo:openai:security"], 3) == 2 * 11 * 3


@pytest.mark.parametrize("bad", ["solo:", "solo:grok", "solo:gemini:chef", "solo:gemini:review:extra", "solo", "triple"])
def test_invalid_modes_are_rejected(bad):
    assert not is_valid_mode(bad)
    with pytest.raises(ValueError):
        review_steps(POLICY, bad)


def test_builtin_modes_and_solo_are_valid():
    assert all(is_valid_mode(m) for m in (*MODES, "solo:openai", "solo:gemini:architecture"))


def test_cli_accepts_a_comma_separated_mode_list_and_rejects_bad_ones(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["init", "-p", str(tmp_path)]).exit_code == 0
    ok = runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--mock", "--mode", "single,solo:gemini:review", "--cases", "c01_sql_injection,c10_clean_tests_docs"])
    assert ok.exit_code == 0, ok.output
    out = tmp_path / ".devorch" / "bench"
    data = json.loads(next(out.iterdir()).joinpath("results.json").read_text(encoding="utf-8"))
    assert data["modes"] == ["single", "solo:gemini:review"] and {r["mode"] for r in data["results"]} == set(data["modes"])
    bad = runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--mock", "--mode", "single,solo:grok"])
    assert bad.exit_code == 2
    assert runner.invoke(app, ["bench", "run", "-p", str(tmp_path), "--mock", "--mode", "both"]).exit_code == 0
