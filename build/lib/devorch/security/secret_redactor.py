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
]
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
