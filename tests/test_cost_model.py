"""Token-based cost estimates (replacing the flat per-call guesses) and their honesty about what is measured."""

import json
import sys

import pytest
import yaml
from gitutil import run_git
from typer.testing import CliRunner

from devorch.bench.cases import load_cases
from devorch.bench.runner import review_steps
from devorch.cli.main import app
from devorch.config import Policy
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.execution.executor import Executor
from devorch.core.orchestrator.engine import WorkflowRun
from devorch.core.orchestrator.review import REVIEWER_SYSTEM, build_review_prompt
from devorch.core.router.cost_model import (
    ASSUMED_CHARS_PER_TOKEN,
    ASSUMED_OUTPUT_TOKENS,
    estimate_call,
    merge_prices,
    prices_from_config,
    prices_from_registry,
)
from devorch.core.router.model_router import plan_routing
from devorch.models import ImpactReport
from devorch.providers.adapters import AnthropicProvider
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane

OA = {"input_per_mtok": 2.0, "output_per_mtok": 10.0}
GE = {"input_per_mtok": 0.75, "output_per_mtok": 3.75}
CL = {"input_per_mtok": 2.0, "output_per_mtok": 10.0}
HIGH_TASK = "로그인 JWT 인증 추가"


# ---- the formula -----------------------------------------------------------------------------------
def test_estimate_uses_the_measured_coefficients_for_openai_and_gemini():
    e = estimate_call("openai", 3360, {"openai": OA})
    assert (e.input_tokens, e.output_tokens, e.measured) == (1000, 317, True)
    assert e.usd == pytest.approx((1000 * 2.0 + 317 * 10.0) / 1e6)
    g = estimate_call("gemini", 3010, {"gemini": GE})
    assert (g.input_tokens, g.output_tokens, g.measured) == (1000, 979, True)  # Gemini's output includes thinking tokens


def test_an_unmeasured_provider_gets_flagged_assumptions_not_silent_ones():
    e = estimate_call("claude", 3000, {"claude": CL})
    assert not e.measured
    assert (e.input_tokens, e.output_tokens) == (round(3000 / ASSUMED_CHARS_PER_TOKEN), ASSUMED_OUTPUT_TOKENS)


@pytest.mark.parametrize("prices", [None, {}, {"openai": {"input_per_mtok": 2.0}}, {"gemini": GE}])
def test_missing_or_incomplete_prices_mean_no_estimate_never_zero(prices):
    assert estimate_call("openai", 3360, prices).usd is None


def test_bigger_prompts_cost_more_and_the_estimate_is_linear_in_the_input():
    a, b = estimate_call("openai", 3000, {"openai": OA}), estimate_call("openai", 6000, {"openai": OA})
    assert b.usd > a.usd and b.output_tokens == a.output_tokens and b.input_tokens == 2 * a.input_tokens


def test_prices_come_from_config_and_from_registered_adapters_and_the_registry_wins():
    cfg = {"providers": {"openai": {**OA, "model": "m"}, "gemini": {"model": "g"}, "claude": {"input_per_mtok": 1}}}
    assert prices_from_config(cfg) == {"openai": OA}  # gemini has no prices, claude only half of them
    reg = ProviderRegistry()
    reg.register(AnthropicProvider(model="m", client=object(), prices=CL))
    reg.register(MockProvider("openai"))  # no prices attribute at all
    assert prices_from_registry(reg) == {"claude": CL}
    assert merge_prices(prices_from_config(cfg), prices_from_registry(reg)) == {"openai": OA, "claude": CL}
    other = {"claude": {"input_per_mtok": 9.0, "output_per_mtok": 9.0}}
    assert merge_prices({"claude": CL}, other)["claude"]["input_per_mtok"] == 9.0  # later sources override


