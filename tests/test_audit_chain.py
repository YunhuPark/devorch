"""The audit log is tamper-EVIDENT: editing, removing, reordering or forging records, or cutting off its end, is detected by `devorch audit`."""

import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from devorch.audit import AuditLogger, verify_audit
from devorch.cli.main import app
from devorch.usage import load_events, summarize


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def write(path, ls):
    path.write_text("\n".join(ls) + "\n", encoding="utf-8")


@pytest.fixture
def log(tmp_path):
    p = tmp_path / ".devorch" / "audit.jsonl"
    a = AuditLogger(p)
    for i in range(6):
        a.log("run1", "llm_call", {"i": i, "provider": "openai", "cost_usd": 0.01, "한글": "값"})
    return p


def test_an_untouched_log_verifies_and_keeps_its_readable_fields(log):
    r = verify_audit(log)
    assert r.ok and r.total == 6 and r.chained == 6 and r.legacy == 0 and r.problems == [] and r.warnings == []
    first = json.loads(lines(log)[0])
    assert {"ts", "run_id", "event", "data", "seq", "prev", "mac"} <= set(first) and first["seq"] == 1 and first["prev"] == ""
    assert json.loads(lines(log)[1])["prev"] == first["mac"]  # chained


def test_editing_a_record_is_detected_at_that_line(log):
    ls = lines(log)
    rec = json.loads(ls[2])
    rec["data"]["cost_usd"] = 0.0  # hide a cost
    ls[2] = json.dumps(rec, ensure_ascii=False)
    write(log, ls)
    r = verify_audit(log)
    assert not r.ok and any(p.startswith("3번째 줄") and "서명이 맞지 않음" in p for p in r.problems)


def test_removing_a_record_from_the_middle_is_detected(log):
    ls = lines(log)
    del ls[2]
    write(log, ls)
    r = verify_audit(log)
    assert not r.ok and any("이어지지 않음" in p for p in r.problems) and any("순번" in p for p in r.problems)


def test_reordering_records_is_detected(log):
    ls = lines(log)
    ls[1], ls[2] = ls[2], ls[1]
    write(log, ls)
    assert not verify_audit(log).ok


def test_a_record_added_by_hand_cannot_be_signed(log):
    forged = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": "run1", "event": "approval", "data": {"approved": True}, "seq": 7, "prev": json.loads(lines(log)[-1])["mac"], "mac": "f" * 64}
    write(log, lines(log) + [json.dumps(forged)])
    r = verify_audit(log)
    assert not r.ok and any("서명이 맞지 않음" in p for p in r.problems)
    unsigned = {k: v for k, v in forged.items() if k != "mac"}
    write(log, lines(log)[:-1] + [json.dumps(unsigned)])  # a legacy-looking line AFTER the chain started
    assert not verify_audit(log).ok


def test_cutting_records_off_the_end_is_detected_through_the_signed_head(log):
    write(log, lines(log)[:-2])  # the remaining prefix is itself a valid chain
    r = verify_audit(log)
    assert not r.ok and any("audit.head" in p and "끝부분" in p for p in r.problems)


def test_a_missing_head_is_only_a_warning(log):
    log.with_name("audit.head").unlink()
    r = verify_audit(log)
    assert r.ok and any("audit.head" in w for w in r.warnings)


def test_a_forged_head_does_not_hide_truncation(log):
    head = log.with_name("audit.head")
    keep = lines(log)[:-1]
    last = json.loads(keep[-1])
    head.write_text(json.dumps({"count": len(keep), "mac": last["mac"], "sig": "0" * 64}), encoding="utf-8")  # makes the shorter log "match" but cannot sign
    write(log, keep)
    r = verify_audit(log)
    assert not r.ok and any("audit.head 의 서명" in p for p in r.problems)


def test_an_old_unsigned_log_stays_valid_and_the_chain_starts_after_it(tmp_path):
    p = tmp_path / ".devorch" / "audit.jsonl"
    p.parent.mkdir(parents=True)
    old = [json.dumps({"ts": "2026-10-01T00:00:00+00:00", "run_id": "old", "event": "state", "data": {"i": i}}) for i in range(3)]
    write(p, old)
    a = AuditLogger(p)
    a.log("new", "state", {"x": 1})
    a.log("new", "state", {"x": 2})
    r = verify_audit(p)
    assert r.ok and r.legacy == 3 and r.chained == 2 and r.total == 5
    assert json.loads(lines(p)[3])["seq"] == 4  # numbering continues from the legacy lines
    write(p, lines(p) + [old[0]])  # an unsigned line appended after the chain started is not legacy any more
    assert not verify_audit(p).ok


def test_concurrent_writers_never_fork_the_chain(tmp_path):
    p = tmp_path / ".devorch" / "audit.jsonl"
    code = (
        "import sys;from pathlib import Path;from devorch.audit import AuditLogger;"
        "a=AuditLogger(Path(sys.argv[1]));[a.log(sys.argv[2],'e',{'i':i}) for i in range(15)]"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(p), f"w{k}"]) for k in range(3)]
    assert [pr.wait(timeout=120) for pr in procs] == [0, 0, 0]
    r = verify_audit(p)
    assert r.ok and r.total == 45 and r.chained == 45, r.problems[:3]


def test_a_missing_key_never_loses_an_event_but_the_gap_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVORCH_KEY_FILE", str(tmp_path))  # a directory: no usable key
    p = tmp_path / "audit.jsonl"
    AuditLogger(p).log("r", "e", {"a": 1})
    assert len(lines(p)) == 1 and json.loads(lines(p)[0])["event"] == "e"
    assert not verify_audit(p).ok


def test_a_corrupt_line_is_reported_not_raised(log):
    write(log, lines(log)[:3] + ["{ this is not json"] + lines(log)[3:])
    r = verify_audit(log)
    assert not r.ok and any("JSON 이 아님" in p for p in r.problems)


def test_a_missing_log_is_not_an_error(tmp_path):
    r = verify_audit(tmp_path / "nope.jsonl")
    assert r.ok and r.total == 0 and r.warnings


def test_the_spend_report_still_reads_chained_records(log):
    s = summarize(load_events(log))
    assert s["calls"] == 6 and s["total_cost_usd"] == pytest.approx(0.06)


def test_cli_audit_exit_codes_and_json(log, tmp_path):
    project = tmp_path
    ok = CliRunner().invoke(app, ["audit", "-p", str(project)])
    assert ok.exit_code == 0 and "이상 없음" in ok.output and "막는 것이 아닙니다" in ok.output
    ls = lines(log)
    ls[1] = ls[1].replace('"i": 1', '"i": 99')
    write(log, ls)
    bad = CliRunner().invoke(app, ["audit", "-p", str(project)])
    assert bad.exit_code == 1 and "변조" in bad.output
    js = json.loads(CliRunner().invoke(app, ["audit", "-p", str(project), "--json"]).output)
    assert js["ok"] is False and js["problems"]


def test_doctor_reports_the_audit_log_state(log, tmp_path):
    from devorch.doctor import run_checks

    chk = next(c for c in run_checks(tmp_path, {}) if c.name == "감사 로그")
    assert chk.status == "ok"
    ls = lines(log)
    del ls[2]
    write(log, ls)
    chk = next(c for c in run_checks(tmp_path, {}) if c.name == "감사 로그")
    assert chk.status == "fail" and "audit" in chk.hint
