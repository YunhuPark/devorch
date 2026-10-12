"""Token-based cost estimates and their honesty about what is measured.

v2 (2026-10-11): input tokens count ASCII and non-ASCII (Hangul) characters separately, and output tokens are a RANGE
(low / mid / high) that grows with the prompt — a flat output guess was off by up to 42% on real 6-36KB diffs.
"""

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
    ASSUMED_ASCII_CHARS_PER_TOKEN,
    count_chars,
    estimate_call,
    estimate_counts,
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
def test_count_chars_splits_ascii_from_everything_else():
    assert count_chars("abc 가나다 é") == (5, 4)  # a b c and two spaces / 가 나 다 é


def test_input_tokens_use_one_rate_for_ascii_and_about_one_token_per_hangul_char():
    e = estimate_counts("openai", 3500, 0, {"openai": OA})
    assert e.input_tokens == 1000 and e.measured
    assert estimate_counts("openai", 0, 1000, {"openai": OA}).input_tokens == 950
    assert estimate_counts("gemini", 3000, 0, {"gemini": GE}).input_tokens == 1000  # gemini counts more tokens per ascii char


def test_output_is_a_range_that_grows_with_the_prompt_and_the_estimate_is_the_middle():
    small, big = estimate_counts("openai", 3500, 0, {"openai": OA}), estimate_counts("openai", 35000, 0, {"openai": OA})
    assert (small.output_low, small.output_tokens, small.output_high) == (25, 400, 1450)  # 300+0.10*in / 1200+0.25*in at in=1000
    assert big.output_tokens > small.output_tokens and big.output_high > small.output_high
    assert small.usd_low < small.usd < small.usd_high
    assert small.usd == pytest.approx((1000 * 2.0 + 400 * 10.0) / 1e6)
    g = estimate_counts("gemini", 3000, 0, {"gemini": GE})
    assert (g.output_low, g.output_tokens, g.output_high) == (200, 1250, 3000)  # thinking tokens are in the range


def test_the_output_budget_caps_the_range():
    e = estimate_counts("gemini", 60000, 0, {"gemini": GE}, max_output_tokens=4096)
    assert e.output_high == 4096 and e.output_tokens <= 4096
    assert estimate_counts("gemini", 60000, 0, {"gemini": GE}).output_high > 4096


def test_an_unmeasured_provider_is_flagged_not_silently_assumed():
    e = estimate_counts("claude", 3000, 0, {"claude": CL})
    assert not e.measured and e.input_tokens == round(3000 / ASSUMED_ASCII_CHARS_PER_TOKEN)


@pytest.mark.parametrize("prices", [None, {}, {"openai": {"input_per_mtok": 2.0}}, {"gemini": GE}])
def test_missing_or_incomplete_prices_mean_no_estimate_never_zero(prices):
    e = estimate_counts("openai", 3500, 0, prices)
    assert e.usd is None and e.usd_low is None and e.usd_high is None
    assert e.input_tokens == 1000  # the token estimate itself doesn't need a price


def test_estimate_call_counts_the_text_it_is_given():
    assert estimate_call("openai", "x" * 3500, {"openai": OA}).input_tokens == 1000


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


# ---- calibration 1: benchmark totals (small diffs) ---------------------------------------------------
# Measured 2026-10-09 (16 cases, 1 repeat; GPT $2/$10, Gemini $0.75/$3.75 per Mtok). If a case's task/diff text changes, recalibrate.
MEASURED_2026_10_09 = {"multi": 0.1751, "single": 0.0567, "solo:gemini:review": 0.0613, "solo:openai:architecture": 0.0535}
MODE_STEPS = {
    "multi": [("security", "openai"), ("architecture", "gemini"), ("review", "openai")],
    "single": [("review", "openai")],
    "solo:gemini:review": [("review", "gemini")],
    "solo:openai:architecture": [("architecture", "openai")],
}


