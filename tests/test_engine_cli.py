import json

import pytest
from typer.testing import CliRunner

from devorch.audit import AuditLogger
from devorch.cli.main import app
from devorch.config import Policy
from devorch.core.orchestrator.engine import WorkflowEngine
from devorch.core.orchestrator.state import State
from devorch.providers.base import LLMProvider, LLMRequest, ProviderRegistry
from devorch.providers.mock import MockProvider

SEC_FINDING = json.dumps(
    [{"severity": "high", "category": "security", "title": "권한 검증 누락", "detail": "role check missing", "file": "AuthController.java"}]
)
ARCH_WARN = json.dumps([{"severity": "medium", "category": "architecture", "title": "SecurityFilterChain 구조 개선"}])


def registry(**providers):
    reg = ProviderRegistry()
    for name in ("claude", "openai", "gemini"):
        reg.register(providers.get(name) or MockProvider(name, ["impl"] if name == "claude" else ["[]"]))
    return reg


def engine(tmp_path, reg, approver=None, **kw):
    return WorkflowEngine(tmp_path, Policy(), reg, audit=AuditLogger(tmp_path / "audit.jsonl"), approver=approver, **kw)


async def test_low_risk_runs_straight_through(tmp_path):
    reg = registry()
    run = await engine(tmp_path, reg).run("버튼 텍스트 변경")
    assert run.state is State.COMPLETED and run.rounds == 1
    assert reg.get("openai").calls == [] and reg.get("gemini").calls == []  # single model
    assert run.approved is None


async def test_high_risk_not_ready_then_fixed_then_approved(tmp_path):
    """The design doc's demo: GPT finds a security hole, Claude fixes it, then approval."""
    openai = MockProvider("openai", [SEC_FINDING, "[]"])
    gemini = MockProvider("gemini", [ARCH_WARN, "[]"])
    claude = MockProvider("claude", ["impl v1", "impl v2"])
    asked = []

    def approver(run):
        asked.append(run.evaluation.status)
        return True

    run = await engine(tmp_path, registry(claude=claude, openai=openai, gemini=gemini), approver).run("로그인 기능에 JWT 인증 추가해줘")

    assert run.state is State.COMPLETED
    assert run.rounds == 2 and run.approved is True
    assert asked == ["READY"]
    # revision prompt carries the blocking finding, not the warning
    assert "권한 검증 누락" in claude.calls[1].prompt and "impl v1" in claude.calls[1].prompt
    assert "SecurityFilterChain" not in claude.calls[1].prompt


async def test_reviewers_are_independent(tmp_path):
    openai = MockProvider("openai", [SEC_FINDING])
    gemini = MockProvider("gemini", ["[]"])
    await engine(tmp_path, registry(openai=openai, gemini=gemini), lambda r: True).run("로그인 JWT 인증 추가")
    assert "권한 검증 누락" not in gemini.calls[0].prompt  # gemini never sees openai's findings


async def test_gives_up_after_max_retries(tmp_path):
    openai = MockProvider("openai", [SEC_FINDING])  # always finds the same problem
    pol = Policy.model_validate({"limits": {"max_retries": 2}})
    eng = WorkflowEngine(tmp_path, pol, registry(openai=openai), approver=lambda r: True)
    run = await eng.run("로그인 JWT 인증 추가")
    assert run.state is State.FAILED and run.rounds == 3
    assert "차단 이슈" in run.failure_reason


async def test_rejected_by_human(tmp_path):
    run = await engine(tmp_path, registry(), lambda r: False).run("로그인 JWT 인증 추가")
    assert run.state is State.REJECTED and run.approved is False


async def test_async_approver_supported(tmp_path):
    async def approver(run):
        return True

    run = await engine(tmp_path, registry(), approver).run("로그인 JWT 인증 추가")
    assert run.state is State.COMPLETED


async def test_approval_required_but_no_approver_fails_closed(tmp_path):
    run = await engine(tmp_path, registry()).run("로그인 JWT 인증 추가")
    assert run.state is State.FAILED and "approver" in run.failure_reason


async def test_failed_reviewer_call_is_not_treated_as_pass(tmp_path):
    class Boom(LLMProvider):
        name = "openai"

        async def generate(self, request: LLMRequest):
            raise TimeoutError("upstream timeout")

    run = await engine(tmp_path, registry(openai=Boom()), lambda r: True).run("로그인 JWT 인증 추가")
    assert run.state is State.FAILED
    assert "필수 리뷰 미완료" in run.failure_reason and "security" in run.failure_reason
    assert any("TimeoutError" in n for n in run.notes)


