"""Re-assess a task against the diff it actually produced — RAISE ONLY.

The first analysis sees only the task *description*, and in the MCP flow that text is written by the agent that is about to make the change.
A change described as "update the documentation wording" that really edits an authentication module used to come out as: type docs, risk
LOW, no tests, no reviewers, no human approval — and was committed (reproduced 2026-10-14). The description can be careless, outdated or
simply wrong, so what decides the gates must not rest on it alone.

Once the diff exists we know which files it touches. Here we (1) flip a "docs" task to a code change when the diff touches anything other than
documentation files, and (2) add the diff's files to the impact (a path that looks security- or database-related raises the risk, exactly as
it does when the task text names such a file). The result can only be higher than before; it is never lowered.

Limit: this looks at file PATHS only. A risky edit inside a neutrally named file is not noticed.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from devorch.core.analyzer import test_weakening
from devorch.core.analyzer.keywords import DATABASE_PATH_HINTS, SECURITY_PATH_HINTS
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.models import ImpactReport, RiskAssessment, RiskLevel, TaskAnalysis, TaskType

DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
DOC_NAMES = {"license", "licence", "notice", "authors", "changelog", "readme", "copying", "contributing"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".mjs"}


def is_doc_file(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    return p.suffix.lower() in DOC_SUFFIXES or (p.suffix == "" and p.name.lower() in DOC_NAMES)


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def path_matches(path: str, pattern: str) -> bool:
    """Owner-declared pattern (see config.ProtectedPath): folder prefix if it ends in '/', file name anywhere if it has no '/', else fnmatch."""
    p, pat = _norm(path), _norm(pattern)
    if pat.endswith("/"):
        return p.startswith(pat)
    if "/" not in pat:
        return fnmatchcase(p.rsplit("/", 1)[-1], pat)
    return fnmatchcase(p, pat)


def protected_floor(files: list[str], protected: Sequence[tuple[str, str]]) -> tuple[RiskLevel | None, list[str]]:
    """Highest ``min_level`` among the owner's protected paths that ``files`` touch, and the files/patterns that caused it."""
    best: RiskLevel | None = None
    hits: list[tuple[RiskLevel, str]] = []
    for pattern, level_name in protected:
        level = RiskLevel(level_name)
        for f in files:
            if path_matches(f, pattern):
                hits.append((level, f"{f} (패턴 {pattern!r})"))
                if best is None or level.rank > best.rank:
                    best = level
    return best, [text for level, text in hits if best is not None and level == best]


def reassess_from_diff(
    analysis: TaskAnalysis, risk: RiskAssessment, impact: ImpactReport, files: list[str],
    protected: Sequence[tuple[str, str]] = (),
    diff: str = "",
    detect_weakened_tests: bool = True,
) -> tuple[TaskAnalysis, RiskAssessment, ImpactReport, list[str]]:
    """(analysis, risk, impact, reasons). ``reasons`` is empty when nothing had to be raised — then the inputs are returned unchanged."""
    reasons: list[str] = []
    non_doc = [f for f in files if not is_doc_file(f)]

    new_analysis = analysis
    if analysis.task_type is TaskType.DOCS and non_doc:
        new_analysis = analysis.model_copy(update={"task_type": TaskType.CHANGE})
        shown = ", ".join(non_doc[:3]) + (f" 외 {len(non_doc) - 3}개" if len(non_doc) > 3 else "")
        reasons.append(f"작업은 문서 변경으로 설명됐지만 diff 가 문서 이외의 파일을 바꿈({shown}) → 코드 변경으로 취급(테스트 필요)")

    lowered = [f.lower().replace("\\", "/") for f in files]
    sec = impact.security_impact or any(h in f for f in lowered for h in SECURITY_PATH_HINTS)
    db = impact.database_impact or any(h in f for f in lowered for h in DATABASE_PATH_HINTS)
    if sec and not impact.security_impact:
        reasons.append("diff 가 보안 민감해 보이는 경로를 바꿈(auth/token/payment 등) → 영향 범위에 보안 포함")
    if db and not impact.database_impact:
        reasons.append("diff 가 DB 관련으로 보이는 경로를 바꿈 → 영향 범위에 DB 포함")
    source = [f for f in files if PurePosixPath(f).suffix.lower() in SOURCE_SUFFIXES]
    direct = sorted(set(impact.direct_files) | set(source))
    new_impact = impact.model_copy(update={"direct_files": direct, "security_impact": sec, "database_impact": db})

    new_risk = assess_risk(new_analysis, new_impact)
    if new_risk.level.rank < risk.level.rank or (new_risk.level.rank == risk.level.rank and new_risk.score <= risk.score):
        new_risk = risk  # raise-only: never lower the level, and keep what we had (it may carry a user-set or area floor)
    elif new_risk.level.rank > risk.level.rank:
        reasons.append(f"diff 기준 재평가로 위험도 {risk.level.value} → {new_risk.level.value}")
        new_risk = new_risk.model_copy(update={"floor_reason": "; ".join(reasons)})
    # same level, higher score: use the new assessment so the score and its factors show what was counted

    floor, hits = protected_floor(files, protected)  # the owner's own declaration beats any guess: raise to at least that level
    if floor is not None and floor.rank > new_risk.level.rank:
        shown = ", ".join(hits[:3]) + (f" 외 {len(hits) - 3}개" if len(hits) > 3 else "")
        reasons.append(f"소유자가 보호 경로로 지정한 파일을 diff 가 바꿈({shown}) → 최소 {floor.value.upper()}")
        new_risk = new_risk.model_copy(update={"level": floor, "floor_reason": "; ".join(reasons)})

    if detect_weakened_tests and diff:  # content, not paths: a change that deletes/skips/trivialises tests (see core/analyzer/test_weakening.py)
        signals = test_weakening.detect(diff)
        weak = test_weakening.floor_for(signals)
        if weak is not None and weak.rank > new_risk.level.rank:
            reasons.append("테스트를 약하게 만드는 변경으로 보임(" + "; ".join(s.detail for s in signals) + f") → 최소 {weak.value.upper()}")
            new_risk = new_risk.model_copy(update={"level": weak, "floor_reason": "; ".join(reasons)})

    if not reasons:
        return analysis, risk, impact, []
    return new_analysis, new_risk, new_impact, reasons
