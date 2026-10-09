"""Token-based cost estimate for reviewer calls: prompt size x configured price + a typical answer length.

Where the numbers come from (be honest about what they are):
- ``CHARS_PER_TOKEN`` and ``OUTPUT_TOKENS`` for openai and gemini were FITTED on a benchmark run (2026-10-08, 11 cases) and
  checked on a different one (2026-10-09, 16 cases, other reviewer mixes): the predicted total cost per mode was within
  +2..+11%. That is an aggregate over many calls on SMALL diffs (~300 input tokens). A single call can be far off
  (measured output per call ranged 36..518 tokens for openai and 241..1533 for gemini, thinking tokens included), and large
  diffs have not been measured at all.
- Anything for another provider (claude) is a flagged ASSUMPTION, not a measurement.
- At analysis time the diff does not exist yet, so its size is an assumption (``assumed_diff_chars``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CHARS_PER_TOKEN: dict[str, float] = {"openai": 3.36, "gemini": 3.01}
OUTPUT_TOKENS: dict[str, int] = {"openai": 317, "gemini": 979}  # per review call, mean; gemini includes thinking tokens
ASSUMED_CHARS_PER_TOKEN = 3.0
ASSUMED_OUTPUT_TOKENS = 500
CONTEXT_OVERHEAD_CHARS = 300  # task/risk/file-list header and section labels around the diff in a review prompt
DEFAULT_DIFF_CHARS_PER_FILE = 1500.0  # assumption: used only before a real diff exists

Prices = Mapping[str, Mapping[str, float]]


@dataclass(frozen=True)
class CallEstimate:
    usd: float | None  # None = no (complete) price configured: unknown, never 0
    input_tokens: int
    output_tokens: int
    measured: bool  # coefficients were fitted on benchmark data (False = flagged assumption)


def estimate_call(provider: str, prompt_chars: int, prices: Prices | None) -> CallEstimate:
    measured = provider in CHARS_PER_TOKEN
    in_tok = round(prompt_chars / CHARS_PER_TOKEN.get(provider, ASSUMED_CHARS_PER_TOKEN))
    out_tok = OUTPUT_TOKENS.get(provider, ASSUMED_OUTPUT_TOKENS)
    p = (prices or {}).get(provider) or {}
    if "input_per_mtok" not in p or "output_per_mtok" not in p:
        return CallEstimate(None, in_tok, out_tok, measured)
    return CallEstimate((in_tok * p["input_per_mtok"] + out_tok * p["output_per_mtok"]) / 1_000_000, in_tok, out_tok, measured)


def assumed_prompt_chars(task_text: str, n_direct_files: int, chars_per_file: float = DEFAULT_DIFF_CHARS_PER_FILE) -> int:
    """Review prompt size BEFORE the diff exists: system prompt + header + an assumed diff size."""
    from devorch.core.orchestrator.review import REVIEWER_SYSTEM  # local: avoids an import cycle with the engine

    return round(len(REVIEWER_SYSTEM) + len(task_text) + CONTEXT_OVERHEAD_CHARS + max(1, n_direct_files) * chars_per_file)


def _complete(p: Mapping[str, Any] | None) -> dict[str, float] | None:
    if not p or "input_per_mtok" not in p or "output_per_mtok" not in p:
        return None
    try:
        return {"input_per_mtok": float(p["input_per_mtok"]), "output_per_mtok": float(p["output_per_mtok"])}
    except (TypeError, ValueError):
        return None


def prices_from_config(config: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """Per-provider prices from ``config.yaml`` — only providers with BOTH prices set."""
    out: dict[str, dict[str, float]] = {}
    for name, cfg in ((config or {}).get("providers") or {}).items():
        if (p := _complete(cfg)) is not None:
            out[name] = p
    return out


def prices_from_registry(registry) -> dict[str, dict[str, float]]:
    """Prices of the registered adapters (providers without a ``prices`` attribute, e.g. mocks, are skipped)."""
    out: dict[str, dict[str, float]] = {}
    for name in registry.names():
        if (p := _complete(getattr(registry.get(name), "prices", None))) is not None:
            out[name] = p
    return out


def merge_prices(*sources: Prices | None) -> dict[str, dict[str, float]]:
    """Later sources override earlier ones, per provider."""
    out: dict[str, dict[str, float]] = {}
    for s in sources:
        for name, p in (s or {}).items():
            out[name] = dict(p)
    return out
