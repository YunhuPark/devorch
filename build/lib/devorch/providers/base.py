"""Provider abstraction. Concrete vendor adapters implement ``LLMProvider``."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterable

from pydantic import BaseModel, Field


class LLMRequest(BaseModel):
    role: str
    prompt: str
    system: str = ""
    max_tokens: int = 4096
    metadata: dict[str, str] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    text: str
    provider: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None  # None = unknown
    latency_s: float = 0.0


def valid_price(value) -> float | None:
    """A per-million-token price as a float, or None if it isn't a finite number >= 0 (NaN, inf, negatives, junk).
    A NaN/negative price would silently corrupt every cost and defeat the spend cap."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


class ProviderError(RuntimeError):
    """A provider call that did not produce a complete, usable answer.

    When the API DID answer (e.g. the reply was cut at max_tokens, or came back empty), the tokens it reports were still
    generated and billed: ``input_tokens`` / ``output_tokens`` / ``cost_usd`` carry that spend (``cost_usd`` None = no price
    configured, unknown — never 0) so the cost accounting and the spend cap can see it. ``truncated`` marks a max_tokens cut.
    """

    def __init__(self, message: str, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float | None = None, truncated: bool = False) -> None:
        super().__init__(message)
        self.input_tokens, self.output_tokens, self.cost_usd, self.truncated = input_tokens, output_tokens, cost_usd, truncated


class LLMProvider(ABC):
    name: str

    @property
    def priced(self) -> bool:
        """True if ``generate`` reports a cost, i.e. a spend cap can be enforced for this provider."""
        return True

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Run one request. Must raise on failure (never return partial text silently)."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}

    def register(self, provider: LLMProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> LLMProvider:
        try:
            return self._providers[name]
        except KeyError:
            known = ", ".join(sorted(self._providers)) or "(none)"
            raise KeyError(f"provider '{name}' is not registered (registered: {known})") from None

    def names(self) -> list[str]:
        return sorted(self._providers)

    def unpriced(self, names: Iterable[str]) -> list[str]:
        """Registered providers among ``names`` whose calls would have unknown cost."""
        return sorted({n for n in names if n in self._providers and not self._providers[n].priced})