def prompt_text(case, role):
    a = analyze_task(case.task)
    i = ImpactReport(direct_files=list(case.files))
    run = WorkflowRun(task_text=case.task, analysis=a, impact=i, risk=assess_risk(a, i), implementation=case.diff)
    step = review_steps(Policy(), "single")[0].model_copy(update={"role": role, "purpose": role})
    return REVIEWER_SYSTEM + build_review_prompt(run, step)


@pytest.mark.parametrize("mode", list(MODE_STEPS))
def test_formula_reproduces_the_measured_benchmark_total_within_15_percent(mode):
    prices = {"openai": OA, "gemini": GE}
    predicted = sum(estimate_call(p, prompt_text(c, role), prices).usd for c in load_cases() for role, p in MODE_STEPS[mode])
    assert predicted == pytest.approx(MEASURED_2026_10_09[mode], rel=0.15), (mode, predicted)


# ---- calibration 2: REAL reviews of this repository's own commits (HIGH, openai security + gemini architecture) ----------
# (ascii chars, non-ascii chars) of the real prompt, then per provider (actual input tokens, actual output tokens).
# Output tokens grow with the diff; Gemini's include thinking tokens.
# Batch 1 (2026-10-11 morning) set the v2 model. Batch 2 (same day, 7 fresh reviews) was the hold-out: inputs within -2..+6% on all
# 14 calls, outputs in range on 12/14, review totals in range on 6/7 -> the high bounds were widened (v3) and these rows added.
LARGE = {
    # batch 1
    "c9163fc": ((3333, 1216), {"openai": (2136, 27), "gemini": (2312, 249)}),
    "7a2ae3f": ((19009, 1118), {"openai": (6653, 907), "gemini": (7529, 3562)}),
    "5f24ce0 run1": ((27923, 1398), {"openai": (9145, 1566), "gemini": (None, 4096)}),  # gemini: cut at the old 4096 cap (a lower bound)
    "1e5fefa": ((32799, 1120), {"openai": (9942, 1280), "gemini": (11912, 2612)}),
    # batch 2 (hold-out, then folded in)
    "f5e2c94..a3e9a9e": ((7262, 681), {"openai": (2585, 897), "gemini": (2978, 3417)}),
    "04b4338..5cac504": ((8113, 1056), {"openai": (3262, 166), "gemini": (3692, 1927)}),
    "ec63b2d..dbb0c5a": ((15063, 1366), {"openai": (5595, 471), "gemini": (6469, 2439)}),
    "b2222c4..fdaedad": ((23806, 1133), {"openai": (7442, 2702), "gemini": (8821, 5770)}),
    "44755d4..7a2ae3f": ((33220, 2513), {"openai": (11959, 481), "gemini": (13614, 4314)}),
    "c129b42..6e874f3": ((61009, 2954), {"openai": (19459, 1507), "gemini": (23129, 5298)}),
    "5f24ce0 run2": ((27923, 1398), {"openai": (9145, 1381), "gemini": (10459, 5183)}),  # same prompt as run1, new output budget (8192)
}
PRICES = {"openai": OA, "gemini": GE}


@pytest.mark.parametrize("commit", list(LARGE))
def test_input_token_estimate_is_within_8_percent_on_real_diffs_including_hangul_heavy_ones(commit):
    (a, n), obs = LARGE[commit]
    for prov, (act_in, _) in obs.items():
        if act_in:
            est = estimate_counts(prov, a, n, PRICES).input_tokens
            assert abs(est - act_in) / act_in < 0.08, (commit, prov, est, act_in)