# ---- calibration: the numbers must reproduce what was measured ----------------------------------------
# Measured on 2026-10-09 (16 cases, 1 repeat, GPT $2/$10 and Gemini $0.75/$3.75 per Mtok). The coefficients were fitted on a
# DIFFERENT run (2026-10-08, 11 cases) — this is a held-out check. If the cases' task/diff text changes, recalibrate.
MEASURED_2026_10_09 = {"multi": 0.1751, "single": 0.0567, "solo:gemini:review": 0.0613, "solo:openai:architecture": 0.0535}
MODE_STEPS = {
    "multi": [("security", "openai"), ("architecture", "gemini"), ("review", "openai")],
    "single": [("review", "openai")],
    "solo:gemini:review": [("review", "gemini")],
    "solo:openai:architecture": [("architecture", "openai")],
}


def prompt_chars(case, role):
    a = analyze_task(case.task)
    i = ImpactReport(direct_files=list(case.files))
    run = WorkflowRun(task_text=case.task, analysis=a, impact=i, risk=assess_risk(a, i), implementation=case.diff)
    step = review_steps(Policy(), "single")[0].model_copy(update={"role": role, "purpose": role})
    return len(REVIEWER_SYSTEM) + len(build_review_prompt(run, step))


@pytest.mark.parametrize("mode", list(MODE_STEPS))
def test_formula_reproduces_the_measured_total_cost_within_15_percent(mode):
    prices = {"openai": OA, "gemini": GE}
    predicted = sum(estimate_call(p, prompt_chars(c, role), prices).usd for c in load_cases() for role, p in MODE_STEPS[mode])
    assert predicted == pytest.approx(MEASURED_2026_10_09[mode], rel=0.15), (mode, predicted)


def test_the_old_flat_estimate_was_far_off_which_is_why_it_was_replaced():
    flat = len(load_cases()) * (0.03 + 0.02 + 0.03)  # old defaults: openai 0.03, gemini 0.02, openai 0.03 per multi case
    assert flat > 3 * MEASURED_2026_10_09["multi"]


# ---- the plan ---------------------------------------------------------------------------------------------
def plan(task=HIGH_TASK, files=("a.py",), prices=None, policy=None):
    analysis = analyze_task(task)
    impact = ImpactReport(direct_files=list(files))
    return plan_routing(analysis, assess_risk(analysis, impact), impact, policy or Policy(), prices=prices)


def test_without_prices_the_plan_is_exactly_the_old_flat_estimate():
    p = plan()
    assert p.estimated_cost_usd == pytest.approx(0.05 + 0.03 + 0.02)
    assert {line.basis for line in p.cost_breakdown} == {"기본 고정값"}
    assert any("고정값" in n for n in p.notes)


def test_with_prices_reviewers_are_token_based_and_implementation_stays_flagged_as_a_default():
    p = plan(prices={"openai": OA, "gemini": GE})
    by = {line.role: line for line in p.cost_breakdown}
    assert by["implementation"].basis == "기본 고정값" and by["implementation"].usd == pytest.approx(0.05)
    assert by["security"].basis == "토큰×단가" and by["architecture"].basis == "토큰×단가"
    assert p.estimated_cost_usd == pytest.approx(sum(line.usd for line in p.cost_breakdown))
    assert p.estimated_cost_usd < 0.10  # reviewers are now ~$0.005 each instead of $0.02-0.03
    assert any("가정" in n and "diff" in n for n in p.notes)  # the diff size is an assumption at analysis time


def test_a_priced_but_unmeasured_provider_is_labelled_as_an_assumption():
    pol = Policy.model_validate({"rules": {**Policy().rules, "high": {"implementation": "openai", "review": "claude"}}})
    p = plan(prices={"openai": OA, "claude": CL}, policy=pol)
    line = next(x for x in p.cost_breakdown if x.provider == "claude")
    assert line.basis == "토큰×단가(미측정 가정)"
    assert any("미측정" in n and "claude" in n for n in p.notes)


def test_a_mix_of_priced_and_unpriced_reviewers_falls_back_per_provider():
    p = plan(prices={"openai": OA})  # gemini has no price
    bases = {line.provider: line.basis for line in p.cost_breakdown if line.role != "implementation"}
    assert bases == {"openai": "토큰×단가", "gemini": "기본 고정값"}


