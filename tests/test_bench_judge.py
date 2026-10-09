"""LLM judge for seeded-defect detection: runs on SAVED results (no reviewer calls), reported next to the regex matcher,
blind to which model produced a finding, fail-closed (a failed judgement is 'unjudged', never a silent miss or hit)."""

import json

import pytest
from typer.testing import CliRunner

from devorch.bench.cases import load_cases
from devorch.bench.judge import (
    build_judge_prompt,
    judge_results,
    parse_verdict,
    render_judged_report,
    summarize_judged,
)
from devorch.bench.metrics import score_run
from devorch.cli.main import app
from devorch.models import Finding, Severity
from devorch.providers.base import LLMProvider, LLMRequest, LLMResponse, ProviderRegistry

CASES = {c.id: c for c in load_cases()}
C14, C15, C01, C09 = CASES["c14_duplicated_rule"], CASES["c15_global_cache"], CASES["c01_sql_injection"], CASES["c09_clean_refactor"]


def fnd(title, detail="", sev="medium", source="openai", role="review"):
    return Finding(source=source, role=role, severity=Severity(sev), category="x", title=title, detail=detail)


def result(case, mode, rep, findings, completed=True, models=None):
    r = score_run(case, mode, rep, findings, completed, models=models or {"openai": "gpt-x"})
    return r.to_dict()


class Scripted(LLMProvider):
    """A judge that answers from a rule on the prompt; records every prompt it saw. Costs 0.01 per call when priced."""

    def __init__(self, rule, name="claude", cost=0.01, fail=False):
        self.name, self.rule, self._cost, self.fail = name, rule, cost, fail
        self.prompts: list[str] = []

    async def generate(self, request: LLMRequest):
        self.prompts.append(request.prompt)
        if self.fail:
            raise RuntimeError("judge api down")
        return LLMResponse(text=self.rule(request.prompt), provider=self.name, model="judge-1", input_tokens=500, output_tokens=40, cost_usd=self._cost)


def verdict(detected, finding=1, reason="same root problem"):
    return json.dumps({"detected": detected, "finding": finding if detected else None, "reason": reason})


def reg(p):
    r = ProviderRegistry()
    r.register(p)
    return r


# ---- verdict parsing is strict ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,detected,finding",
    [
        ('{"detected": true, "finding": 2, "reason": "x"}', True, 2),
        ('```json\n{"detected": false, "finding": null, "reason": "x"}\n```', False, None),
        ('Sure! {"detected": true, "finding": 1, "reason": "x"} hope it helps', True, 1),
        ('{"detected": true, "reason": "no finding index"}', True, None),
    ],
)
def test_parse_verdict_accepts_real_booleans(text, detected, finding):
    v = parse_verdict(text)
    assert (v.detected, v.finding) == (detected, finding)


@pytest.mark.parametrize("text", ["", "I think yes", "{}", '{"detected": "true"}', '{"detected": 1}', '{"detected": null}', "{not json}", '["detected"]'])
def test_parse_verdict_rejects_anything_that_is_not_an_explicit_boolean(text):
    assert parse_verdict(text).detected is None


# ---- the prompt: informative but blind ----------------------------------------------------------------
def test_prompt_has_the_seeded_defect_and_the_findings_but_not_who_wrote_them():
    fs = [fnd("TAX_RATE 를 무시함", "invoice_total 이 0.1을 직접 씀", source="openai", role="architecture"), fnd("다른 문제", source="gemini", role="security")]
    p = build_judge_prompt(C14, C14.defects[0], fs)
    assert C14.defects[0].description in p and "1. [" in p and "2. [" in p and "TAX_RATE 를 무시함" in p and C14.task in p
    for leak in ("openai", "gemini", "architecture", "security", "multi", "single", "solo"):
        assert leak not in p.lower(), leak
    assert C14.defects[0].match_any[0] not in p  # the regex is never shown to the judge


# ---- judging saved results ------------------------------------------------------------------------------
def payload(*results):
    return {"mock": False, "modes": sorted({r["mode"] for r in results}), "repeats": 1, "results": list(results)}


