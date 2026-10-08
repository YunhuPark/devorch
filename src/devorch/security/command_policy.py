"""Shell command allowlist. AI-generated commands are never run unless they pass this check."""

from __future__ import annotations

import shlex

_FORBIDDEN_CHARS = set(";&|`$<>\n\r(){}")
_FORBIDDEN_BINARIES = {"rm", "curl", "wget", "chmod", "chown", "sudo", "su", "dd", "mkfs", "ssh", "scp", "nc", "eval"}


def check_command(command: str, allowed: list[str]) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    if not command.strip():
        return False, "빈 명령"
    if any(c in _FORBIDDEN_CHARS for c in command):
        return False, "셸 메타문자(; & | ` $ < > 등)는 허용되지 않음"
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return False, f"파싱 실패: {e}"
    if tokens[0] in _FORBIDDEN_BINARIES:
        return False, f"금지된 실행 파일: {tokens[0]}"

    for entry in allowed:
        prefix = shlex.split(entry)
        if tokens[: len(prefix)] == prefix:
            for arg in tokens[len(prefix):]:
                if arg.startswith("/") or ".." in arg.replace("\\", "/").split("/"):
                    return False, f"절대경로/상위경로 인자는 허용되지 않음: {arg}"
            return True, f"allowlist 일치: {entry}"
    return False, "allowlist에 없는 명령"
