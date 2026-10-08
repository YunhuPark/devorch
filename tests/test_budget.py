"""Per-task spend cap (limits.max_cost_usd) and the benchmark report's failure visibility."""

import json
import sys

import pytest
from gitutil import run_git

from devorch.audit import AuditLogger
from devorch.bench.cases import load_cases
from devorch.bench.metrics import score_run
from devorch.bench.report import render_report
from devorch.config import Policy
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.execution.executor import Executor
from devorch.core.orchestrator.engine import WorkflowEngine, WorkflowRun
from devorch.core.orchestrator.review import BudgetExceeded, budget_preflight, call_provider, run_reviews
from devorch.core.orchestrator.state import State
from devorch.models import ImpactReport, RoutingStep
from devorch.providers.adapters import AnthropicProvider
from devorch.providers.base import LLMProvider, LLMRequest, LLMResponse, ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane, ControlPlaneError

SEC_FINDING = json.dumps([{"severity": "high", "category": "security", "title": "권한 검증 누락", "file": "calc.py"}])
HIGH_TASK = "로그인 JWT 인증 추가"
PYTEST_CMD = f"{sys.executable} -m pytest -q"


class FakeProvider(LLMProvider):
    """Reports tokens and a fixed cost per call; ``priced=False`` mimics an adapter without configured prices."""

    def __init__(self, name, responses, cost=0.04, priced=True):
        self.name, self._responses, self._cost, self._priced = name, responses, cost, priced
        self.calls: list[LLMRequest] = []

    @property
    def priced(self) -> bool:
        return self._priced

    async def generate(self, request):
        self.calls.append(request)
        text = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        return LLMResponse(
            text=text, provider=self.name, model=f"{self.name}-test", input_tokens=100, output_tokens=50,
            cost_usd=self._cost if self._priced else None,
        )


def registry(**providers):
    reg = ProviderRegistry()
    for name in ("claude", "openai", "gemini"):
        reg.register(providers.get(name) or FakeProvider(name, ["impl"] if name == "claude" else ["[]"]))
    return reg


def make_run(task="버튼 텍스트 변경", **kw):
    analysis = analyze_task(task)
    impact = ImpactReport()
    return WorkflowRun(task_text=task, analysis=analysis, impact=impact, risk=assess_risk(analysis, impact), implementation="diff", **kw)


def events(path, name):
    return [e for e in (json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()) if e["event"] == name]


STEP = RoutingStep(role="review", provider="openai", purpose="review")


# ---- the cap is a hard stop before the next call -----------------------------------------------
async def test_call_is_refused_once_the_cap_is_reached_and_the_provider_is_never_called(tmp_path):
    openai = FakeProvider("openai", ["[]"])
    run = make_run(total_cost_usd=0.10)
    audit = AuditLogger(tmp_path / "a.jsonl")
    with pytest.raises(BudgetExceeded, match="상한 도달"):
        await call_provider(run, registry(openai=openai), audit, STEP, LLMRequest(role="review", prompt="x"), max_cost_usd=0.10)
    assert openai.calls == []  # no tokens spent
    (ev,) = events(tmp_path / "a.jsonl", "budget_exceeded")
    assert ev["data"]["spent_usd"] == 0.10 and ev["data"]["max_cost_usd"] == 0.10


async def test_unknown_cost_with_a_cap_stops_further_calls(tmp_path):
    openai = FakeProvider("openai", ["[]"])
    run = make_run(cost_unknown=True)  # an earlier call had no price → the total can't be trusted
    with pytest.raises(BudgetExceeded, match="알 수 없는"):
        await call_provider(run, registry(openai=openai), AuditLogger(None), STEP, LLMRequest(role="review", prompt="x"), max_cost_usd=5.0)
    assert openai.calls == []


async def test_without_a_cap_nothing_changes(tmp_path):
    openai = FakeProvider("openai", ["[]"], cost=999.0)
    run = make_run(total_cost_usd=1000.0, cost_unknown=True)
    await call_provider(run, registry(openai=openai), AuditLogger(None), STEP, LLMRequest(role="review", prompt="x"))
    assert len(openai.calls) == 1


async def test_a_reviewer_blocked_by_the_cap_is_incomplete_never_a_pass():
    openai = FakeProvider("openai", ["[]"])
    run = make_run(total_cost_usd=0.50)
    findings, completed = await run_reviews(run, [STEP], registry(openai=openai), AuditLogger(None), max_cost_usd=0.50)
    assert completed == [] and findings == [] and openai.calls == []
    assert any("BudgetExceeded" in n for n in run.notes)


