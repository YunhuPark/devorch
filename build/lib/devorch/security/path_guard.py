"""Path traversal guard."""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    pass


def resolve_inside(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``; raise if it escapes (``..``, absolute paths, symlinks)."""
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise PathEscapeError(f"경로가 프로젝트 루트를 벗어남: {relative}")
    return target
