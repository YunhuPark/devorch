"""Builds a ProviderRegistry from ``.devorch/config.yaml`` and environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from devorch.providers.adapters import AnthropicProvider, GeminiProvider, OpenAIProvider
from devorch.providers.base import LLMProvider, ProviderRegistry, valid_price

CLASSES: dict[str, type[LLMProvider]] = {
    "claude": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}
# Only Claude gets a built-in default model; others must be set explicitly in config.yaml
# so DevOrch never guesses a model id.
DEFAULT_MODELS = {"claude": "claude-sonnet-5-5"}


def key_problem(api_key: str) -> str | None:
    """Why this key can't be sent as an HTTP header (None = fine). The text never contains the key."""
    if not api_key.isascii() or any(c.isspace() or not c.isprintable() for c in api_key):
        return "공백·보이지 않는 문자·비ASCII 문자가 섞여 있음"
    return None


def build_registry(
    config: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> tuple[ProviderRegistry, dict[str, str]]:
    """Return (registry, skipped) where skipped maps provider name → reason."""
    env = os.environ if env is None else env
    registry = ProviderRegistry()
    skipped: dict[str, str] = {}

    for name, cls in CLASSES.items():
        cfg = (config.get("providers") or {}).get(name)
        if cfg is None:
            skipped[name] = "config.yaml에 설정 없음"
            continue
        key_env = cfg.get("api_key_env")
        api_key = (env.get(key_env) or "").strip() if key_env else None
        if not api_key:
            skipped[name] = f"환경변수 {key_env or '(api_key_env 미설정)'} 가 비어 있음"
            continue
        if (problem := key_problem(api_key)) is not None:
            # A key with stray whitespace / invisible or non-ASCII characters makes the HTTP header malformed, which the
            # vendors answer with a bodiless 400. Refuse before any call; never print the key itself.
            skipped[name] = f"환경변수 {key_env} 에 {problem} (키를 다시 입력하세요)"
            continue
        model = cfg.get("model") or DEFAULT_MODELS.get(name)
        if not model:
            skipped[name] = "config.yaml의 providers.%s.model 이 설정되지 않음" % name
            continue
        prices = None
        given = [k for k in ("input_per_mtok", "output_per_mtok") if cfg.get(k) is not None]
        if len(given) == 2:
            ip, op = valid_price(cfg["input_per_mtok"]), valid_price(cfg["output_per_mtok"])
            if ip is None or op is None:
                skipped[name] = f"providers.{name} 의 단가가 올바르지 않음(input_per_mtok / output_per_mtok 는 0 이상의 유한한 숫자여야 함)"
                continue
            prices = {"input_per_mtok": ip, "output_per_mtok": op}
        try:
            registry.register(cls(model=model, api_key=api_key, name=name, prices=prices))
        except ImportError as e:
            skipped[name] = f"SDK 미설치: {e.name or e}"
    return registry, skipped
