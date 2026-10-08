"""Deterministic provider for tests and offline demos."""

from __future__ import annotations

from collections.abc import Callable

from devorch.providers.base import LLMProvider, LLMRequest, LLMResponse

Responder = Callable[[LLMRequest], str]


class MockProvider(LLMProvider):
    """Returns scripted responses.

    ``responses`` is either a list (consumed in order, last one repeats) or a
    callable ``request -> text``. Every request is recorded in ``calls``.
    """

    def __init__(self, name: str, responses: list[str] | Responder | None = None) -> None:
        self.name = name
        self._responses = responses if responses is not None else ["ok"]
        self.calls: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if callable(self._responses):
            text = self._responses(request)
        else:
            idx = min(len(self.calls) - 1, len(self._responses) - 1)
            text = self._responses[idx]
        return LLMResponse(text=text, provider=self.name, model="mock", cost_usd=0.0)
