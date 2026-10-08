"""Vendor adapters (Anthropic / OpenAI / Gemini) behind the ``LLMProvider`` interface.

SDKs are imported lazily so the core engine works without them installed.
A ``client`` can be injected for testing. Costs are only reported when the
caller supplies per-million-token prices; otherwise ``cost_usd`` is ``None``.
"""

from __future__ import annotations

import time
from typing import Any

from devorch.providers.base import LLMProvider, LLMRequest, LLMResponse


class ProviderError(RuntimeError):
    """A provider call that did not produce a complete, usable answer."""


def _cost(prices: dict[str, float] | None, in_tok: int, out_tok: int) -> float | None:
    if not prices or "input_per_mtok" not in prices or "output_per_mtok" not in prices:
        return None
    return round((in_tok * prices["input_per_mtok"] + out_tok * prices["output_per_mtok"]) / 1_000_000, 6)


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        name: str = "claude",
        prices: dict[str, float] | None = None,
        client: Any = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.name, self.model, self.prices = name, model, prices
        if client is None:
            import anthropic  # lazy

            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s)
        self._client = client

    async def generate(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system:
            kwargs["system"] = request.system
        t0 = time.monotonic()
        resp = await self._client.messages.create(**kwargs)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise ProviderError(f"{self.name}: 응답이 max_tokens({request.max_tokens})에서 잘림")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
        if not text:
            raise ProviderError(f"{self.name}: 빈 응답 (stop_reason={getattr(resp, 'stop_reason', None)})")
        in_tok, out_tok = resp.usage.input_tokens, resp.usage.output_tokens
        return LLMResponse(
            text=text, provider=self.name, model=self.model, input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=_cost(self.prices, in_tok, out_tok), latency_s=round(time.monotonic() - t0, 3),
        )


class OpenAIProvider(LLMProvider):
    """Uses the Responses API."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        name: str = "openai",
        prices: dict[str, float] | None = None,
        client: Any = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.name, self.model, self.prices = name, model, prices
        if client is None:
            import openai  # lazy

            client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout_s)
        self._client = client

    async def generate(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": request.prompt,
            "max_output_tokens": max(request.max_tokens, 16),
        }
        if request.system:
            kwargs["instructions"] = request.system
        t0 = time.monotonic()
        resp = await self._client.responses.create(**kwargs)
        if getattr(resp, "status", "completed") == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", "unknown")
            raise ProviderError(f"{self.name}: 응답이 완료되지 않음 ({reason})")
        text = getattr(resp, "output_text", "") or ""
        if not text:
            raise ProviderError(f"{self.name}: 빈 응답 (status={getattr(resp, 'status', None)})")
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        return LLMResponse(
            text=text, provider=self.name, model=self.model, input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=_cost(self.prices, in_tok, out_tok), latency_s=round(time.monotonic() - t0, 3),
        )


class GeminiProvider(LLMProvider):
    """Uses ``models.generate_content``.

    The SDK also ships an Interactions API; this adapter deliberately sticks to
    generate_content, whose response shape is stable and verified, and can be
    swapped later behind the same interface.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        name: str = "gemini",
        prices: dict[str, float] | None = None,
        client: Any = None,
        timeout_s: float = 300.0,
    ) -> None:
        self.name, self.model, self.prices = name, model, prices
        self._timeout_s = timeout_s
        if client is None:
            from google import genai  # lazy
            from google.genai import types

            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        self._client = client

    async def generate(self, request: LLMRequest) -> LLMResponse:
        config: dict[str, Any] = {"max_output_tokens": request.max_tokens}
        if request.system:
            config["system_instruction"] = request.system
        t0 = time.monotonic()
        resp = await self._client.aio.models.generate_content(model=self.model, contents=request.prompt, config=config)

        candidates = getattr(resp, "candidates", None) or []
        finish = str(getattr(candidates[0], "finish_reason", "")) if candidates else ""
        if "MAX_TOKENS" in finish:
            raise ProviderError(f"{self.name}: 응답이 max_tokens({request.max_tokens})에서 잘림")
        text = getattr(resp, "text", None)
        if not text:
            raise ProviderError(f"{self.name}: 빈 응답 또는 차단됨 (finish_reason={finish or 'n/a'})")
        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        return LLMResponse(
            text=text, provider=self.name, model=self.model, input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=_cost(self.prices, in_tok, out_tok), latency_s=round(time.monotonic() - t0, 3),
        )