@pytest.mark.parametrize("commit", list(LARGE))
def test_every_observed_output_and_cost_falls_inside_the_estimated_range(commit):
    """IN-SAMPLE: the range was set/widened after seeing these 11 reviews (+ the benchmark). It documents coverage; it is not a held-out
    validation — new measurements must be added here, and a miss means the range needs widening, not the test loosening."""
    (a, n), obs = LARGE[commit]
    for prov, (act_in, act_out) in obs.items():
        e = estimate_counts(prov, a, n, PRICES, max_output_tokens=8192)
        assert e.output_low <= act_out <= e.output_high, (commit, prov, e.output_low, act_out, e.output_high)
        if act_in:
            p = PRICES[prov]
            actual = (act_in * p["input_per_mtok"] + act_out * p["output_per_mtok"]) / 1e6
            assert e.usd_low <= actual <= e.usd_high, (commit, prov, e.usd_low, actual, e.usd_high)


def test_the_old_flat_output_guess_was_wrong_for_big_diffs_which_is_why_the_range_exists():
    (a, n), obs = LARGE["7a2ae3f"]
    flat_out = {"openai": 317, "gemini": 979}  # the v1 constants
    assert obs["gemini"][1] > 3 * flat_out["gemini"] and obs["openai"][1] > 2 * flat_out["openai"]


# ---- the plan ---------------------------------------------------------------------------------------------
def plan(task=HIGH_TASK, files=("a.py",), prices=None, policy=None):
    analysis = analyze_task(task)
    impact = ImpactReport(direct_files=list(files))
    return plan_routing(analysis, assess_risk(analysis, impact), impact, policy or Policy(), prices=prices)


def test_without_prices_the_plan_is_exactly_the_old_flat_estimate_with_no_range():
    p = plan()
    assert p.estimated_cost_usd == pytest.approx(0.05 + 0.03 + 0.02)
    assert p.estimated_cost_low_usd == p.estimated_cost_high_usd == p.estimated_cost_usd
    assert {line.basis for line in p.cost_breakdown} == {"기본 고정값"}
    assert any("고정값" in n for n in p.notes)


def test_with_prices_reviewers_are_token_based_with_a_range_and_implementation_stays_a_flagged_default():
    p = plan(prices={"openai": OA, "gemini": GE})
    by = {line.role: line for line in p.cost_breakdown}
    assert by["implementation"].basis == "기본 고정값" and by["implementation"].usd == pytest.approx(0.05)
    assert by["implementation"].usd_low == by["implementation"].usd_high == by["implementation"].usd  # no range for an unmeasured step
    assert by["security"].basis == "토큰×단가" and by["architecture"].basis == "토큰×단가"
    assert by["security"].usd_low < by["security"].usd < by["security"].usd_high
    assert p.estimated_cost_usd == pytest.approx(sum(line.usd for line in p.cost_breakdown))
    assert p.estimated_cost_low_usd < p.estimated_cost_usd < p.estimated_cost_high_usd
    assert p.estimated_cost_usd < 0.10
    assert any("가정" in n and "diff" in n and "범위" in n for n in p.notes)


def test_the_plan_respects_the_review_output_budget_in_its_range():
    pol = Policy.model_validate({"limits": {"review_max_tokens": 1024}})
    capped = plan(files=[f"f{i}.py" for i in range(40)], prices={"openai": OA, "gemini": GE}, policy=pol)
    roomy = plan(files=[f"f{i}.py" for i in range(40)], prices={"openai": OA, "gemini": GE})
    assert capped.estimated_cost_high_usd < roomy.estimated_cost_high_usd


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


def test_service_analyze_reports_a_token_based_estimate_with_its_breakdown_and_range(repo):
    cfg = {"providers": {"openai": OA, "gemini": GE}}
    priced = make_plane(repo, cfg).analyze_task(HIGH_TASK + " calc.py")
    flat = make_plane(repo, {}).analyze_task(HIGH_TASK + " calc.py")
    assert priced["estimated_cost_usd"] < flat["estimated_cost_usd"]
    assert priced["estimated_cost_range_usd"][0] < priced["estimated_cost_usd"] < priced["estimated_cost_range_usd"][1]
    assert {x["basis"] for x in priced["cost_breakdown"] if x["role"] != "implementation"} == {"토큰×단가"}
    assert {x["basis"] for x in flat["cost_breakdown"]} == {"기본 고정값"}


