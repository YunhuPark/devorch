"""Markdown report. Mock runs always carry a banner; limitations are always listed."""

from __future__ import annotations

from devorch.bench.cases import Case
from devorch.bench.metrics import ModeSummary, RunResult, paired_comparison, summarize

MOCK_BANNER = (
    "> ⚠️ **MOCK — harness check only.** 이 결과는 가짜 리뷰어(정규식 규칙)로 만든 것이며 "
    "실제 LLM의 성능을 나타내지 않습니다. 인용/비교하지 마세요."
)

LIMITATIONS = [
    "케이스 수가 적습니다(신뢰구간이 넓음). 구간이 겹치면 차이가 있다고 말할 수 없습니다.",
    "탐지 판정은 정규식 매칭입니다. 올바르게 지적했지만 표현이 달라 놓친 경우(과소)나, 우연히 맞은 경우(과대)가 있을 수 있습니다.",
    "'매칭 안 된 지적'은 오탐이 아니라 *확인 필요* 항목입니다(유효한 추가 지적일 수 있음). 오탐률은 대조군에서만 측정합니다.",
    "LLM 출력은 비결정적입니다. --repeats 로 반복 측정해야 변동을 볼 수 있습니다.",
    "불완전한 리뷰(호출 실패/파싱 실패)는 '탐지 못함'으로 계산합니다(fail-closed).",
    "비용은 provider 단가가 설정된 경우에만 합산되며, 아니면 unknown 입니다.",
]


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _ci(ci: tuple[float, float]) -> str:
    return f"[{_pct(ci[0])}–{_pct(ci[1])}]"


def _cost(s: ModeSummary) -> str:
    if s.cost_unknown:
        return f"unknown (알려진 합계 ${s.cost_usd:.4f})" if s.cost_usd else "unknown"
    return f"${s.cost_usd:.4f}"


def render_report(cases: list[Case], results: list[RunResult], modes: list[str], mock: bool, repeats: int) -> str:
    lines = ["# DevOrch Benchmark Report", ""]
    if mock:
        lines += [MOCK_BANNER, ""]
    n_def = sum(len(c.defects) for c in cases)
    lines += [
        f"- 케이스 {len(cases)}개 (결함 {n_def}개 / 대조군 {sum(c.clean for c in cases)}개), 반복 {repeats}회, 모드: {', '.join(modes)}",
        "",
        "## 요약 (95% Wilson 신뢰구간)",
        "",
        "| 모드 | 탐지율 | 오탐률(대조군) | 불완전 리뷰 | 매칭 안 된 지적 | 비용 | 평균 지연(s) |",
        "|---|---|---|---|---|---|---|",
    ]
    sums = {m: summarize(cases, results, m) for m in modes}
    for m, s in sums.items():
        lines.append(
            f"| {m} | {s.detected}/{s.defect_runs} = {_pct(s.detection_rate)} {_ci(s.detection_ci)} "
            f"| {s.false_positives}/{s.clean_runs_completed} = {_pct(s.fp_rate)} {_ci(s.fp_ci)} "
            f"| {s.incomplete}/{s.runs} | {s.unmatched} | {_cost(s)} | {s.mean_latency_s:.2f} |"
        )
    cats = sorted({k for s in sums.values() for k in s.by_category})
    if cats:
        lines += ["", "## 카테고리별 탐지", "", "| 카테고리 | " + " | ".join(modes) + " |", "|---|" + "---|" * len(modes)]
        for c in cats:
            cells = []
            for m in modes:
                d, t = sums[m].by_category.get(c, (0, 0))
                cells.append(f"{d}/{t}")
            lines.append(f"| {c} | " + " | ".join(cells) + " |")
    if len(modes) == 2:
        p = paired_comparison(cases, results, modes[0], modes[1])
        lines += [
            "",
            "## 쌍체 비교 (정확 부호검정)",
            "",
            f"- {p['a']}만 탐지 {p['only_a']}, {p['b']}만 탐지 {p['only_b']}, 둘 다 {p['both']}, 둘 다 못함 {p['neither']}",
            f"- p = {p['p_value']:.3f} " + ("(차이가 있다고 보기 어려움: p ≥ 0.05)" if p["p_value"] >= 0.05 else "(p < 0.05)"),
        ]
    by_id = {c.id: c for c in cases}
    lines += ["", "## 케이스별 결과", "", "| 케이스 | 모드 | 결과 |", "|---|---|---|"]
    for r in sorted(results, key=lambda r: (r.case_id, r.mode, r.repeat)):
        c = by_id[r.case_id]
        if not r.completed:
            res = "⚠ 불완전"
        elif c.clean:
            res = "오탐" if r.false_positive else "정상(오탐 없음)"
        else:
            res = ", ".join(f"{'✔' if d.id in r.detected else '✘'} {d.id}" for d in c.defects)
        lines.append(f"| {r.case_id} | {r.mode}#{r.repeat} | {res} |")
    lines += ["", "## 한계", ""] + [f"- {x}" for x in LIMITATIONS] + [""]
    return "\n".join(lines)