def test_more_affected_files_means_a_higher_estimate():
    prices = {"openai": OA, "gemini": GE}
    assert plan(files=[f"f{i}.py" for i in range(12)], prices=prices).estimated_cost_usd > plan(files=["a.py"], prices=prices).estimated_cost_usd


# ---- wiring: MCP service and CLI ---------------------------------------------------------------------------
PYTEST_CMD = f"{sys.executable} -m pytest -q"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    run_git(r, "config", "user.email", "t@example.com")
    run_git(r, "config", "user.name", "T")
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "init")
    return r


def make_plane(repo, config):
    reg = ProviderRegistry()
    for name in ("claude", "openai", "gemini"):
        reg.register(MockProvider(name, ["[]"] if name != "claude" else ["x"]))
    pol = Policy(allowed_commands=[PYTEST_CMD])
    return ControlPlane(repo, pol, config, reg, Executor(repo, pol, test_command=PYTEST_CMD))


def test_service_analyze_reports_a_token_based_estimate_with_its_breakdown(repo):
    cfg = {"providers": {"openai": OA, "gemini": GE}}
    priced = make_plane(repo, cfg).analyze_task(HIGH_TASK + " calc.py")
    flat = make_plane(repo, {}).analyze_task(HIGH_TASK + " calc.py")
    assert priced["estimated_cost_usd"] < flat["estimated_cost_usd"]
    assert {x["basis"] for x in priced["cost_breakdown"] if x["role"] != "implementation"} == {"토큰×단가"}
    assert {x["basis"] for x in flat["cost_breakdown"]} == {"기본 고정값"}


async def test_service_review_estimates_from_the_real_diff_and_logs_it(repo):
    plane = make_plane(repo, {"providers": {"openai": OA, "gemini": GE}})
    tid = plane.analyze_task(HIGH_TASK + " calc.py")["task_id"]
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n" + "# padding line\n" * 200, encoding="utf-8")
    res = await plane.request_review(tid)
    est = res["estimated_review_cost_usd"]
    assert est is not None and 0 < est < 0.5
    logged = [json.loads(l) for l in plane.audit.path.read_text(encoding="utf-8").splitlines()]
    ev = [e for e in logged if e["event"] == "mcp:review_estimate"]
    assert ev and ev[-1]["data"]["estimated_usd"] == pytest.approx(est) and ev[-1]["data"]["input_tokens"] > 1000  # the padded diff is counted


async def test_service_review_has_no_estimate_when_a_reviewer_has_no_price(repo):
    plane = make_plane(repo, {"providers": {"openai": OA}})  # gemini unpriced
    tid = plane.analyze_task(HIGH_TASK + " calc.py")["task_id"]
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert (await plane.request_review(tid))["estimated_review_cost_usd"] is None  # unknown, not zero


def test_cli_analyze_shows_the_basis_of_the_estimate(tmp_path):
    runner = CliRunner()
    runner.invoke(app, ["init", "-p", str(tmp_path)])
    flat = runner.invoke(app, ["analyze", HIGH_TASK, "-p", str(tmp_path)])
    assert flat.exit_code == 0 and "기본 고정값" in flat.output
    cfgp = tmp_path / ".devorch" / "config.yaml"
    cfg = yaml.safe_load(cfgp.read_text(encoding="utf-8"))
    cfg["providers"]["openai"].update(OA)
    cfg["providers"]["gemini"].update(GE)
    cfgp.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    priced = runner.invoke(app, ["analyze", HIGH_TASK, "-p", str(tmp_path)])
    assert priced.exit_code == 0 and "토큰×단가" in priced.output and "가정" in priced.output
    js = json.loads(runner.invoke(app, ["analyze", HIGH_TASK, "-p", str(tmp_path), "--json"]).output)
    assert any(x["basis"] == "토큰×단가" for x in js["plan"]["cost_breakdown"])