# ---- engine: stops the revision loop ----------------------------------------------------------
async def test_engine_stops_before_the_next_round_when_over_budget(tmp_path):
    claude = FakeProvider("claude", ["impl v1", "impl v2"])
    openai = FakeProvider("openai", [SEC_FINDING])  # blocking every round → would loop
    gemini = FakeProvider("gemini", ["[]"])
    pol = Policy.model_validate({"limits": {"max_cost_usd": 0.10}})
    audit_path = tmp_path / "audit.jsonl"
    eng = WorkflowEngine(tmp_path, pol, registry(claude=claude, openai=openai, gemini=gemini), audit=AuditLogger(audit_path), approver=lambda r: True)

    run = await eng.run(HIGH_TASK)

    assert run.state is State.FAILED and "BudgetExceeded" in run.failure_reason
    assert len(claude.calls) == 1  # round 2's implementation call was never made
    assert run.total_cost_usd == pytest.approx(0.12)  # impl 0.04 + two parallel reviews 0.04 each
    assert any("비용 상한 초과" in n for n in run.notes)  # the overshoot is reported, not hidden
    assert events(audit_path, "budget_exceeded")


async def test_engine_refuses_to_start_when_a_provider_has_no_prices(tmp_path):
    claude, openai = FakeProvider("claude", ["impl"]), FakeProvider("openai", ["[]"], priced=False)
    eng = WorkflowEngine(tmp_path, Policy.model_validate({"limits": {"max_cost_usd": 1.0}}), registry(claude=claude, openai=openai), approver=lambda r: True)
    run = await eng.run(HIGH_TASK)
    assert run.state is State.FAILED and "단가" in run.failure_reason and "openai" in run.failure_reason
    assert claude.calls == [] and openai.calls == []  # nothing was spent


async def test_engine_without_a_cap_still_runs_unpriced_providers(tmp_path):
    openai = FakeProvider("openai", ["[]"], priced=False)
    run = await WorkflowEngine(tmp_path, Policy(), registry(openai=openai), approver=lambda r: True).run(HIGH_TASK)
    assert run.state is State.COMPLETED and run.cost_unknown


async def test_engine_within_budget_completes_and_logs_tokens_and_totals(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    pol = Policy.model_validate({"limits": {"max_cost_usd": 1.0}})
    run = await WorkflowEngine(tmp_path, pol, registry(), audit=AuditLogger(audit_path), approver=lambda r: True).run("버튼 텍스트 변경")
    assert run.state is State.COMPLETED and not events(audit_path, "budget_exceeded")
    (call,) = events(audit_path, "llm_call")
    assert call["data"]["input_tokens"] == 100 and call["data"]["output_tokens"] == 50
    assert call["data"]["total_cost_usd"] == 0.04 and call["data"]["max_cost_usd"] == 1.0
    assert run.models == {"claude": "claude-test"}


# ---- policy / provider plumbing ----------------------------------------------------------------
@pytest.mark.parametrize("bad", [0, -1, -0.01])
def test_policy_rejects_non_positive_caps(bad):
    with pytest.raises(ValueError):
        Policy.model_validate({"limits": {"max_cost_usd": bad}})


def test_cap_defaults_to_none():
    assert Policy().limits.max_cost_usd is None


def test_adapters_are_priced_only_when_prices_are_configured():
    prices = {"input_per_mtok": 1.0, "output_per_mtok": 2.0}
    assert AnthropicProvider(model="m", client=object(), prices=prices).priced
    assert not AnthropicProvider(model="m", client=object()).priced
    assert not AnthropicProvider(model="m", client=object(), prices={"input_per_mtok": 1.0}).priced  # half a price list is none
    assert MockProvider("claude").priced  # mock reports cost 0.0


def test_budget_preflight_names_only_unpriced_registered_providers():
    reg = registry(openai=FakeProvider("openai", ["[]"], priced=False))
    assert budget_preflight(reg, ["claude", "openai"], None) is None
    assert budget_preflight(reg, ["claude"], 1.0) is None
    msg = budget_preflight(reg, ["claude", "openai", "not-registered"], 1.0)
    assert msg and "openai" in msg and "not-registered" not in msg and "claude" not in msg


# ---- MCP service: request_review ----------------------------------------------------------------
def test_service_refuses_review_before_spending_when_a_provider_is_unpriced(tmp_path):
    import asyncio

    repo = tmp_path / "proj"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "T")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    openai = FakeProvider("openai", ["[]"], priced=False)
    pol = Policy(allowed_commands=[PYTEST_CMD], limits={"max_cost_usd": 1.0})
    plane = ControlPlane(repo, pol, {}, registry(openai=openai), Executor(repo, pol, test_command=PYTEST_CMD))
    tid = plane.analyze_task("로그인 JWT 인증 추가 calc.py")["task_id"]
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    with pytest.raises(ControlPlaneError, match="단가"):
        asyncio.run(plane.request_review(tid))
    assert openai.calls == []


