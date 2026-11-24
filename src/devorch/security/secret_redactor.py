"""Masks likely secrets before data is logged."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                 # OpenAI / Anthropic style keys
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),                # Google API keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),             # GitHub tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS access key id
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),            # GitHub fine-grained tokens
    re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"),          # Slack tokens (bot, user, app, ...)
    re.compile(r"[sr]k_live_[A-Za-z0-9]{16,}"),             # Stripe live secret / restricted keys (test keys are left alone)
]
# Redacted wherever text is shown or logged, but NOT a reason to refuse a diff: these shapes are common in ordinary code and fixtures
# (example JWTs in tests, ``postgres://postgres:postgres@localhost`` in compose files), so blocking on them would stop legitimate work.
_REDACT_ONLY = [
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT (header.payload.signature)
]
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@'\"]+:)[^\s@/'\"]+(@)")  # postgres://user:<this>@host — only the password
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b(\s*[:=]\s*)(['\"]?)[^\s'\",;]+"
)


def has_secret_literal(text: str) -> bool:
    """True if ``text`` contains a literal that looks like a real credential (key formats only,
    not ``token = something`` assignments, which are ordinary code)."""
    return any(p.search(text) for p in _PATTERNS)


def redact(text: str) -> str:
    for pat in _PATTERNS:
        text = pat.sub(REDACTED, text)
    for pat in _REDACT_ONLY:
        text = pat.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + REDACTED + m.group(2), text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", text)


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts / lists."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
