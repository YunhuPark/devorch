"""Heuristic mock reviewers for the benchmark. HARNESS CHECK ONLY.

These are regex rules written by us against the benchmark's own cases. Their "scores" say
nothing about real models — only that the harness runs end to end.
"""

from __future__ import annotations

import json
import re

from devorch.providers.base import LLMRequest
from devorch.providers.mock import MockProvider

_RULES: dict[str, list[tuple[str, str, str, str]]] = {
    # role -> [(added-line regex, severity, title, detail)]
    "review": [
        (r"verify_signature.*False", "high", "JWT signature verification disabled", "verify_signature=False accepts forged tokens"),
        (r"hashlib\.md5", "high", "Weak hash md5", "md5 is unsuitable for passwords; use bcrypt"),
        (r"page \* size", "medium", "Possible off-by-one", "page is 1-based so the first page is skipped"),
    ],
    "security": [
        (r"""\+ email \+|LIKE '%\{""", "critical", "SQL injection", "user input concatenated into SQL; use parameterized queries"),
        (r"verify_signature.*False", "critical", "Signature verification disabled", "forged tokens accepted"),
        (r"hashlib\.md5", "high", "Weak password hash (md5)", "use bcrypt/argon2 with salt"),
        (r"os\.path\.join\(UPLOAD_DIR, name\)", "high", "Path traversal", "../ in name escapes the upload directory"),
    ],
    "architecture": [
        (r"sqlite3\.connect", "medium", "Layering violation", "business logic and SQL inside a view; move to the service layer"),
    ],
}


def _added_lines(prompt: str) -> str:
    return "\n".join(l[1:] for l in prompt.splitlines() if l.startswith("+") and not l.startswith("+++"))


def heuristic_responder(request: LLMRequest) -> str:
    added = _added_lines(request.prompt)
    out = []
    for rx, sev, title, detail in _RULES.get(request.role, []):
        if re.search(rx, added):
            out.append({"severity": sev, "category": request.role, "title": title, "detail": detail})
    return json.dumps(out)


def heuristic_provider(name: str) -> MockProvider:
    return MockProvider(name, heuristic_responder)