# ---- failure descriptions keep the HTTP status and body, redacted -----------------------------------
def test_describe_error_adds_status_and_body_when_the_sdk_message_drops_them():
    from types import SimpleNamespace as NS

    from devorch.core.orchestrator.review import describe_error

    class BadRequestError(Exception):
        status_code = 400

    e = BadRequestError("Error code: 400")
    e.response = NS(text='{"error": {"message": "Invalid header value", "key": "sk-abcdefghijklmnopqrstuvwxyz0123"}}')
    out = describe_error(e)
    assert "BadRequestError" in out and "Invalid header value" in out and "응답 본문" in out
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in out  # redacted


def test_describe_error_never_fails_and_does_not_duplicate_a_body_already_in_the_message():
    from types import SimpleNamespace as NS

    from devorch.core.orchestrator.review import describe_error

    class Weird(Exception):
        @property
        def response(self):
            raise RuntimeError("body not read")

    assert describe_error(Weird("boom")) == "Weird: boom"
    e = Exception("Error code: 400 - {'message': 'bad'}")
    e.response = NS(text="{'message': 'bad'}")
    assert describe_error(e).count("bad") == 1


async def test_a_failed_review_note_carries_the_response_body():
    class Boom(FakeProvider):
        async def generate(self, request):
            err = RuntimeError("Error code: 400")
            err.status_code = 400
            err.response = type("R", (), {"text": "<html>Error 400 (Bad Request)</html>"})()
            raise err

    run = make_run()
    findings, completed = await run_reviews(run, [STEP], registry(openai=Boom("openai", ["[]"])), AuditLogger(None))
    assert completed == []
    (note,) = [n for n in run.notes if "호출 실패" in n]
    assert "Error code: 400" in note and "응답 본문: <html>Error 400 (Bad Request)</html>" in note
    assert note.count("HTTP 400") == 0  # the status is already in the message, so it isn't repeated


# ---- token usage is recorded even without prices -------------------------------------------------
async def test_usage_accumulates_per_provider_even_when_unpriced():
    openai = FakeProvider("openai", ["[]"], priced=False)
    run = make_run()
    reg = registry(openai=openai)
    for _ in range(2):
        await call_provider(run, reg, AuditLogger(None), STEP, LLMRequest(role="review", prompt="x"))
    assert run.usage == {"openai": {"calls": 2, "input_tokens": 200, "output_tokens": 100}}
    assert run.cost_unknown  # tokens are known, the cost is not


async def test_a_failed_call_records_no_tokens():
    class Boom(FakeProvider):
        async def generate(self, request):
            raise RuntimeError("HTTP 400")

    run = make_run()
    with pytest.raises(RuntimeError):
        await call_provider(run, registry(openai=Boom("openai", ["[]"])), AuditLogger(None), STEP, LLMRequest(role="review", prompt="x"))
    assert run.usage == {}


async def test_benchmark_results_and_report_carry_tokens(tmp_path):
    from devorch.bench.metrics import summarize
    from devorch.bench.runner import run_benchmark

    cases = load_cases()[:3]
    reg = registry(openai=FakeProvider("openai", ["[]"], priced=False))
    results = await run_benchmark(cases, Policy(), reg, ["single"])
    assert all(r.usage == {"openai": {"calls": 1, "input_tokens": 100, "output_tokens": 50}} for r in results)
    assert all(r.to_dict()["usage"]["openai"]["input_tokens"] == 100 for r in results)
    s = summarize(cases, results, "single")
    assert (s.input_tokens, s.output_tokens) == (300, 150) and s.cost_unknown
    text = render_report(cases, results, ["single"], mock=False, repeats=1)
    assert "토큰(입력/출력)" in text and "300 / 150" in text


