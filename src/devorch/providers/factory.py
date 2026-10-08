"""Builds a ProviderRegistry from ``.devorch/config.yaml`` and environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from devorch.providers.adapters import AnthropicProvider, GeminiProvider, OpenAIProvider
from devorch.providers.base import LLMProvider, ProviderRegistry

CLASSES: dict[str, type[LLMProvider]] = {
    "claude": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}
# Only Claude gets a built-in default model; others must be set explicitly in config.yaml
# so DevOrch never guesses a model id.
DEFAULT_MODELS = {"claude": "claude-sonnet-5-5"}


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
        if not api_key.isascii() or any(c.isspace() or not c.isprintable() for c in api_key):
            # A key with stray whitespace / invisible or non-ASCII characters makes the HTTP header malformed, which the
            # vendors answer with a bodiless 400. Refuse before any call; never print the key itself.
            skipped[name] = f"환경변수 {key_env} 에 공백·보이지 않는 문자·비ASCII 문자가 섞여 있음 (키를 다시 입력하세요)"
            continue
        model = cfg.get("model") or DEFAULT_MODELS.get(name)
        if not model:
            skipped[name] = "config.yaml의 providers.%s.model 이 설정되지 않음" % name
            continue
        prices = None
        if "input_per_mtok" in cfg and "output_per_mtok" in cfg:
            prices = {"input_per_mtok": float(cfg["input_per_mtok"]), "output_per_mtok": float(cfg["output_per_mtok"])}
        try:
            registry.register(cls(model=model, api_key=api_key, name=name, prices=prices))
        except ImportError as e:
            skipped[name] = f"SDK 미설치: {e.name or e}"
    return registry, skipped
