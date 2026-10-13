import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from devorch.cli.main import app
from devorch.usage import load_events, render, summarize

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def ev(run, event, data, ago_h=1.0):
    return {"ts": (NOW - timedelta(hours=ago_h)).isoformat(timespec="seconds"), "run_id": run, "event": event, "data": data}


def call(run, role, provider, cost, inp=1000, out=500, model="m", ago_h=1.0, **extra):
    return ev(run, "llm_call", {"role": role, "provider": provider, "model": model, "input_tokens": inp, "output_tokens": out, "cost_usd": cost, **extra}, ago_h)


def test_costs_are_summed_per_task_and_provider_and_the_task_text_comes_from_the_log():
    events = [
        ev("t1", "mcp:analyze_task", {"task": "결제 로직 변경", "risk": "high"}),
        call("t1", "security", "openai", 0.02), call("t1", "architecture", "gemini", 0.03),
        ev("t2", "task_created", {"task": "버그 수정"}), call("t2", "implementation", "claude", 0.01),
    ]
    s = summarize(events, now=NOW)
    assert s["total_cost_usd"] == pytest.approx(0.06) and s["calls"] == 3
    t1 = next(r for r in s["runs"] if r["id"] == "t1")
    assert t1["task"] == "결제 로직 변경" and t1["risk"] == "high" and t1["cost_usd"] == pytest.approx(0.05)
    assert s["by_provider"]["openai"]["cost_usd"] == pytest.approx(0.02) and s["by_provider"]["claude"]["calls"] == 1


def test_the_risk_of_a_cli_run_comes_from_its_planned_state_event():
    events = [ev("t9", "task_created", {"task": "x"}), ev("t9", "state", {"from": "ANALYZING", "to": "PLANNED", "risk": "medium"}), call("t9", "review", "openai", 0.01)]
    assert summarize(events, now=NOW)["runs"][0]["risk"] == "medium"


def test_an_unpriced_call_is_flagged_as_unknown_never_summed_as_zero():
    s = summarize([call("t1", "review", "openai", None), call("t1", "security", "gemini", 0.02)], now=NOW)
    assert s["unknown_calls"] == 1 and s["total_cost_usd"] == pytest.approx(0.02)
    text = render(s)
    assert "비용을 알 수 없는" in text and "실제 지출은 이보다 큽니다" in text


def test_mock_calls_are_left_out_unless_asked():
    events = [call("t1", "review", "openai", 0.0, model="mock"), call("t2", "review", "openai", 0.01)]
    assert [r["id"] for r in summarize(events, now=NOW)["runs"]] == ["t2"]
    assert len(summarize(events, include_mock=True, now=NOW)["runs"]) == 2
    assert "기록된 LLM 호출이 없습니다" in render(summarize([call("t1", "review", "openai", 0.0, model="mock")], now=NOW))


def test_days_filters_by_age():
    events = [call("old", "review", "openai", 0.5, ago_h=24 * 10), call("new", "review", "openai", 0.01, ago_h=2)]
    assert [r["id"] for r in summarize(events, days=3, now=NOW)["runs"]] == ["new"]
    assert len(summarize(events, now=NOW)["runs"]) == 2


def test_failed_calls_are_counted_and_their_cost_is_kept():
    s = summarize([call("t1", "review", "gemini", 0.04, failed=True, error="cut at max_tokens"), call("t1", "review", "gemini", 0.04)], now=NOW)
    assert s["failed_calls"] == 1 and s["total_cost_usd"] == pytest.approx(0.08)


def test_review_estimate_is_compared_with_the_actual_review_cost_only():
    est = ev("t1", "mcp:review_estimate", {"estimated_usd": 0.03, "estimated_low_usd": 0.02, "estimated_high_usd": 0.05})
    inside = summarize([est, call("t1", "review", "openai", 0.04)], now=NOW)
    assert inside["estimate_check"] == {"comparable_runs": 1, "inside_range": 1}
    outside = summarize([est, call("t1", "review", "openai", 0.09)], now=NOW)
    assert outside["estimate_check"] == {"comparable_runs": 1, "inside_range": 0} and "범위 밖" in render(outside)
    # the implementation call is not part of the review estimate
    impl = summarize([est, call("t1", "implementation", "claude", 0.50), call("t1", "review", "openai", 0.04)], now=NOW)
    assert impl["estimate_check"]["inside_range"] == 1
    # an unpriced estimate or an unknown review cost can't be compared
    unknown_est = ev("t1", "mcp:review_estimate", {"estimated_usd": None, "estimated_low_usd": None, "estimated_high_usd": None})
    assert summarize([unknown_est, call("t1", "review", "openai", 0.04)], now=NOW)["estimate_check"]["comparable_runs"] == 0
    assert summarize([est, call("t1", "review", "openai", None)], now=NOW)["estimate_check"]["comparable_runs"] == 0


def test_a_missing_or_partly_corrupt_log_does_not_crash(tmp_path):
    assert load_events(tmp_path / "nope.jsonl") == []
    p = tmp_path / "audit.jsonl"
    good = json.dumps(call("t1", "review", "openai", 0.01))
    p.write_text(f"{good}\nnot json\n{{\"truncated\": \n[1,2]\n{good}", encoding="utf-8")
    assert len(load_events(p)) == 2


def test_cli_usage_reads_the_project_audit_log(tmp_path):
    (tmp_path / ".devorch").mkdir()
    recs = [ev("t1", "mcp:analyze_task", {"task": "로그인 추가", "risk": "high"}), call("t1", "security", "openai", 0.0123)]
    (tmp_path / ".devorch" / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["usage", "-p", str(tmp_path)])
    assert r.exit_code == 0 and "$0.0123" in r.output and "로그인 추가" in r.output and "HIGH" in r.output
    js = json.loads(CliRunner().invoke(app, ["usage", "-p", str(tmp_path), "--json"]).output)
    assert js["total_cost_usd"] == pytest.approx(0.0123)
    empty = CliRunner().invoke(app, ["usage", "-p", str(tmp_path / "other")])
    assert empty.exit_code == 0 and "기록된 LLM 호출이 없습니다" in empty.output
