"""`devorch doctor`: diagnose why DevOrch can't do its job on this machine — without a network call and without ever
printing a key."""

import json
import sys

import yaml
from gitutil import run_git
from typer.testing import CliRunner

from devorch.cli.main import app
from devorch.doctor import render, run_checks

GOOD = {"ANTHROPIC_API_KEY": "sk-ant-" + "a" * 40, "OPENAI_API_KEY": "sk-" + "b" * 40, "GEMINI_API_KEY": "AIza" + "c" * 35}
OA = {"input_per_mtok": 2.0, "output_per_mtok": 10.0}


def project(tmp_path, providers=None, policy=None, git=True, tests=True):
    r = tmp_path / "p"
    r.mkdir()
    d = r / ".devorch"
    d.mkdir()
    cfg = {"version": 1, "project": {"test_command": f"{sys.executable} -m pytest -q"} if tests else {},
           "providers": providers or {
               "claude": {"api_key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5", **OA},
               "openai": {"api_key_env": "OPENAI_API_KEY", "model": "gpt-x", **OA},
               "gemini": {"api_key_env": "GEMINI_API_KEY", "model": "gem-x", **OA},
           }}
    (d / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (d / "policy.yaml").write_text(yaml.safe_dump(policy or {}), encoding="utf-8")
    if git:
        run_git(r, "init", "-q", "-b", "main")
        run_git(r, "config", "user.email", "t@example.com")
        run_git(r, "config", "user.name", "T")
        (r / "a.py").write_text("x = 1\n", encoding="utf-8")
        run_git(r, "add", "-A")
        run_git(r, "commit", "-q", "-m", "init")
    return r


def by(checks, name):
    return next(c for c in checks if c.name == name)


def statuses(checks):
    return {c.name: c.status for c in checks}


def test_a_healthy_setup_has_no_failures_and_every_risk_level_is_runnable(tmp_path):
    checks = run_checks(project(tmp_path), env=GOOD)
    assert not [c for c in checks if c.status == "fail"], render(checks)
    for lvl in ("low", "medium", "high", "critical"):
        assert by(checks, f"위험도 {lvl}").status == "ok"
    assert all(by(checks, f"provider {p}").status == "ok" for p in ("claude", "openai", "gemini"))


def test_a_key_that_is_a_pasted_control_character_is_called_out_without_printing_it(tmp_path):
    env = {**GOOD, "OPENAI_API_KEY": "\x16"}  # what Ctrl+V into a SecureString prompt produced
    checks = run_checks(project(tmp_path), env=env)
    c = by(checks, "provider openai")
    assert c.status == "warn" and "보이지 않는 문자" in c.detail and "길이 1" in c.detail
    assert "Get-Clipboard" in c.hint or "export" in c.hint


def test_no_check_ever_contains_a_key_value(tmp_path):
    secret = "sk-SECRETVALUE0123456789abcdefghij"
    env = {**GOOD, "OPENAI_API_KEY": secret, "GEMINI_API_KEY": secret + " with space"}
    checks = run_checks(project(tmp_path), env=env)
    blob = render(checks) + json.dumps([c.__dict__ for c in checks], ensure_ascii=False)
    assert secret not in blob and "SECRETVALUE" not in blob


def test_an_unset_key_a_missing_model_and_a_missing_price_each_get_their_own_message(tmp_path):
    providers = {
        "claude": {"api_key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5"},  # no price
        "openai": {"api_key_env": "OPENAI_API_KEY", "model": None, **OA},  # no model
        "gemini": {"api_key_env": "GEMINI_API_KEY", "model": "gem-x", **OA},  # key unset below
    }
    env = {k: v for k, v in GOOD.items() if k != "GEMINI_API_KEY"}
    checks = run_checks(project(tmp_path, providers=providers), env=env)
    assert "단가" in by(checks, "provider claude").detail and by(checks, "provider claude").status == "warn"
    assert "model" in by(checks, "provider openai").detail
    assert "GEMINI_API_KEY" in by(checks, "provider gemini").detail


def test_levels_show_which_reviewers_are_missing(tmp_path):
    only_claude = {"ANTHROPIC_API_KEY": GOOD["ANTHROPIC_API_KEY"]}
    checks = run_checks(project(tmp_path), env=only_claude)
    assert by(checks, "위험도 low").status == "ok"  # no external reviewer needed
    medium = by(checks, "위험도 medium")
    assert medium.status == "warn" and "openai" in medium.detail
    assert "gemini" in by(checks, "위험도 high").detail


def test_nothing_usable_is_a_failure(tmp_path):
    checks = run_checks(project(tmp_path), env={})
    assert by(checks, "사용 가능한 provider").status == "fail"


def test_a_cost_cap_without_prices_is_a_failure_because_it_cannot_be_kept(tmp_path):
    providers = {"claude": {"api_key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5"},
                 "openai": {"api_key_env": "OPENAI_API_KEY", "model": "gpt-x"}, "gemini": {"api_key_env": "GEMINI_API_KEY", "model": "g"}}
    checks = run_checks(project(tmp_path, providers=providers, policy={"limits": {"max_cost_usd": 1.0}}), env=GOOD)
    c = by(checks, "비용 상한")
    assert c.status == "fail" and "단가" in c.detail


def test_a_cost_cap_with_every_price_set_is_fine(tmp_path):
    c = by(run_checks(project(tmp_path, policy={"limits": {"max_cost_usd": 1.0}}), env=GOOD), "비용 상한")
    assert c.status == "ok" and "$1.0" in c.detail


def test_no_cost_cap_is_only_a_recommendation(tmp_path):
    c = by(run_checks(project(tmp_path), env=GOOD), "비용 상한")
    assert c.status == "info" and "max_cost_usd" in c.detail


def test_project_state_problems_have_actionable_hints(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    checks = run_checks(bare, env=GOOD)
    assert by(checks, "프로젝트 설정").status == "warn" and "devorch init" in by(checks, "프로젝트 설정").hint
    assert by(checks, "git 저장소").status == "warn"
    nogit = run_checks(project(tmp_path, git=False, tests=False), env=GOOD)
    assert by(nogit, "git 저장소").status == "warn"
    assert by(nogit, "테스트 명령").status == "warn"


def test_mcp_registration_command_is_given_for_this_project(tmp_path):
    p = project(tmp_path)
    c = by(run_checks(p, env=GOOD), "MCP 등록")
    assert str(p) in c.detail and "mcp" in c.detail and c.status == "info"


def test_a_broken_config_file_is_reported_not_raised(tmp_path):
    p = project(tmp_path)
    (p / ".devorch" / "policy.yaml").write_text("rules: [1, 2", encoding="utf-8")
    checks = run_checks(p, env=GOOD)
    assert by(checks, "설정 파일").status == "fail"


def test_cli_doctor_exit_codes_and_json(tmp_path, monkeypatch):
    p = project(tmp_path)
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    ok = CliRunner().invoke(app, ["doctor", "-p", str(p)])
    assert ok.exit_code == 0, ok.output
    assert "[OK]" in ok.output
    for k in GOOD:
        monkeypatch.delenv(k)
    bad = CliRunner().invoke(app, ["doctor", "-p", str(p)])
    assert bad.exit_code == 1 and "[실패]" in bad.output
    js = json.loads(CliRunner().invoke(app, ["doctor", "-p", str(p), "--json"]).output)
    assert any(c["name"] == "사용 가능한 provider" and c["status"] == "fail" for c in js["checks"]) and js["ok"] is False