# ---- benchmark: a TOTAL cap across all cases (the per-case cap can't bound a whole run) ---------------
async def test_benchmark_total_cap_stops_starting_new_cases_and_reports_them_incomplete():
    from devorch.bench.runner import run_benchmark

    cases = load_cases()[:5]
    openai = FakeProvider("openai", ["[]"], cost=0.04)
    results = await run_benchmark(cases, Policy(), registry(openai=openai), ["single"], concurrency=1, max_total_cost_usd=0.10)
    ran = [r for r in results if r.completed]
    skipped = [r for r in results if not r.completed]
    assert len(ran) == 3 and len(skipped) == 2  # 0.04 -> 0.08 -> 0.12 (>= cap), then nothing more starts
    assert len(openai.calls) == 3  # skipped cases made no LLM call at all
    assert all("합계 상한" in n for r in skipped for n in r.notes)
    assert sum(r.cost_usd for r in results) == pytest.approx(0.12)


async def test_benchmark_without_total_cap_runs_everything():
    from devorch.bench.runner import run_benchmark

    cases = load_cases()[:4]
    openai = FakeProvider("openai", ["[]"], cost=5.0)
    results = await run_benchmark(cases, Policy(), registry(openai=openai), ["single"], concurrency=1)
    assert all(r.completed for r in results) and len(openai.calls) == 4


def test_bench_cli_refuses_a_total_cap_with_unpriced_providers_before_any_call(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from devorch.cli.main import app

    openai = FakeProvider("openai", ["[]"], priced=False)
    monkeypatch.setattr("devorch.cli.bench_cmd.build_registry", lambda cfg: (registry(openai=openai), {}))
    r = CliRunner().invoke(app, ["bench", "run", "-p", str(tmp_path), "--mode", "single", "--max-total-usd", "1", "--yes"])
    assert r.exit_code == 2 and "단가" in r.output and openai.calls == []


def test_bench_cli_rejects_a_non_positive_total_cap(tmp_path):
    from typer.testing import CliRunner

    from devorch.cli.main import app

    r = CliRunner().invoke(app, ["bench", "run", "-p", str(tmp_path), "--mock", "--max-total-usd", "0"])
    assert r.exit_code != 0


# ---- benchmark report: failures must be visible ---------------------------------------------------
CASES = load_cases()[:2]
CREDIT_ERR = (
    "[{cid}] review(claude) 호출 실패: BadRequestError: Error code: 400 - {{'type': 'error', 'error': "
    "{{'message': 'Your credit balance is too low'}}, 'request_id': 'req_{n}'}}"
)


def result(case, completed, notes=(), models=None):
    return score_run(case, "single", 0, [], completed, notes=list(notes), models=models or {})


def test_report_warns_loudly_when_every_review_failed_and_dedupes_reasons():
    rs = [result(c, False, [CREDIT_ERR.format(cid=c.id, n=i)]) for i, c in enumerate(CASES)]
    text = render_report(CASES, rs, ["single"], mock=False, repeats=1)
    head = text.split("## 요약")[0]
    assert "모든 리뷰(2건)가 실패했습니다" in head and "인용하지 마세요" in head
    assert head.count("credit balance is too low") == 1  # same reason once, not once per case
    assert "req_0" not in text and "req_1" not in text  # per-request ids stripped
    assert "사용 모델: 기록 없음" in text


def test_report_partial_failure_warning_and_models_line():
    rs = [result(CASES[0], True, models={"claude": "claude-x"}), result(CASES[1], False, ["[c] review(claude) 응답을 파싱하지 못해 리뷰 미완료로 처리"])]
    text = render_report(CASES, rs, ["single"], mock=False, repeats=1)
    assert "불완전 리뷰 1/2건" in text and "모든 리뷰" not in text
    assert "파싱하지 못해" in text and "사용 모델: claude=claude-x" in text


def test_report_has_no_warning_when_everything_completed():
    rs = [result(c, True, models={"claude": "claude-x"}) for c in CASES]
    assert "⚠️" not in render_report(CASES, rs, ["single"], mock=False, repeats=1)


def test_report_failure_reasons_are_redacted():
    leak = "sk-abcdefghijklmnopqrstuvwxyz0123"
    rs = [result(c, False, [f"[{c.id}] review(claude) 호출 실패: AuthError: bad key {leak}"]) for c in CASES]
    text = render_report(CASES, rs, ["single"], mock=False, repeats=1)
    assert leak not in text and "AuthError" in text
