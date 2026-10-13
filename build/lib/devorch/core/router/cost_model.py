"""Token-based cost estimate for reviewer calls: prompt size x configured price + an output-token RANGE.

What the numbers are (be honest about it):

INPUT tokens: ASCII characters / ``ASCII_CHARS_PER_TOKEN`` + non-ASCII characters x ``NONASCII_TOKENS_PER_CHAR`` (Hangul is
about one token per character for both OpenAI and Gemini tokenizers). Checked on 16 benchmark prompts the coefficients were NOT
fitted on (per-case -5..+10%, total +2..+3%) and on 4 real 6-36KB diffs including a Hangul-heavy one (within +-5%; the
v1 single-rate formula was off by -35% on that one).

OUTPUT tokens (what the model writes, thinking included) are the uncertain part: they grow with the prompt and vary a lot
(OpenAI 27..2702, Gemini 249..5770 on real diffs). So the estimate is a range (low / mid / high), each an affine function of the
input size.

History of the output model (so it can't be mistaken for a validated one):
- v2 (2026-10-11 morning): set from 9 observations (benchmark mean + 4 real reviews). Then tested on 7 NEW reviews (14 calls):
  inputs within -2..+6% on all 14; outputs inside the range on 12/14 (two heavy-finding reviews above the high bound); total cost
  inside the range on 6/7 reviews; mid bias +8% (openai) and -17% (gemini).
- v3 (now): high bounds widened and the gemini mid slope raised using those 7 reviews. The model is therefore IN-SAMPLE again on 11
  real reviews + the benchmark: it documents coverage, it is NOT a validation. Test it on fresh reviews before trusting it, add
  every new measurement to tests/test_cost_model.py, and widen (never narrow) on a miss.

Not covered: the implementation step (never measured), claude (flagged assumption), diffs larger than ~12k tokens, other models.
At analysis time the diff does not exist yet, so its size is an assumption (``assumed_prompt_counts``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from devorch.providers.base import valid_price

ASCII_CHARS_PER_TOKEN: dict[str, float] = {"openai": 3.5, "gemini": 3.0}
NONASCII_TOKENS_PER_CHAR = 0.95
ASSUMED_ASCII_CHARS_PER_TOKEN = 3.0  # providers never measured (claude)

# output tokens = base + slope * input_tokens, for (low, mid, high); low is a flat floor (an answer of "[]" plus a little thinking)
OUTPUT_MODEL: dict[str, dict[str, tuple[float, float]]] = {
    "openai": {"low": (25, 0.0), "mid": (300, 0.10), "high": (1200, 0.25)},
    "gemini": {"low": (200, 0.0), "mid": (900, 0.35), "high": (2500, 0.50)},
}
ASSUMED_OUTPUT_MODEL = {"low": (25, 0.0), "mid": (500, 0.10), "high": (1500, 0.30)}  # unmeasured providers
CONTEXT_OVERHEAD_CHARS = 300  # task/risk/file-list header and section labels around the diff in a review prompt
DEFAULT_DIFF_CHARS_PER_FILE = 1500.0  # assumption, used only before a real diff exists

Prices = Mapping[str, Mapping[str, float]]


@dataclass(frozen=True)
class CallEstimate:
    usd: float | None  # the middle estimate; None = no (complete) price configured: unknown, never 0
    usd_low: float | None
    usd_high: float | None
    input_tokens: int
    output_tokens: int  # middle
    output_low: int
    output_high: int
    measured: bool  # the coefficients were checked against benchmark/real data (False = flagged assumption)


def count_chars(text: str) -> tuple[int, int]:
    """(ASCII characters, everything else)."""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars, len(text) - ascii_chars


def estimate_counts(
    provider: str, ascii_chars: int, nonascii_chars: int, prices: Prices | None, max_output_tokens: int | None = None
) -> CallEstimate:
    """Estimate one call from the character counts of its prompt. ``max_output_tokens`` (the configured output budget) caps the range."""
    measured = provider in ASCII_CHARS_PER_TOKEN
    in_tok = round(ascii_chars / ASCII_CHARS_PER_TOKEN.get(provider, ASSUMED_ASCII_CHARS_PER_TOKEN) + nonascii_chars * NONASCII_TOKENS_PER_CHAR)
    model = OUTPUT_MODEL.get(provider, ASSUMED_OUTPUT_MODEL)

    def out(which: str) -> int:
        base, slope = model[which]
        tokens = round(base + slope * in_tok)
        return min(tokens, max_output_tokens) if max_output_tokens else tokens

    lo, mid, hi = out("low"), out("mid"), out("high")
    p = (prices or {}).get(provider) or {}
    if "input_per_mtok" not in p or "output_per_mtok" not in p:
        return CallEstimate(None, None, None, in_tok, mid, lo, hi, measured)

    def usd(out_tokens: int) -> float:
        return (in_tok * p["input_per_mtok"] + out_tokens * p["output_per_mtok"]) / 1_000_000

    return CallEstimate(usd(mid), usd(lo), usd(hi), in_tok, mid, lo, hi, measured)


# The implementation step writes a diff. TWO real observations so far (2026-10-12, claude-sonnet-5-5): a 2-line fix 265 in / 182 out (the range
# was set after seeing it), then a hold-out "add divide() + test" 273 in / 1155 out — inside the range but ~3x the midpoint. So the output
# range is still an ASSUMPTION spanning "tiny fix" to "multi-file change" (use the range, not the midpoint); input is the real file contents.
IMPLEMENTATION_OUTPUT_TOKENS = {"low": 100, "mid": 400, "high": 4000}


def estimate_implementation(
    provider: str, ascii_chars: int, nonascii_chars: int, prices: Prices | None, max_output_tokens: int | None = None
) -> CallEstimate:
    """One implementation call: input from the real prompt (system + context + file contents), output from a flagged assumed range."""
    in_tok = round(ascii_chars / ASCII_CHARS_PER_TOKEN.get(provider, ASSUMED_ASCII_CHARS_PER_TOKEN) + nonascii_chars * NONASCII_TOKENS_PER_CHAR)

    def out(which: str) -> int:
        tokens = IMPLEMENTATION_OUTPUT_TOKENS[which]
        return min(tokens, max_output_tokens) if max_output_tokens else tokens

    lo, mid, hi = out("low"), out("mid"), out("high")
    p = (prices or {}).get(provider) or {}
    if "input_per_mtok" not in p or "output_per_mtok" not in p:
        return CallEstimate(None, None, None, in_tok, mid, lo, hi, False)

    def usd(out_tokens: int) -> float:
        return (in_tok * p["input_per_mtok"] + out_tokens * p["output_per_mtok"]) / 1_000_000

    return CallEstimate(usd(mid), usd(lo), usd(hi), in_tok, mid, lo, hi, False)


def estimate_call(provider: str, prompt: str, prices: Prices | None, max_output_tokens: int | None = None) -> CallEstimate:
    """Estimate one call from its prompt text (system prompt + user prompt)."""
    a, n = count_chars(prompt)
    return estimate_counts(provider, a, n, prices, max_output_tokens)


def assumed_prompt_counts(task_text: str, n_direct_files: int, chars_per_file: float = DEFAULT_DIFF_CHARS_PER_FILE) -> tuple[int, int]:
    """Review prompt size BEFORE the diff exists: system prompt + header + task text + an assumed (ASCII) diff size."""
    from devorch.core.orchestrator.review import REVIEWER_SYSTEM  # local: avoids an import cycle with the engine

    t_ascii, t_non = count_chars(task_text)
    diff_chars = max(1, n_direct_files) * chars_per_file
    return round(len(REVIEWER_SYSTEM) + CONTEXT_OVERHEAD_CHARS + t_ascii + diff_chars), t_non


def _complete(p: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Both prices, each a finite number >= 0 — otherwise None (unknown). NaN/negative prices never reach an estimate."""
    if not p:
        return None
    ip, op = valid_price(p.get("input_per_mtok")), valid_price(p.get("output_per_mtok"))
    return None if ip is None or op is None else {"input_per_mtok": ip, "output_per_mtok": op}


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