async def test_service_review_estimates_from_the_real_diff_with_a_range_and_logs_it(repo):
    plane = make_plane(repo, {"providers": {"openai": OA, "gemini": GE}})
    tid = plane.analyze_task(HIGH_TASK + " calc.py")["task_id"]
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n" + "# padding line\n" * 200, encoding="utf-8")
    res = await plane.request_review(tid)
    est = res["estimated_review_cost_usd"]
    lo, hi = res["estimated_review_cost_range_usd"]
    assert est is not None and 0 < lo < est < hi < 0.5
    logged = [json.loads(l) for l in plane.audit.path.read_text(encoding="utf-8").splitlines()]
    ev = [e for e in logged if e["event"] == "mcp:review_estimate"]
    assert ev and ev[-1]["data"]["estimated_usd"] == pytest.approx(est) and ev[-1]["data"]["input_tokens"] > 1000  # the padded diff is counted
    assert ev[-1]["data"]["estimated_low_usd"] == pytest.approx(lo) and ev[-1]["data"]["estimated_high_usd"] == pytest.approx(hi)


async def test_service_review_has_no_estimate_when_a_reviewer_has_no_price(repo):
    plane = make_plane(repo, {"providers": {"openai": OA}})  # gemini unpriced
    tid = plane.analyze_task(HIGH_TASK + " calc.py")["task_id"]
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    res = await plane.request_review(tid)
    assert res["estimated_review_cost_usd"] is None and res["estimated_review_cost_range_usd"] is None  # unknown, not zero


def test_cli_analyze_shows_the_basis_and_the_range_of_the_estimate(tmp_path):
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
    assert priced.exit_code == 0 and "토큰×단가" in priced.output and "가정" in priced.output and "범위" in priced.output
    js = json.loads(runner.invoke(app, ["analyze", HIGH_TASK, "-p", str(tmp_path), "--json"]).output)
    assert any(x["basis"] == "토큰×단가" for x in js["plan"]["cost_breakdown"])


# ---- the implementation step (first real observation: 2026-10-12, claude-sonnet-5-5, a 2-line fix) ---------------------------
from devorch.core.orchestrator.engine import IMPLEMENTER_SYSTEM, WorkflowEngine, implementation_prompt_counts  # noqa: E402
from devorch.core.router.cost_model import estimate_implementation  # noqa: E402

CL_PRICES = {"claude": CL}


def test_the_implementation_estimate_has_a_flagged_output_range_and_a_real_input_size():
    e = estimate_implementation("claude", 3000, 0, CL_PRICES)
    assert not e.measured and e.input_tokens == 1000
    assert e.output_low < e.output_tokens < e.output_high
    assert e.usd_low < e.usd < e.usd_high
    assert estimate_implementation("claude", 3000, 0, None).usd is None  # no price: unknown, never 0


def test_the_one_real_implementation_observation_is_inside_the_range_and_the_flat_guess_was_far_too_high():
    # 265 input / 182 output tokens for a 2-line bug fix, billed at $2/$10 per Mtok
    actual = (265 * 2.0 + 182 * 10.0) / 1e6
    e = estimate_implementation("claude", 265 * 3, 0, CL_PRICES)
    assert e.usd_low <= actual <= e.usd_high
    assert 0.05 > 20 * actual  # the old flat default


def test_the_output_budget_caps_the_implementation_range():
    assert estimate_implementation("claude", 3000, 0, CL_PRICES, max_output_tokens=1024).output_high == 1024


