"""Provider abstraction. Concrete vendor adapters implement ``LLMProvider``."""

from __future__ import annotations

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
