"""Provider abstraction. Concrete vendor adapters implement ``LLMProvider``."""

from __future__ import annotations

from abc import ABC, abstractmethod

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
