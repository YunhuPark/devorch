"""Cost estimate for running `devorch bench run/judge` on a directory of cases, BEFORE any LLM call (docs/EVAL_DESIGN.md).

    python tools/eval/estimate_cost.py <cases dir> <project dir with .devorch/config.yaml> [--modes multi,single,solo:gemini:review] [--repeats 1]

Uses the same prompt builder and token/price model as `devorch review` (core/router/cost_model.py): reviewer calls get a low/mid/high range;
the judge (one call per defect per reviewed result, claude) is estimated from its prompt size with the implementation-style output range.
Prices come from config.yaml; a missing price makes that part "unknown", never 0. Reads local files only.
"""

from __future__ import annotations

import sys
from pathlib import Path

from devorch.bench.cases import load_cases
from devorch.bench.judge import JUDGE_SYSTEM, MAX_DIFF_CHARS
from devorch.bench.runner import review_steps
from devorch.config import load_config, load_policy
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.orchestrator.engine import WorkflowRun
from devorch.core.orchestrator.review import REVIEWER_SYSTEM, build_review_prompt
from devorch.core.router.cost_model import count_chars, estimate_counts, prices_from_config
from devorch.models import ImpactReport

JUDGE_OUT_TOKENS = 120  # {"detected": bool, "finding": n, "reason": "<one sentence>"}
JUDGE_FINDINGS_CHARS = 2500  # numbered findings of one review (measured sizes vary; assumption)


def main() -> None:
    args = sys.argv[1:]
    cases_dir, project = Path(args[0]), Path(args[1])
    modes = (args[args.index("--modes") + 1] if "--modes" in args else "multi,single,solo:gemini:review").split(",")
    repeats = int(args[args.index("--repeats") + 1]) if "--repeats" in args else 1
    cases = load_cases(cases_dir)
    policy, prices = load_policy(project), prices_from_config(load_config(project))
    print(f"{len(cases)} cases ({sum(1 for c in cases if c.defects)} with defects, {sum(1 for c in cases if c.clean)} clean), modes={modes}, repeats={repeats}")
    print("prices configured for:", ", ".join(sorted(prices)) or "(none)")

    totals = {m: [0.0, 0.0, 0.0] for m in modes}
    unknown: set[str] = set()
    n_calls = 0
    for case in cases:
        a = analyze_task(case.task)
        impact = ImpactReport(direct_files=list(case.files))
        run = WorkflowRun(task_text=case.task, analysis=a, impact=impact, risk=assess_risk(a, impact), implementation=case.diff)
        for mode in modes:
            for step in review_steps(policy, mode):
                text = REVIEWER_SYSTEM + build_review_prompt(run, step.model_copy(update={"purpose": step.role}))
                e = estimate_counts(step.provider, *count_chars(text), prices, max_output_tokens=policy.limits.review_max_tokens)
                n_calls += repeats
                if e.usd is None:
                    unknown.add(step.provider)
                    continue
                for i, v in enumerate((e.usd_low, e.usd, e.usd_high)):
                    totals[mode][i] += v * repeats
    judge = [0.0, 0.0, 0.0]
    j_calls = 0
    cp = prices.get("claude")
    for case in cases:
        for _ in case.defects:
            for mode in modes:
                j_calls += repeats
                if not cp:
                    unknown.add("claude(judge)")
                    continue
                a_chars, n_chars = count_chars(JUDGE_SYSTEM + case.task + case.diff[:MAX_DIFF_CHARS] + case.defects[0].description + "x" * JUDGE_FINDINGS_CHARS)
                tin = a_chars / 3.0 + n_chars * 0.95
                usd = (tin * cp["input_per_mtok"] + JUDGE_OUT_TOKENS * cp["output_per_mtok"]) / 1e6
                for i in range(3):
                    judge[i] += usd * (0.6, 1.0, 2.0)[i]  # output/prompt-size uncertainty: assumed spread, not measured
    for m, (lo, mid, hi) in totals.items():
        print(f"  reviewers {m:<24} ${mid:.3f}  (range ${lo:.3f} ~ ${hi:.3f})")
    rl, rm, rh = (sum(t[i] for t in totals.values()) for i in range(3))
    print(f"  reviewer calls: {n_calls}   TOTAL reviewers  ${rm:.3f}  (range ${rl:.3f} ~ ${rh:.3f})")
    print(f"  judge calls:    {j_calls}   TOTAL judge      ${judge[1]:.3f}  (assumed range ${judge[0]:.3f} ~ ${judge[2]:.3f})")
    print(f"  GRAND TOTAL  ${rm + judge[1]:.3f}  (range ${rl + judge[0]:.3f} ~ ${rh + judge[2]:.3f})")
    if unknown:
        print("  UNKNOWN (no price configured, NOT included above):", ", ".join(sorted(unknown)))


if __name__ == "__main__":
    main()
