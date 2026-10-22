"""Spot changes that WEAKEN the tests, from the content of the diff — a deterministic check that needs no model and costs nothing.

Why: an AI agent whose change makes a test fail has a cheap way out — delete the test, skip it, or make its assertion trivially true — and the test step then
"passes". DevOrch's other re-assessments look at file PATHS only (``diff_reassess.py``); this one reads the changed LINES of test files.

Rules (fixed before they were measured on real history; see docs/DOGFOOD_LOG.md):
  deleted_test_file     a test file is deleted
  tests_removed         more test functions removed than added, summed over all test files (moving or renaming a test nets to zero)
  skip_added            more skip/xfail markers added than removed
  trivial_assert_added  an always-true assertion is added (``assert True``, ``expect(true).toBe(true)`` ...)
  asserts_removed       two or more assertions net removed — only when neither of the first two fired (they explain a drop in assertions)
One kind of signal raises the risk to at least MEDIUM (an independent reviewer is added); two or more kinds raise it to HIGH (a human approval). Raise-only.

Limits, honestly: only Python, JavaScript/TypeScript, Java, Go and Rust patterns are recognised; every other language yields no signal. It is a line-pattern
heuristic: it flags legitimate clean-ups (removing tests of a feature that was removed) and misses weakening that keeps the shape (loosening an expected value,
catching the failure in a try/except, deleting the failing case from a parametrize list).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from devorch.models import RiskLevel

_TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|(Test|Tests)\.java$|\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$|_spec\.rb$",
    re.I,
)
_TEST_FN = re.compile(
    r"^\s*(?:async\s+)?def\s+test\w*\s*\(|^\s*(?:it|test)(?:\.(?:only|each))?\s*\(|^\s*@Test\b|^\s*func\s+Test\w*\s*\(|^\s*#\[test\]"
)
_ASSERT = re.compile(
    r"^\s*assert\b|\bself\.assert\w*\s*\(|\bexpect\s*\(|\bassert\w*\s*\(|\bt\.(?:Error|Errorf|Fatal|Fatalf)\s*\(|\bassert(?:_eq|_ne)?!"
)
_SKIP = re.compile(
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail)\s*\(|@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"
    r"|\b(?:it|test|describe)\.(?:skip|todo)\b|\bx(?:it|describe|test)\s*\(|@Disabled\b|@Ignore\b|\bt\.Skip(?:Now|f)?\s*\(|#\[ignore\]"
)
_TRIVIAL = re.compile(
    r"^\s*assert\s+(?:True|1|not\s+False)\s*(?:#.*)?$|\bassertTrue\s*\(\s*True\s*\)|\bexpect\s*\(\s*(?:true|1)\s*\)\s*\.\s*(?:toBe|toEqual)\s*\(\s*(?:true|1)\s*\)"
    r"|\bexpect\s*\(\s*true\s*\)\s*\.\s*toBeTruthy\s*\(\s*\)"
)


@dataclass
class Signal:
    kind: str
    detail: str


@dataclass
class _Counts:
    removed_tests: int = 0
    added_tests: int = 0
    removed_asserts: int = 0
    added_asserts: int = 0
    removed_skips: int = 0
    added_skips: int = 0
    trivial_added: int = 0
    deleted_files: list[str] = field(default_factory=list)


def is_test_file(path: str) -> bool:
    p = str(PurePosixPath(path.replace("\\", "/")))
    return bool(_TEST_FILE.search(p))


def _scan(diff: str) -> _Counts:
    c = _Counts()
    path = ""
    deleted = in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            path = m.group(2) if m else ""
            deleted = in_hunk = False
            continue
        if not in_hunk:
            if line.startswith("deleted file mode"):
                deleted = True
                if is_test_file(path):
                    c.deleted_files.append(path)
            elif line.startswith("@@"):
                in_hunk = True
            continue
        if line.startswith("@@"):
            continue
        if not is_test_file(path) or not line or line[0] not in "+-":
            continue
        sign, body = line[0], line[1:]
        added = sign == "+"
        if _TEST_FN.search(body):
            if added:
                c.added_tests += 1
            else:
                c.removed_tests += 1
        if _ASSERT.search(body):
            if added:
                c.added_asserts += 1
            else:
                c.removed_asserts += 1
        if _SKIP.search(body):
            if added:
                c.added_skips += 1
            else:
                c.removed_skips += 1
        if added and _TRIVIAL.search(body):
            c.trivial_added += 1
    return c


def detect(diff: str) -> list[Signal]:
    """Signals found in ``diff`` (a unified diff). Empty list = nothing recognised (which is not the same as "the tests are fine")."""
    c = _scan(diff or "")
    out: list[Signal] = []
    if c.deleted_files:
        shown = ", ".join(c.deleted_files[:3]) + (f" 외 {len(c.deleted_files) - 3}개" if len(c.deleted_files) > 3 else "")
        out.append(Signal("deleted_test_file", f"테스트 파일 삭제: {shown}"))
    net_tests = c.removed_tests - c.added_tests
    if net_tests > 0:
        out.append(Signal("tests_removed", f"테스트 함수가 순수하게 {net_tests}개 줄어듦(제거 {c.removed_tests}, 추가 {c.added_tests})"))
    net_skips = c.added_skips - c.removed_skips
    if net_skips > 0:
        out.append(Signal("skip_added", f"건너뛰기/xfail 표시가 순수하게 {net_skips}개 늘어남"))
    if c.trivial_added > 0:
        out.append(Signal("trivial_assert_added", f"항상 참인 단언이 {c.trivial_added}개 추가됨"))
    net_asserts = c.removed_asserts - c.added_asserts
    if net_asserts >= 2 and not c.deleted_files and net_tests <= 0:
        out.append(Signal("asserts_removed", f"단언이 순수하게 {net_asserts}개 줄어듦(제거 {c.removed_asserts}, 추가 {c.added_asserts})"))
    return out


def floor_for(signals: list[Signal]) -> RiskLevel | None:
    """MEDIUM for one kind of signal, HIGH for two or more kinds, None for none."""
    kinds = {s.kind for s in signals}
    if not kinds:
        return None
    return RiskLevel.HIGH if len(kinds) >= 2 else RiskLevel.MEDIUM
