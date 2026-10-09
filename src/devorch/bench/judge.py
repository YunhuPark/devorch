"""LLM judge for seeded-defect detection — a second opinion next to the regex matcher, NOT a replacement.

Runs on SAVED results (no reviewer is called again). The judge sees the task, the diff, ONE seeded defect and the
numbered findings — never who wrote them (blind) and never the regex. Fail-closed: a failed or unparsable judgement
is 'unjudged' (judge=None), counted separately, never silently a hit or a miss. The judge itself is unvalidated:
the report lists every disagreement with the judge's reason so a human can decide.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from devorch.audit import AuditLogger
from devorch.bench.cases import Case, Defect
from devorch.bench.metrics import majority_detected, wilson
from devorch.core.orchestrator.engine import WorkflowRun
from devorch.core.orchestrator.review import BudgetExceeded, call_provider, describe_error
from devorch.models import Finding, RoutingStep
from devorch.providers.base import LLMRequest, ProviderRegistry
from devorch.security.secret_redactor import redact

JUDGE_SYSTEM = (
    "You grade a code-review benchmark. You get ONE seeded defect (the ground truth) and a numbered list of findings "
    "written by a reviewer. Decide whether ANY finding identifies that specific defect: the same root problem, even if "
    "worded differently or written in another language. A finding about a different problem does NOT count, even if it "
    'is valid or concerns the same file. Reply ONLY with JSON: {"detected": true|false, "finding": <number or null>, '
    '"reason": "<one short sentence>"}.'
)
MAX_DIFF_CHARS = 4000
MAX_DETAIL_CHARS = 500


@dataclass(frozen=True)
class Verdict:
    detected: bool | None  # None = could not be parsed into an explicit boolean
    finding: int | None
    reason: str


def parse_verdict(text: str) -> Verdict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return Verdict(None, None, "")
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return Verdict(None, None, "")
    if not isinstance(obj, dict) or not isinstance(obj.get("detected"), bool):
        return Verdict(None, None, "")  # "true", 1, null, missing: not an explicit answer
    finding = obj.get("finding")
    reason = obj.get("reason")
    return Verdict(
        obj["detected"],
        finding if isinstance(finding, int) and not isinstance(finding, bool) else None,
        reason.strip()[:300] if isinstance(reason, str) else "",
    )


def build_judge_prompt(case: Case, defect: Defect, findings: list[Finding]) -> str:
    lines = [
        f"Task given to the developer: {case.task}",
        "",
        f"Diff under review:\n{case.diff[:MAX_DIFF_CHARS]}",
        "",
        f"Seeded defect (ground truth): {defect.description}",
        f"File: {defect.file or '-'}",
        "",
        "Findings:",
    ]
    for i, f in enumerate(findings, 1):  # no source/role/category: the judge must not know which model said what
        lines.append(f"{i}. [{f.severity.value}] {f.title} — {(f.detail or '')[:MAX_DETAIL_CHARS]}")
    return "\n".join(lines)


@dataclass
class JudgeRecord:
    mode: str
    repeat: int
    case_id: str
    defect_id: str
    regex: bool  # what the regex matcher said (from the saved results)
    judge: bool | None  # None = unjudged
    finding: int | None = None
    finding_title: str | None = None
    reason: str = ""
    n_findings: int = 0


@dataclass
class JudgeOutcome:
    records: list[JudgeRecord]
    cost_usd: float = 0.0
    cost_unknown: bool = False
    usage: dict[str, dict[str, int]] = field(default_factory=dict)
    model: str = ""
    judge_provider: str = ""


def _judgeable(cases: dict[str, Case], payload: dict):
    """Yield (result dict, case, defect, findings) for every completed defect-case run, one per seeded defect."""
    for r in payload["results"]:
        case = cases[r["case_id"]]
        if not r["completed"] or case.clean:
            continue
        findings = [Finding.model_validate(f) for f in r["findings"]]
        for d in case.defects:
            yield r, case, d, findings


def count_judge_calls(cases: list[Case], payload: dict) -> int:
    by_id = {c.id: c for c in cases}
    return sum(1 for _, _, _, findings in _judgeable(by_id, payload) if findings)


async def judge_results(
    cases: list[Case],
    payload: dict,
    providers: ProviderRegistry,
    provider_name: str,
    concurrency: int = 4,
    max_total_cost_usd: float | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> JudgeOutcome:
    by_id = {c.id: c for c in cases}
    run = WorkflowRun(task_text="bench judge")  # accumulates cost / usage / model for the judge calls only
    audit = AuditLogger(None)
    step = RoutingStep(role="judge", provider=provider_name, purpose="judge")
    sem = asyncio.Semaphore(max(1, concurrency))
    items = list(_judgeable(by_id, payload))
    records: list[JudgeRecord | None] = [None] * len(items)
    total = sum(1 for *_, findings in items if findings)
    done = 0

    def base(r: dict, d: Defect, findings: list[Finding]) -> JudgeRecord:
        return JudgeRecord(r["mode"], r["repeat"], r["case_id"], d.id, d.id in set(r["detected"]), None, n_findings=len(findings))

    async def one(i: int, r: dict, case: Case, d: Defect, findings: list[Finding]) -> None:
        nonlocal done
        rec = base(r, d, findings)
        records[i] = rec
        if not findings:
            rec.judge, rec.reason = False, "지적 없음 (호출 없이 미탐지로 판정)"
            return
        async with sem:
            try:
                resp = await call_provider(
                    run, providers, audit, step,
                    LLMRequest(role="judge", system=JUDGE_SYSTEM, prompt=build_judge_prompt(case, d, findings)),
                    max_cost_usd=max_total_cost_usd,
                )
            except BudgetExceeded:
                rec.reason = "판정 비용 합계 상한에 도달해 판정하지 않음"
            except Exception as e:  # noqa: BLE001 — any provider failure is 'unjudged', recorded, never a verdict
                rec.reason = f"판정 호출 실패: {describe_error(e)}"
            else:
                v = parse_verdict(resp.text)
                if v.detected is None:
                    rec.reason = "판정 응답을 파싱하지 못함"
                else:
                    rec.judge, rec.reason = v.detected, redact(v.reason)
                    if v.finding is not None and 1 <= v.finding <= len(findings):
                        rec.finding, rec.finding_title = v.finding, findings[v.finding - 1].title
            done += 1
            if on_progress:
                on_progress(done, total)

    await asyncio.gather(*(one(i, *item) for i, item in enumerate(items)))
    return JudgeOutcome(
        [r for r in records if r is not None], run.total_cost_usd, run.cost_unknown, run.usage,
        run.models.get(provider_name, ""), provider_name,
    )


@dataclass
class JudgedSummary:
    mode: str
    total_defects: int = 0
    regex_detected: int = 0  # defects found in a majority of repeats, by the regex
    judge_detected: int = 0  # same, by the judge (unjudged counts as not detected, and is reported)
    unjudged: int = 0  # judge records with no verdict
    agreements: int = 0
    disagreements: list[JudgeRecord] = field(default_factory=list)

    @property
    def judge_ci(self) -> tuple[float, float]:
        return wilson(self.judge_detected, self.total_defects)

    @property
    def regex_ci(self) -> tuple[float, float]:
        return wilson(self.regex_detected, self.total_defects)


def summarize_judged(cases: list[Case], records: list[JudgeRecord]) -> dict[str, JudgedSummary]:
    out: dict[str, JudgedSummary] = {}
    per: dict[tuple[str, str, str], list[JudgeRecord]] = defaultdict(list)
    for r in records:
        per[(r.mode, r.case_id, r.defect_id)].append(r)
        s = out.setdefault(r.mode, JudgedSummary(r.mode))
        if r.judge is None:
            s.unjudged += 1
        elif r.judge == r.regex:
            s.agreements += 1
        else:
            s.disagreements.append(r)
    for (mode, _, _), rs in per.items():
        s = out[mode]
        s.total_defects += 1
        s.regex_detected += majority_detected(sum(x.regex for x in rs), len(rs))
        s.judge_detected += majority_detected(sum(x.judge is True for x in rs), len(rs))
    return out


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def render_judged_report(cases: list[Case], payload: dict, outcome: JudgeOutcome, judge_provider: str) -> str:
    sums = summarize_judged(cases, outcome.records)
    reviewer_providers = sorted({p for r in payload["results"] for p in (r.get("models") or {})})
    lines = ["# DevOrch Benchmark — 판정기(LLM judge) 재판정", ""]
    lines += [
        f"- 판정기: {judge_provider}" + (f" ({outcome.model})" if outcome.model else "") + f" — 판정 호출 {sum(1 for r in outcome.records if r.n_findings)}회, "
        + ("비용 unknown" if outcome.cost_unknown else f"판정 비용 ${outcome.cost_usd:.4f}"),
        "- **이 판정기 자체는 검증되지 않았습니다.** 정규식을 대체하지 않으며, 두 판정이 엇갈린 항목은 사람이 확인해야 합니다.",
    ]
    if judge_provider in reviewer_providers:
        lines.append(
            f"> ⚠️ 판정기({judge_provider})가 리뷰어와 같은 회사 모델입니다. 자기 선호 편향이 있을 수 있어, 가능하면 리뷰어와 다른 회사 모델로 판정하세요."
        )
    lines += [
        "",
        "## 결함 단위 탐지 (반복은 과반에서 탐지돼야 탐지)",
        "",
        "| 모드 | 정규식 | 판정기 | 판정기 95% 구간 | 두 판정 일치 | 엇갈림 | 미판정 |",
        "|---|---|---|---|---|---|---|",
    ]
    for m, s in sums.items():
        lo, hi = s.judge_ci
        judged = s.agreements + len(s.disagreements)
        lines.append(
            f"| {m} | {s.regex_detected}/{s.total_defects} | {s.judge_detected}/{s.total_defects} | [{_pct(lo)}–{_pct(hi)}] "
            f"| {s.agreements}/{judged} | {len(s.disagreements)} | {s.unjudged} |"
        )
    lines += ["", "## 엇갈린 판정 (사람이 확인할 것)", ""]
    any_dis = False
    for m, s in sums.items():
        for d in s.disagreements:
            any_dis = True
            who = "정규식 탐지 / 판정기 미탐지" if d.regex else "정규식 미탐지 / 판정기 탐지"
            title = f" — 지적: {d.finding_title}" if d.finding_title else ""
            lines.append(f"- `{m}#{d.repeat}` {d.case_id}.{d.defect_id}: {who}{title} — 판정기 사유: {d.reason}")
    if not any_dis:
        lines.append("- 없음")
    unj = [r for r in outcome.records if r.judge is None]
    if unj:
        lines += ["", "## 미판정 (호출 실패·파싱 실패·비용 상한) — 탐지로도 미탐지로도 세지 않음", ""]
        lines += [f"- `{r.mode}#{r.repeat}` {r.case_id}.{r.defect_id}: {r.reason}" for r in unj]
    lines += [
        "",
        "## 한계",
        "",
        "- 판정기는 LLM이라 비결정적이고 틀릴 수 있습니다. 한 번만 판정했습니다.",
        "- 판정기는 어느 모델이 쓴 지적인지, 정규식이 무엇인지 모릅니다(블라인드). 시드한 결함 설명과 지적 목록만 봅니다.",
        "- 시드한 결함 정의 자체가 실제로 중요한 문제와 다르면(예: c15) 판정기도 '미탐지'라고 답하는 것이 맞습니다.",
        "",
    ]
    return "\n".join(lines)


def outcome_to_json(outcome: JudgeOutcome) -> dict:
    return {
        "judge": outcome.judge_provider, "model": outcome.model, "cost_usd": outcome.cost_usd,
        "cost_unknown": outcome.cost_unknown, "usage": outcome.usage,
        "records": [asdict(r) for r in outcome.records],
    }