async def test_only_completed_runs_of_defect_cases_with_findings_are_judged_and_disagreements_are_kept():
    hit = fnd("invoice_total 이 TAX_RATE 와 calc_tax 를 무시한다", "세율 0.1을 직접 사용")  # real detection the regex misses
    ttl = fnd("캐시에 TTL 이 없음", "오래된 환율")
    rs = [
        result(C14, "single", 0, [hit]),  # regex: missed. judge should say detected -> disagreement
        result(C15, "single", 0, [ttl]),  # both: missed
        result(C14, "multi", 0, []),  # no findings -> no call, certainly not detected
        result(C01, "multi", 0, [fnd("x")], completed=False),  # incomplete -> never judged
        result(C09, "single", 0, [fnd("키워드 인자 깨짐")]),  # clean case -> not judged
    ]
    judge = Scripted(lambda prompt: verdict("TAX_RATE" in prompt))
    out = await judge_results(list(CASES.values()), payload(*rs), reg(judge), "claude")

    assert len(judge.prompts) == 2  # c14/single and c15/single only
    by = {(r.case_id, r.mode): r for r in out.records}
    assert (by[("c14_duplicated_rule", "single")].regex, by[("c14_duplicated_rule", "single")].judge) == (False, True)
    assert (by[("c15_global_cache", "single")].regex, by[("c15_global_cache", "single")].judge) == (False, False)
    assert by[("c14_duplicated_rule", "multi")].judge is False and "지적 없음" in by[("c14_duplicated_rule", "multi")].reason
    assert ("c01_sql_injection", "multi") not in by and ("c09_clean_refactor", "single") not in by
    assert by[("c14_duplicated_rule", "single")].finding_title.startswith("invoice_total")
    assert out.cost_usd == pytest.approx(0.02) and out.model == "judge-1"


async def test_a_failed_or_unparsable_judgement_is_unjudged_not_a_silent_miss_or_hit():
    rs = [result(C14, "single", 0, [fnd("TAX_RATE 무시")])]
    down = await judge_results(list(CASES.values()), payload(*rs), reg(Scripted(lambda p: verdict(True), fail=True)), "claude")
    assert down.records[0].judge is None and "호출 실패" in down.records[0].reason
    garbage = await judge_results(list(CASES.values()), payload(*rs), reg(Scripted(lambda p: "maybe?")), "claude")
    assert garbage.records[0].judge is None and "파싱" in garbage.records[0].reason
    s = summarize_judged(list(CASES.values()), garbage.records)["single"]
    assert (s.unjudged, s.judge_detected) == (1, 0)  # counted separately; never as a detection


async def test_total_cost_cap_stops_judging_and_marks_the_rest_unjudged():
    rs = [result(CASES[c], "single", 0, [fnd("지적")]) for c in ("c01_sql_injection", "c02_missing_authz", "c03_jwt_verify_off", "c05_double_refund", "c06_weak_hash")]
    judge = Scripted(lambda p: verdict(False), cost=0.04)
    out = await judge_results(list(CASES.values()), payload(*rs), reg(judge), "claude", concurrency=1, max_total_cost_usd=0.10)
    assert len(judge.prompts) == 3  # 0.04, 0.08, 0.12 (>= cap) then stop
    assert sum(r.judge is None for r in out.records) == 2 and any("상한" in r.reason for r in out.records if r.judge is None)


async def test_summary_counts_defects_not_repeats_and_lists_disagreements():
    hit = fnd("TAX_RATE 무시", "0.1 직접 사용")
    rs = [result(C14, "single", i, [hit]) for i in range(3)]
    out = await judge_results(list(CASES.values()), payload(*rs), reg(Scripted(lambda p: verdict(True))), "claude")
    s = summarize_judged(list(CASES.values()), out.records)["single"]
    assert (s.total_defects, s.regex_detected, s.judge_detected) == (1, 0, 1)  # 3 repeats = one defect
    assert len(s.disagreements) == 3 and s.agreements == 0


