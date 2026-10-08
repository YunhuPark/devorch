"""Extract a unified diff from model output and validate it before it touches a worktree."""

from __future__ import annotations

import re

MAX_DIFF_BYTES = 1_000_000
FORBIDDEN_PREFIXES = (".git/", ".devorch/")

_FENCE = re.compile(r"```(?:diff|patch)[^\n]*\n(.*?)```", re.S)


class PatchError(ValueError):
    pass


def extract_diff(text: str) -> str | None:
    """Return the unified diff in ``text`` (fenced ```diff block or raw), or None."""
    for m in _FENCE.finditer(text):
        body = m.group(1)
        if "\n+++ " in "\n" + body or body.startswith("diff --git"):
            return body if body.endswith("\n") else body + "\n"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("diff --git ") or (line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")):
            return "\n".join(lines[i:]).rstrip("\n") + "\n"
    return None


def _clean(path: str) -> str:
    path = path.split("\t")[0].strip().strip('"')
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def diff_paths(diff: str) -> list[str]:
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line[len("diff --git "):].split(" ")
            for p in parts:
                paths.append(_clean(p))
        elif line.startswith(("--- ", "+++ ")):
            p = line[4:]
            if p.strip() != "/dev/null":
                paths.append(_clean(p))
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            paths.append(_clean(line.split(" ", 2)[2]))
    seen: set[str] = set()
    return [p for p in paths if p and not (p in seen or seen.add(p))]


def validate_diff(diff: str) -> list[str]:
    """Return the touched paths, or raise PatchError if the diff is unsafe."""
    if not diff.strip():
        raise PatchError("빈 diff")
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        raise PatchError("diff가 너무 큼")
    if "GIT binary patch" in diff or re.search(r"^Binary files .* differ$", diff, re.M):
        raise PatchError("바이너리 변경은 허용되지 않음")
    if re.search(r"^(new|old|deleted)? ?file mode 120000", diff, re.M) or re.search(r"^new mode 120000", diff, re.M):
        raise PatchError("심볼릭 링크 변경은 허용되지 않음")

    paths = diff_paths(diff)
    if not paths:
        raise PatchError("diff에서 파일 경로를 찾지 못함")
    for p in paths:
        norm = p.replace("\\", "/")
        if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in norm.split("/"):
            raise PatchError(f"허용되지 않는 경로: {p}")
        if norm.startswith(FORBIDDEN_PREFIXES) or norm in (".git", ".devorch"):
            raise PatchError(f"보호된 경로 변경 시도: {p}")
    return paths
