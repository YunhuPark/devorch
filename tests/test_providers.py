from types import SimpleNamespace as NS

import pytest
from typer.testing import CliRunner

from devorch.cli.main import app
from devorch.providers.adapters import AnthropicProvider, GeminiProvider, OpenAIProvider, ProviderError
from devorch.providers.base import LLMRequest
from devorch.providers.factory import build_registry

REQ = LLMRequest(role="security", prompt="review this", system="be strict", max_tokens=500)
PRICES = {"input_per_mtok": 3.0, "output_per_mtok": 15.0}


class Recorder:
    def __init__(self, result):
        self.result, self.kwargs = result, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.result

    async def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return self.result


# ---- Anthropic -------------------------------------------------------------
async def test_anthropic_success_and_request_shape():
    result = NS(content=[NS(type="text", text="[]")], stop_reason="end_turn", usage=NS(input_tokens=1000, output_tokens=200))
    rec = Recorder(result)
    p = AnthropicProvider("m-1", client=NS(messages=rec), prices=PRICES)
    resp = await p.generate(REQ)
    assert resp.text == "[]" and resp.provider == "claude" and resp.model == "m-1"
    assert (resp.input_tokens, resp.output_tokens) == (1000, 200)
    assert resp.cost_usd == pytest.approx(0.003 + 0.003)
    assert rec.kwargs["system"] == "be strict" and rec.kwargs["max_tokens"] == 500
    assert rec.kwargs["messages"] == [{"role": "user", "content": "review this"}]


async def test_anthropic_omits_empty_system_and_cost_unknown_without_prices():
    rec = Recorder(NS(content=[NS(type="text", text="x")], stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1)))
    resp = await AnthropicProvider("m", client=NS(messages=rec)).generate(LLMRequest(role="r", prompt="p"))
    assert "system" not in rec.kwargs and resp.cost_usd is None


async def test_anthropic_truncated_or_empty_raises():
    trunc = Recorder(NS(content=[NS(type="text", text="{\"a\"")], stop_reason="max_tokens", usage=NS(input_tokens=1, output_tokens=1)))
    with pytest.raises(ProviderError, match="잘림"):
        await AnthropicProvider("m", client=NS(messages=trunc)).generate(REQ)
    empty = Recorder(NS(content=[NS(type="tool_use")], stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=0)))
    with pytest.raises(ProviderError, match="빈 응답"):
        await AnthropicProvider("m", client=NS(messages=empty)).generate(REQ)


# ---- OpenAI ----------------------------------------------------------------
async def test_openai_success_and_request_shape():
    result = NS(status="completed", output_text="[]", usage=NS(input_tokens=2000, output_tokens=100))
    rec = Recorder(result)
    p = OpenAIProvider("m-2", client=NS(responses=rec), prices=PRICES)
    resp = await p.generate(REQ)
    assert resp.text == "[]" and resp.provider == "openai"
    assert resp.cost_usd == pytest.approx(0.006 + 0.0015)
    assert rec.kwargs == {"model": "m-2", "input": "review this", "max_output_tokens": 500, "instructions": "be strict"}


async def test_openai_incomplete_and_empty_raise():
    inc = Recorder(NS(status="incomplete", incomplete_details=NS(reason="max_output_tokens"), output_text="par", usage=None))
    with pytest.raises(ProviderError, match="max_output_tokens"):
        await OpenAIProvider("m", client=NS(responses=inc)).generate(REQ)
    empty = Recorder(NS(status="completed", output_text="", usage=None))
    with pytest.raises(ProviderError, match="빈 응답"):
        await OpenAIProvider("m", client=NS(responses=empty)).generate(REQ)


# ---- Gemini ----------------------------------------------------------------
def gem(result):
    return NS(aio=NS(models=Recorder(result)))


async def test_gemini_success_and_request_shape():
    result = NS(text="[]", candidates=[NS(finish_reason="STOP")], usage_metadata=NS(prompt_token_count=500, candidates_token_count=50))
    client = gem(result)
    resp = await GeminiProvider("m-3", client=client, prices=PRICES).generate(REQ)
    assert resp.text == "[]" and resp.provider == "gemini" and resp.input_tokens == 500
    kw = client.aio.models.kwargs
    assert kw["model"] == "m-3" and kw["contents"] == "review this"
    assert kw["config"] == {"max_output_tokens": 500, "system_instruction": "be strict"}


async def test_gemini_truncated_blocked_raise():
    trunc = gem(NS(text="par", candidates=[NS(finish_reason="FinishReason.MAX_TOKENS")], usage_metadata=None))
    with pytest.raises(ProviderError, match="잘림"):
        await GeminiProvider("m", client=trunc).generate(REQ)
    blocked = gem(NS(text=None, candidates=[NS(finish_reason="SAFETY")], usage_metadata=None))
    with pytest.raises(ProviderError, match="차단"):
        await GeminiProvider("m", client=blocked).generate(REQ)


# ---- factory ---------------------------------------------------------------
CONFIG = {
    "providers": {
        "claude": {"api_key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5"},
        "openai": {"api_key_env": "OPENAI_API_KEY", "model": None},
        "gemini": {"api_key_env": "GEMINI_API_KEY", "model": "g-model", "input_per_mtok": 1, "output_per_mtok": 2},
    }
}


def test_factory_registers_only_usable_providers_and_explains_the_rest():
    reg, skipped = build_registry(CONFIG, env={"ANTHROPIC_API_KEY": "k1", "OPENAI_API_KEY": "k2"})
    assert reg.names() == ["claude"]
    assert "model" in skipped["openai"]                 # key present but no model → never guess one
    assert "GEMINI_API_KEY" in skipped["gemini"]         # model present but no key


def test_factory_prices_and_model_default():
    reg, _ = build_registry(CONFIG, env={"GEMINI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"})
    assert reg.get("gemini").prices == {"input_per_mtok": 1.0, "output_per_mtok": 2.0}
    cfg = {"providers": {"claude": {"api_key_env": "ANTHROPIC_API_KEY"}}}
    reg, _ = build_registry(cfg, env={"ANTHROPIC_API_KEY": "k"})
    assert reg.get("claude").model == "claude-sonnet-5-5"


def test_factory_missing_config_section():
    reg, skipped = build_registry({"providers": {}}, env={})
    assert reg.names() == [] and set(skipped) == {"claude", "openai", "gemini"}


# ---- CLI -------------------------------------------------------------------
def test_cli_providers_and_real_run_without_keys(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    runner = CliRunner()
    r = runner.invoke(app, ["providers", "-p", str(tmp_path)])
    assert r.exit_code == 0 and "사용불가  claude" in r.output and "ANTHROPIC_API_KEY" in r.output

    r = runner.invoke(app, ["run", "로그인 JWT 인증 추가", "-p", str(tmp_path)])
    assert r.exit_code == 2 and "사용할 수 없습니다" in r.output  # fails before any API call


def test_cli_real_run_asks_before_spending(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    r = CliRunner().invoke(app, ["run", "버튼 텍스트 변경", "-p", str(tmp_path)], input="n\n")
    assert r.exit_code == 1 and "진행할까요?" in r.output and "실제 LLM API를 호출" in r.output