async def test_report_shows_both_judgements_the_disagreements_and_warns_about_a_same_company_judge():
    hit = fnd("TAX_RATE 무시", "0.1 직접 사용")
    rs = [result(C14, "single", 0, [hit], models={"openai": "gpt-x"})]
    out = await judge_results(list(CASES.values()), payload(*rs), reg(Scripted(lambda p: verdict(True, reason="calc_tax 를 재사용하지 않음", ), name="openai")), "openai")
    text = render_judged_report(list(CASES.values()), payload(*rs), out, "openai")
    assert "정규식" in text and "판정기" in text and "엇갈린" in text and "calc_tax 를 재사용하지 않음" in text and "TAX_RATE 무시" in text
    assert "같은 회사" in text and "자기 선호" in text
    cross = render_judged_report(list(CASES.values()), payload(*rs), out, "claude")
    assert "같은 회사" not in cross
    assert "검증되지 않았" in cross  # the judge itself is unvalidated and the report says so


# ---- CLI ----------------------------------------------------------------------------------------------------
def write_results(tmp_path, results, mock=False):
    d = tmp_path / ".devorch" / "bench" / "20261009-000000"
    d.mkdir(parents=True)
    (d / "results.json").write_text(json.dumps({**payload(*results), "mock": mock}, ensure_ascii=False), encoding="utf-8")
    return d


def test_cli_judge_writes_files_and_makes_one_call_per_judgeable_pair(tmp_path, monkeypatch):
    d = write_results(tmp_path, [result(C14, "single", 0, [fnd("TAX_RATE 무시")]), result(C15, "single", 0, [fnd("TTL")])])
    judge = Scripted(lambda p: verdict("TAX_RATE" in p))
    monkeypatch.setattr("devorch.cli.bench_cmd.build_registry", lambda cfg: (reg(judge), {}))
    r = CliRunner().invoke(app, ["bench", "judge", str(d), "-p", str(tmp_path), "--judge", "claude", "--yes"])
    assert r.exit_code == 0, r.output
    assert "총 2회" in r.output and len(judge.prompts) == 2
    saved = json.loads((d / "judged.json").read_text(encoding="utf-8"))
    assert {x["case_id"]: x["judge"] for x in saved["records"]} == {"c14_duplicated_rule": True, "c15_global_cache": False}
    assert "판정기" in (d / "judged.md").read_text(encoding="utf-8")


def test_cli_judge_refuses_mock_results_and_unavailable_or_unpriced_judges_before_any_call(tmp_path, monkeypatch):
    runner = CliRunner()
    mock_dir = write_results(tmp_path, [result(C14, "single", 0, [fnd("x")])], mock=True)
    judge = Scripted(lambda p: verdict(True))
    monkeypatch.setattr("devorch.cli.bench_cmd.build_registry", lambda cfg: (reg(judge), {}))
    assert runner.invoke(app, ["bench", "judge", str(mock_dir), "-p", str(tmp_path), "--yes"]).exit_code == 2  # mock results

    real = tmp_path / "real"
    real.mkdir()
    d = write_results(real, [result(C14, "single", 0, [fnd("x")])])
    monkeypatch.setattr("devorch.cli.bench_cmd.build_registry", lambda cfg: (ProviderRegistry(), {"claude": "키 없음"}))
    r = runner.invoke(app, ["bench", "judge", str(d), "-p", str(real), "--judge", "claude", "--yes"])
    assert r.exit_code == 2 and "키 없음" in r.output

    class Unpriced(Scripted):
        @property
        def priced(self):
            return False

    unpriced = Unpriced(lambda p: verdict(True))
    monkeypatch.setattr("devorch.cli.bench_cmd.build_registry", lambda cfg: (reg(unpriced), {}))
    r = runner.invoke(app, ["bench", "judge", str(d), "-p", str(real), "--judge", "claude", "--max-total-usd", "1", "--yes"])
    assert r.exit_code == 2 and "단가" in r.output
    assert judge.prompts == [] and unpriced.prompts == []
    assert not (d / "judged.json").exists()


def test_cli_judge_rejects_unknown_cases_in_the_results(tmp_path, monkeypatch):
    bad = result(C14, "single", 0, [fnd("x")])
    bad["case_id"] = "c99_unknown"
    d = write_results(tmp_path, [bad])
    judge = Scripted(lambda p: verdict(True))
    monkeypatch.setattr("devorch.cli.bench_cmd.build_registry", lambda cfg: (reg(judge), {}))
    r = CliRunner().invoke(app, ["bench", "judge", str(d), "-p", str(tmp_path), "--yes"])
    assert r.exit_code == 2 and "c99_unknown" in r.output and judge.prompts == []
