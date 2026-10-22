"""`devorch quickstart`: the first-run path in one command (git check -> init -> doctor -> policy accept -> MCP registration + first request).

Before it, a new user had to find init, the key setup, `policy accept` and the MCP command in different places.
"""

import json
import sys

import pytest
from gitutil import run_git
from typer.testing import CliRunner

import devorch.cli.main as cli_main
from devorch.security import policy_guard


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for n in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(n, raising=False)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        run_git(r, *a)
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (r / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "init")
    return r


def run(repo, *args, input=None):
    return CliRunner().invoke(cli_main.app, ["quickstart", "-p", str(repo), *args], input=input)


def test_a_fresh_project_gets_initialised_and_a_ready_to_paste_mcp_command(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)
    res = run(repo)
    assert res.exit_code == 0, res.output
    assert (repo / ".devorch" / "policy.yaml").exists() and (repo / ".devorch" / "config.yaml").exists()
    assert f'claude mcp add devorch -- "{sys.executable}" -m devorch mcp -p {repo}' in res.output
    assert "devorch 로 진행해줘" in res.output  # the first request to give the agent


def test_without_a_terminal_it_tells_the_human_to_accept_and_never_accepts_for_them(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)
    res = run(repo)
    assert "devorch policy accept" in res.output
    assert policy_guard.check(repo).status == "unaccepted"  # an unattended run must not self-accept


def test_with_a_terminal_the_human_accepts_in_the_same_flow(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    res = run(repo, input="y\n")
    assert res.exit_code == 0 and policy_guard.check(repo).ok


def test_declining_the_policy_leaves_it_unaccepted_and_says_so(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    res = run(repo, input="n\n")
    assert res.exit_code == 0 and "승인하지 않았습니다" in res.output and policy_guard.check(repo).status == "unaccepted"


def test_running_it_twice_changes_nothing_the_second_time(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    run(repo, input="y\n")
    before = {p.name: p.read_bytes() for p in (repo / ".devorch").glob("*.yaml")}
    again = run(repo)
    assert "이미 되어 있어" in again.output and "이미 승인된" in again.output
    assert before == {p.name: p.read_bytes() for p in (repo / ".devorch").glob("*.yaml")}


def test_not_a_git_repository_stops_before_creating_anything(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = run(plain)
    assert res.exit_code == 2 and "git" in res.output and not (plain / ".devorch").exists()


def test_a_repository_without_a_first_commit_is_refused_with_the_reason(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q", "-b", "main")
    res = run(empty)
    assert res.exit_code == 2 and "커밋" in res.output and not (empty / ".devorch").exists()


def test_missing_keys_are_listed_unless_it_is_only_a_connection_trial(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)
    assert "provider" in run(repo).output  # real use needs a key: say so
    other = repo.parent / "p2"
    other.mkdir()
    run_git(other, "init", "-q", "-b", "main")
    run_git(other, "config", "user.email", "t@example.com")
    run_git(other, "config", "user.name", "T")
    (other / "a.py").write_text("x = 1\n", encoding="utf-8")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-q", "-m", "i")
    trial = run(other, "--mock")
    assert "provider" not in trial.output.split("3) 정책 승인")[0].split("2) 점검")[1]
    assert "-m devorch mcp -p" in trial.output and "--mock" in trial.output


def test_write_mcp_json_creates_a_valid_file_and_never_overwrites_one(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)
    res = run(repo, "--write-mcp-json", "--mock")
    data = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
    srv = data["mcpServers"]["devorch"]
    assert res.exit_code == 0 and srv["command"] == sys.executable
    assert srv["args"] == ["-m", "devorch", "mcp", "-p", str(repo.resolve()), "--mock"]
    mine = '{"mcpServers": {"other": {"command": "x"}}}\n'
    (repo / ".mcp.json").write_text(mine, encoding="utf-8")
    again = run(repo, "--write-mcp-json")
    assert "이미 있어 건드리지 않았습니다" in again.output and (repo / ".mcp.json").read_text(encoding="utf-8") == mine


def test_the_real_registration_command_has_no_mock_flag(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)
    out = run(repo).output
    line = next(l for l in out.splitlines() if "claude mcp add" in l)
    assert "--mock" not in line and "API 키" in out
