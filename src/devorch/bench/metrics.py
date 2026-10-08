"""Scoring and statistics for the benchmark. Pure functions — no I/O, no LLM calls."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from devorch.bench.cases import Case, Defect
from devorch.models import Finding, Severity

FP_MIN_SEVERITY = Severity.MEDIUM  # a clean case is a false positive if any finding is >= this


def finding_text(f: Finding) -> str:
    return " ".join(x for x in (f.category, f.title, f.detail, f.file or "") if x)


def matches_defect(f: Finding, d: Defect) -> bool:
    text = finding_text(f)
    return any(re.search(rx, text, re.IGNORECASE) for rx in d.match_any)


def trap_alarmed(case: Case, findings: list[Finding]) -> bool:
    """A finding of >= MEDIUM whose text matches one of the control case's declared false-alarm patterns."""
    return any(
        f.severity.rank >= FP_MIN_SEVERITY.rank and any(re.search(rx, finding_text(f), re.IGNORECASE) for t in case.traps for rx in t.match_any)
        for f in findings
    )


def judge(case: Case, findings: list[Finding]) -> tuple[set[str], int]:
    """Return (detected defect ids, number of findings matching no seeded defect)."""
    detected: set[str] = set()
    unmatched = 0
    for f in findings:
        hit = [d.id for d in case.defects if matches_defect(f, d)]
        detected.update(hit)
        if not hit and f.severity is not Severity.INFO:
            unmatched += 1
    return detected, unmatched


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion. n == 0 → (0, 1) (no information)."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def sign_test(only_a: int, only_b: int) -> float:
    """Two-sided exact binomial (p=0.5) on discordant pairs. No discordant pairs → 1.0."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


@dataclass
class RunResult:
    case_id: str
    mode: str
    repeat: int
    completed: bool  # every reviewer returned a parsable answer
    findings: list[Finding] = field(default_factory=list)
    detected: set[str] = field(default_factory=set)
    unmatched: int = 0
    false_positive: bool = False  # strict: ANY finding >= MEDIUM on a clean control
    trap_alarm: bool = False  # narrow: a >= MEDIUM finding that matches the case's declared trap (cases with traps only)
    cost_usd: float = 0.0
    cost_unknown: bool = False
    latency_s: float = 0.0
    notes: list[str] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)  # provider -> model id that answered
    usage: dict[str, dict[str, int]] = field(default_factory=dict)  # provider -> calls / input_tokens / output_tokens

    @property
    def input_tokens(self) -> int:
        return sum(u.get("input_tokens", 0) for u in self.usage.values())

    @property
    def output_tokens(self) -> int:
        return sum(u.get("output_tokens", 0) for u in self.usage.values())

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "mode": self.mode,
            "repeat": self.repeat,
            "completed": self.completed,
            "detected": sorted(self.detected),
            "unmatched": self.unmatched,
            "false_positive": self.false_positive,
            "trap_alarm": self.trap_alarm,
            "cost_usd": self.cost_usd,
            "cost_unknown": self.cost_unknown,
            "latency_s": round(self.latency_s, 3),
            "models": self.models,
            "usage": self.usage,
            "notes": self.notes,
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }


def score_run(case: Case, mode: str, repeat: int, findings: list[Finding], completed: bool, **kw) -> RunResult:
    # An incomplete review detects nothing (fail-closed), whatever partial findings exist.
    detected, unmatched = judge(case, findings) if completed else (set(), 0)
    fp = bool(case.clean and completed and any(f.severity.rank >= FP_MIN_SEVERITY.rank for f in findings))
    trap = bool(case.clean and case.traps and completed and trap_alarmed(case, findings))
    return RunResult(case.id, mode, repeat, completed, findings, detected, unmatched, fp, trap, **kw)


@dataclass
class ModeSummary:
    mode: str
    defect_runs: int  # (defect × repeat) instances
    detected: int
    clean_runs_completed: int
    false_positives: int
    runs: int
    incomplete: int
    unmatched: int
    cost_usd: float
    cost_unknown: bool
    mean_latency_s: float
    by_category: dict[str, tuple[int, int]]  # category -> (detected, total)
    input_tokens: int = 0
    output_tokens: int = 0
    trap_runs_completed: int = 0  # completed runs of clean cases that declare a trap
    trap_alarms: int = 0

    @property
    def trap_rate(self) -> float:
        return self.trap_alarms / self.trap_runs_completed if self.trap_runs_completed else 0.0

    @property
    def trap_ci(self) -> tuple[float, float]:
        return wilson(self.trap_alarms, self.trap_runs_completed)

    @property
    def detection_rate(self) -> float:
        return self.detected / self.defect_runs if self.defect_runs else 0.0

    @property
    def detection_ci(self) -> tuple[float, float]:
        return wilson(self.detected, self.defect_runs)

    @property
    def fp_rate(self) -> float:
        return self.false_positives / self.clean_runs_completed if self.clean_runs_completed else 0.0

    @property
    def fp_ci(self) -> tuple[float, float]:
        return wilson(self.false_positives, self.clean_runs_completed)


def summarize(cases: list[Case], results: list[RunResult], mode: str) -> ModeSummary:
    by_id = {c.id: c for c in cases}
    rs = [r for r in results if r.mode == mode]
    defect_runs = detected = 0
    cats: dict[str, list[int]] = {}
    for r in rs:
        for d in by_id[r.case_id].defects:
            defect_runs += 1
            hit = d.id in r.detected
            detected += hit
            c = cats.setdefault(d.category, [0, 0])
            c[0] += hit
            c[1] += 1
    clean_done = [r for r in rs if by_id[r.case_id].clean and r.completed]
    return ModeSummary(
        mode=mode,
        defect_runs=defect_runs,
        detected=detected,
        clean_runs_completed=len(clean_done),
        false_positives=sum(r.false_positive for r in clean_done),
        runs=len(rs),
        incomplete=sum(not r.completed for r in rs),
        unmatched=sum(r.unmatched for r in rs),
        cost_usd=sum(r.cost_usd for r in rs),
        cost_unknown=any(r.cost_unknown for r in rs),
        mean_latency_s=(sum(r.latency_s for r in rs) / len(rs)) if rs else 0.0,
        by_category={k: (v[0], v[1]) for k, v in cats.items()},
        input_tokens=sum(r.input_tokens for r in rs),
        output_tokens=sum(r.output_tokens for r in rs),
        trap_runs_completed=sum(bool(by_id[r.case_id].traps) for r in clean_done),
        trap_alarms=sum(r.trap_alarm for r in clean_done),
    )


def paired_comparison(cases: list[Case], results: list[RunResult], mode_a: str, mode_b: str) -> dict:
    """Per (case, defect, repeat): did A / B detect it? Exact sign test on the discordant pairs."""
    by_id = {c.id: c for c in cases}
    idx = {(r.mode, r.case_id, r.repeat): r for r in results}
    only_a = only_b = both = neither = 0
    for (mode, cid, rep), ra in idx.items():
        if mode != mode_a:
            continue
        rb = idx.get((mode_b, cid, rep))
        if rb is None:
            continue
        for d in by_id[cid].defects:
            a, b = d.id in ra.detected, d.id in rb.detected
            if a and b:
                both += 1
            elif a:
                only_a += 1
            elif b:
                only_b += 1
            else:
                neither += 1
    return {
        "a": mode_a, "b": mode_b, "only_a": only_a, "only_b": only_b, "both": both, "neither": neither,
        "p_value": sign_test(only_a, only_b),
    }
