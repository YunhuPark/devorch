"""Benchmark cases: small code changes with *known* seeded defects (or none, for clean controls).

A case is described by before/after file contents; the unified diff is generated from them so
it is always well-formed (and tests check that it applies to the "before" state with ``git apply``).
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from devorch.core.execution.patch import PatchError, validate_diff

BUILTIN_DIR = Path(__file__).parent / "cases"


class FileChange(BaseModel):
    before: str | None = None  # None → new file
    after: str


class Defect(BaseModel):
    id: str
    category: str  # security | logic | architecture | ...
    severity: str = "high"
    description: str
    # A finding "detects" this defect if its text matches ANY of these (case-insensitive) regexes.
    match_any: list[str]
    file: str | None = None


class Case(BaseModel):
    id: str
    title: str
    task: str
    files: dict[str, FileChange]
    defects: list[Defect] = Field(default_factory=list)
    notes: str = ""

    @property
    def clean(self) -> bool:
        return not self.defects

    @property
    def diff(self) -> str:
        return build_diff(self.files)


def build_diff(files: dict[str, FileChange]) -> str:
    out: list[str] = []
    for path, change in files.items():
        after = change.after.splitlines(keepends=True)
        if change.before is None:
            out.append(f"diff --git a/{path} b/{path}\nnew file mode 100644\n")
            fromfile, before = "/dev/null", []
        else:
            out.append(f"diff --git a/{path} b/{path}\n")
            fromfile, before = f"a/{path}", change.before.splitlines(keepends=True)
        hunk = list(difflib.unified_diff(before, after, fromfile=fromfile, tofile=f"b/{path}", n=3))
        if not hunk:
            raise ValueError(f"{path}: before and after are identical")
        out.append("".join(line if line.endswith("\n") else line + "\n" for line in hunk))
    return "".join(out)


def load_cases(path: Path | None = None) -> list[Case]:
    directory = Path(path) if path else BUILTIN_DIR
    cases = [Case.model_validate(yaml.safe_load(p.read_text(encoding="utf-8"))) for p in sorted(directory.glob("*.yaml"))]
    return cases


def validate_cases(cases: list[Case]) -> list[str]:
    """Return a list of problems (empty = all good)."""
    problems: list[str] = []
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            problems.append(f"{c.id}: 중복된 case id")
        seen.add(c.id)
        try:
            validate_diff(c.diff)
        except (PatchError, ValueError) as e:
            problems.append(f"{c.id}: diff 검증 실패 — {e}")
        dseen: set[str] = set()
        for d in c.defects:
            if d.id in dseen:
                problems.append(f"{c.id}.{d.id}: 중복된 defect id")
            dseen.add(d.id)
            if not d.match_any:
                problems.append(f"{c.id}.{d.id}: match_any 가 비어 있음")
            for rx in d.match_any:
                try:
                    re.compile(rx)
                except re.error as e:
                    problems.append(f"{c.id}.{d.id}: 잘못된 정규식 {rx!r} — {e}")
            if d.file and d.file not in c.files:
                problems.append(f"{c.id}.{d.id}: file {d.file!r} 이(가) 케이스에 없음")
    if not any(c.clean for c in cases):
        problems.append("대조군(결함 없는 케이스)이 하나도 없음 — 오탐률을 측정할 수 없음")
    if not any(not c.clean for c in cases):
        problems.append("결함이 있는 케이스가 하나도 없음")
    return problems
