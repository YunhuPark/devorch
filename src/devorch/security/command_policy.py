"""Shell command allowlist. AI-generated commands are never run unless they pass this check."""

from __future__ import annotations

import os
import re
import shlex

_FORBIDDEN_CHARS = set(";&|`$<>\n\r(){}")
_BATCH_META = set('%^!"')  # cmd.exe metacharacters not already in _FORBIDDEN_CHARS
_FORBIDDEN_BINARIES = {"rm", "curl", "wget", "chmod", "chown", "sudo", "su", "dd", "mkfs", "ssh", "scp", "nc", "eval"}
_WINDOWS_ABSOLUTE = re.compile(r"^([A-Za-z]:|\\)")  # C:\x, C:x, \x, \\server\share


def split_command(command: str, posix: bool | None = None) -> list[str]:
    """Split a command string into argv without a shell.

    ``posix=None`` follows the host: POSIX rules elsewhere, and on Windows non-POSIX rules
    (POSIX mode would eat the backslashes of ``C:\\...\\python.exe``). Pass ``posix=True`` when the
    command will run inside a Linux container. Raises ValueError on unbalanced quotes.
    """
    if posix is None:
        posix = os.name != "nt"
    tokens = shlex.split(command, posix=posix)
    if not posix:  # non-POSIX mode keeps the quotes in the token
        tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]
    return tokens


def check_command(command: str, allowed: list[str], posix: bool | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). ``posix`` as in :func:`split_command` — use the same value that will execute it."""
    if not command.strip():
        return False, "빈 명령"
    if any(c in _FORBIDDEN_CHARS for c in command):
        return False, "셸 메타문자(; & | ` $ < > 등)는 허용되지 않음"
    try:
        tokens = split_command(command, posix)
    except ValueError as e:
        return False, f"파싱 실패: {e}"
    if not tokens:
        return False, "빈 명령"
    if (os.name == "nt") if posix is None else not posix:  # Windows-style tokenization = a Windows command
        # Windows: npm/mvn/gradle are .cmd/.bat launchers, which run through cmd.exe — it still expands %VAR%, and ^ ! " can
        # break out of argument quoting. Reject them instead of trying to escape (a command with such an argument is not a test run).
        bad = next((t for t in tokens if any(c in _BATCH_META for c in t)), None)
        if bad is not None:
            return False, f"Windows 배치 파일 실행에서 위험한 문자(% ^ ! \")가 인자에 있음: {bad}"
    if tokens[0] in _FORBIDDEN_BINARIES:
        return False, f"금지된 실행 파일: {tokens[0]}"

    for entry in allowed:
        prefix = split_command(entry, posix)
        if tokens[: len(prefix)] == prefix:
            for arg in tokens[len(prefix):]:
                if arg.startswith("/") or _WINDOWS_ABSOLUTE.match(arg) or ".." in arg.replace("\\", "/").split("/"):
                    return False, f"절대경로/상위경로 인자는 허용되지 않음: {arg}"
            return True, f"allowlist 일치: {entry}"
    return False, "allowlist에 없는 명령"