def test_a_plan_uses_the_implementation_model_only_with_a_price_and_a_prompt_size():
    a, n = 3000, 0
    p = plan(files=["a.py"], prices=CL_PRICES, policy=None)  # no implementation counts passed: still the flat default
    assert next(x for x in p.cost_breakdown if x.role == "implementation").basis == "기본 고정값"
    analysis = analyze_task("버튼 텍스트 변경")
    impact = ImpactReport(direct_files=["a.py"])
    risk = assess_risk(analysis, impact)
    priced = plan_routing(analysis, risk, impact, Policy(), prices=CL_PRICES, implementation_counts=(a, n))
    impl = next(x for x in priced.cost_breakdown if x.role == "implementation")
    assert impl.basis.startswith("토큰×단가") and "가정" in impl.basis and impl.usd < 0.05 and impl.usd_low < impl.usd < impl.usd_high
    assert any("구현" in note and "1건" in note for note in priced.notes)
    unpriced = plan_routing(analysis, risk, impact, Policy(), prices={}, implementation_counts=(a, n))
    assert next(x for x in unpriced.cost_breakdown if x.role == "implementation").basis == "기본 고정값"


def test_the_implementation_prompt_size_counts_the_real_file_contents(tmp_path, repo):
    analysis = analyze_task("calc.py add 버그 수정")
    impact = ImpactReport(direct_files=["calc.py"])
    run = WorkflowRun(task_text="calc.py add 버그 수정", analysis=analysis, impact=impact, risk=assess_risk(analysis, impact))
    small = implementation_prompt_counts(run, None)
    reader = Executor(repo, Policy(), skip_tests=True)
    with_files = implementation_prompt_counts(run, reader.file_context)
    assert sum(with_files) > sum(small) >= len(IMPLEMENTER_SYSTEM)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n" + "# pad\n" * 800, encoding="utf-8")
    run_git(repo, "commit", "-q", "-am", "bigger")
    assert sum(implementation_prompt_counts(run, reader.file_context)) > sum(with_files) + 3000  # file size drives the input estimate


async def test_the_engine_plans_with_the_real_file_size_and_requests_the_implementation_output_budget(repo):
    seen = []
    claude = MockProvider("claude", ["diff"])
    reg = ProviderRegistry()
    for p in (claude, MockProvider("openai", ["[]"]), MockProvider("gemini", ["[]"])):
        reg.register(p)
    pol = Policy.model_validate({"limits": {"implementation_max_tokens": 9000}})
    reader = Executor(repo, pol, skip_tests=True)
    run = await WorkflowEngine(repo, pol, reg, context_provider=reader.file_context, approver=lambda r: True).run("calc.py add 버그 수정")
    assert claude.calls and claude.calls[0].max_tokens == 9000
    assert "return a - b" in claude.calls[0].prompt  # the real file reached the implementer
    assert run.plan is not None


def test_implementation_max_tokens_has_a_roomy_default_and_validation():
    assert Policy().limits.implementation_max_tokens == 16384
    for bad in (0, 512, 70000):
        with pytest.raises(ValueError):
            Policy.model_validate({"limits": {"implementation_max_tokens": bad}})


def test_cli_analyze_sizes_the_implementation_step_from_the_real_file(repo):
    """`analyze` used to show the flat $0.05 for the implementation step; it now uses the file's real size, like `run`."""
    cfgp = repo / ".devorch" / "config.yaml"
    CliRunner().invoke(app, ["init", "-p", str(repo)])
    cfg = yaml.safe_load(cfgp.read_text(encoding="utf-8"))
    cfg["providers"]["claude"].update(CL)
    cfgp.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    def impl_line(task):
        js = json.loads(CliRunner().invoke(app, ["analyze", task, "-p", str(repo), "--json"]).output)
        return next(x for x in js["plan"]["cost_breakdown"] if x["role"] == "implementation")

    small = impl_line("calc.py add 버그 수정")
    assert small["basis"].startswith("토큰×단가") and small["usd"] < 0.05  # not the flat default
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n" + "# filler line to make the file big\n" * 300, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "big")
    assert impl_line("calc.py add 버그 수정")["input_tokens"] > 10 * small["input_tokens"]
