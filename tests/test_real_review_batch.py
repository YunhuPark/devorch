"""tools/eval/real_review_batch.py: one command, many real reviews, a hard total cap, and honest summary numbers."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "eval" / "real_review_batch.py"
spec = importlib.util.spec_from_file_location("real_review_batch", SCRIPT)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def row(commit, est, actual, in_tok=1000, est_in=1000, out_tok=400, est_out=400, low=None, high=None, warnings=()):
    return {"commit": commit, "est_usd": est, "actual_usd": actual, "in_tokens": in_tok, "est_in_tokens": est_in, "out_tokens": out_tok,
            "est_out_tokens": est_out, "est_low": low if low is not None else est / 2, "est_high": high if high is not None else est * 2.5,
            "warnings": list(warnings), "blocking": 0}


def test_it_stops_before_a_call_that_could_pass_the_total_cap():
    calls = []

    def runner(c):
        calls.append(c)
        return row(c, 0.01, 0.03)

    rows = batch.run_batch(["a", "b", "c", "d"], runner, max_total_usd=0.07)
    # after two calls $0.06 is spent and the next could cost another ~$0.03: that would pass $0.07
    assert calls == ["a", "b"] and [r.get("skipped") for r in rows] == [None, None, True, True]
    assert "총상한" in rows[2]["detail"]


def test_the_first_call_always_runs_and_a_failed_call_costs_nothing_and_does_not_stop_the_rest():
    def runner(c):
        return {"commit": c, "failed": True, "detail": "x"} if c == "bad" else row(c, 0.01, 0.012)

    rows = batch.run_batch(["bad", "a", "b"], runner, max_total_usd=0.40)
    assert [bool(r.get("failed")) for r in rows] == [True, False, False] and not any(r.get("skipped") for r in rows)


def test_a_zero_cost_dry_run_never_trips_the_cap():
    rows = batch.run_batch(list("abcdef"), lambda c: row(c, 0.01, 0.0), max_total_usd=0.001)
    assert not any(r.get("skipped") for r in rows)


def test_summary_reports_ratio_range_token_errors_and_warnings():
    rows = [
        row("a", 0.01, 0.012, in_tok=950, est_in=1000, out_tok=900, est_out=450, warnings=["w"]),
        row("b", 0.02, 0.01, in_tok=1100, est_in=1000, out_tok=200, est_out=400),
        {"commit": "c", "failed": True},
        row("d", 0.01, 0.05, low=0.005, high=0.02),  # outside its estimated range
    ]
    s = batch.summarize(rows)
    assert s["calls"] == 4 and s["failed"] == 1 and s["total_actual_usd"] == 0.072
    assert s["actual_over_estimate"] == {"min": 0.5, "max": 5.0, "mean": 2.23}
    assert s["in_estimated_range"] == "2/3" and s["input_token_error_pct"][:2] == [-5.0, 10.0]
    assert s["output_token_ratio"][:2] == [2.0, 0.5] and s["with_warnings"] == 1


def test_a_free_dry_run_summary_does_not_pretend_to_have_ratios():
    s = batch.summarize([row("a", 0.01, 0.0), row("b", 0.02, 0.0)])
    assert s["total_actual_usd"] == 0.0 and "actual_over_estimate" not in s


def test_json_is_found_after_leading_noise_and_garbage_is_not_json():
    assert batch.parse_json('경고 한 줄\n{"status": "READY"}') == {"status": "READY"}
    assert batch.parse_json("no json here") is None and batch.parse_json("{broken") is None