async def test_unparsable_review_is_not_treated_as_pass(tmp_path):
    openai = MockProvider("openai", ["LGTM!"])
    run = await engine(tmp_path, registry(openai=openai), lambda r: True).run("로그인 JWT 인증 추가")
    assert run.state is State.FAILED and "security" in run.failure_reason


async def test_unregistered_provider_fails_cleanly(tmp_path):
    reg = ProviderRegistry()
    reg.register(MockProvider("claude", ["x"]))
    run = await WorkflowEngine(tmp_path, Policy(), reg, approver=lambda r: True).run("로그인 JWT 인증 추가")
    assert run.state is State.FAILED
    assert "등록되지 않은 provider: gemini, openai" in run.failure_reason
    assert reg.get("claude").calls == []  # failed before spending any tokens


async def test_hooks_and_test_failure(tmp_path):
    applied = []
    run = await engine(tmp_path, registry(), lambda r: True, apply_hook=lambda r: applied.append(r.id), test_hook=lambda r: False).run("로그인 JWT 인증 추가")
    assert applied == [run.id]
    assert run.state is State.FAILED and run.failure_reason == "테스트 실패"


async def test_skipped_steps_are_recorded_not_hidden(tmp_path):
    run = await engine(tmp_path, registry()).run("버튼 텍스트 변경")
    assert any("apply" in n for n in run.notes) and any("test" in n for n in run.notes)


async def test_empty_task_fails_and_audit_is_written_and_redacted(tmp_path):
    run = await engine(tmp_path, registry()).run("   ")
    assert run.state is State.FAILED
    log = tmp_path / "audit.jsonl"
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert events[0]["event"] == "task_created" and events[-1]["data"]["to"] == "FAILED"

    run2 = await engine(tmp_path, registry()).run("버튼 텍스트 변경 api_key=sk-abcdefghijklmnopqrstuv")
    text = log.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in text and run2.state is State.COMPLETED


async def test_cost_tracking_unknown_when_provider_reports_none(tmp_path):
    class NoCost(MockProvider):
        async def generate(self, request):
            r = await super().generate(request)
            return r.model_copy(update={"cost_usd": None})

    run = await engine(tmp_path, registry(claude=NoCost("claude", ["x"]))).run("버튼 텍스트 변경")
    assert run.cost_unknown is True


# ---- CLI ------------------------------------------------------------------
runner = CliRunner()


def test_cli_init_and_analyze(tmp_path):
    r = runner.invoke(app, ["init", "-p", str(tmp_path)])
    assert r.exit_code == 0 and (tmp_path / ".devorch" / "policy.yaml").exists()
    r = runner.invoke(app, ["init", "-p", str(tmp_path)])
    assert "이미 초기화" in r.output

    r = runner.invoke(app, ["analyze", "결제 로직 변경", "-p", str(tmp_path)])
    assert r.exit_code == 0 and "CRITICAL" in r.output and "사람 승인: YES" in r.output

    r = runner.invoke(app, ["analyze", "로그인 JWT 인증 추가", "-p", str(tmp_path), "--json"])
    data = json.loads(r.output)
    assert data["risk"]["level"] == "high" and data["plan"]["requires_human_approval"] is True


def test_cli_analyze_empty_task(tmp_path):
    r = runner.invoke(app, ["analyze", "  ", "-p", str(tmp_path)])
    assert r.exit_code == 2


def test_cli_run_without_provider_keys_fails_before_any_call(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = runner.invoke(app, ["run", "로그인 변경", "-p", str(tmp_path)])
    assert r.exit_code == 2


def test_cli_run_mock_with_approval(tmp_path):
    ok = runner.invoke(app, ["run", "로그인 JWT 인증 추가", "-p", str(tmp_path), "--mock"], input="y\n")
    assert ok.exit_code == 0 and "COMPLETED" in ok.output
    no = runner.invoke(app, ["run", "로그인 JWT 인증 추가", "-p", str(tmp_path), "--mock"], input="n\n")
    assert no.exit_code == 1 and "REJECTED" in no.output
    assert (tmp_path / ".devorch" / "audit.jsonl").exists()
