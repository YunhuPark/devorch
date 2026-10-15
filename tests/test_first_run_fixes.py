"""Found by running the REAL Claude Code client against a clone of the playground (2026-10-15):
1. the agent told the user to run `devorch approve <id>`: on one Windows setup the `devorch` launcher does not run, and from any other folder
   the task is not found without -p;
2. an older project's `.devorch/.gitignore` did not list audit.head / the lock file, so `git status` showed them as untracked.
"""

import os
import sys

import pytest
from gitutil import run_git
from typer.testing import CliRunner

import devorch.cli.main as cli_main
from devorch.audit import AuditLogger
from devorch.config import GITIGNORE_ENTRIES, Policy, ensure_gitignore, init_project
from devorch.core.execution.executor import Executor
from devorch.mcp_server import INSTRUCTIONS
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane, approve_command

PYTEST_CMD = f"{sys.executable} -m pytest -q"
PAYMENT = "결제 로직 변경 calc.py"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        run_git(r, *a)
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (r / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    run_git(r, "add", "-A"); run_git(r, "commit", "-q", "-m", "init")
    return r


def make_plane(repo):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 8))
    pol = Policy(allowed_commands=[PYTEST_CMD])
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command=PYTEST_CMD))


# ---- 1. the approval command the user is given ----------------------------------------------------------------------------------
def test_the_approve_command_names_this_interpreter_the_task_and_the_project(tmp_path):
    cmd = approve_command(tmp_path, "a1b2c3d4")
    assert sys.executable in cmd and "-m devorch approve a1b2c3d4" in cmd and f'-p "{tmp_path}"' in cmd
    assert cmd.startswith("& ") == (os.name == "nt")  # PowerShell needs the call operator for a quoted path


async def test_the_approval_required_response_hands_over_that_exact_command(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    await plane.request_review(a["task_id"])
    await plane.run_tests(a["task_id"])
    r = await plane.apply_changes(a["task_id"])
    assert r["status"] == "approval_required" and approve_command(repo, a["task_id"]) in r["how_to_approve"]
    assert "에이전트는 승인할 수 없습니다" in r["how_to_approve"]


def test_the_agent_is_told_to_pass_that_command_on_verbatim():
    assert "how_to_approve" in INSTRUCTIONS and "EXACTLY" in INSTRUCTIONS


async def test_that_command_finds_the_task_from_any_folder_while_the_bare_one_does_not(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    plane = make_plane(repo)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    await plane.request_review(a["task_id"])
    await plane.run_tests(a["task_id"])
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the user's terminal is not inside the project folder
    with_p = CliRunner().invoke(cli_main.app, ["approve", a["task_id"], "-p", str(repo)], input="n\n")
    assert with_p.exit_code == 0 and "거부로 기록" in with_p.output
    bare = CliRunner().invoke(cli_main.app, ["approve", a["task_id"]], input="n\n")  # what the agent used to say
    assert bare.exit_code != 0 and "거부로 기록" not in bare.output


# ---- 2. .devorch/.gitignore heals itself -----------------------------------------------------------------------------------------------
def test_an_old_gitignore_gets_the_missing_entries_once(tmp_path):
    d = tmp_path / ".devorch"
    d.mkdir()
    (d / ".gitignore").write_text("audit.jsonl\nruns/\n", encoding="utf-8")  # what projects initialised earlier have
    ensure_gitignore(d)
    ensure_gitignore(d)
    lines = (d / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert all(e in lines for e in GITIGNORE_ENTRIES) and len(lines) == len(set(lines))
    assert lines[:2] == ["audit.jsonl", "runs/"]  # nothing the user had was changed or reordered


def test_init_writes_every_entry(tmp_path):
    created = init_project(tmp_path)
    assert (tmp_path / ".devorch" / ".gitignore") in created
    assert set(GITIGNORE_ENTRIES) <= set((tmp_path / ".devorch" / ".gitignore").read_text(encoding="utf-8").splitlines())


def test_a_user_added_ignore_line_is_kept_and_a_read_only_file_is_not_an_error(tmp_path):
    d = tmp_path / ".devorch"
    d.mkdir()
    (d / ".gitignore").write_text("my-own-line\n", encoding="utf-8")
    ensure_gitignore(d)
    assert "my-own-line" in (d / ".gitignore").read_text(encoding="utf-8")
    ensure_gitignore(tmp_path / "does" / "not" / "exist" / ".devorch")  # must not raise


def test_git_stops_showing_the_new_state_files_once_the_logger_has_run(repo):
    d = repo / ".devorch"
    d.mkdir()
    (d / ".gitignore").write_text("audit.jsonl\nruns/\n", encoding="utf-8")
    run_git(repo, "add", "-f", ".devorch/.gitignore"); run_git(repo, "commit", "-q", "-m", "devorch ignore file")
    AuditLogger(d / "audit.jsonl").log("r1", "e", {"a": 1})  # writes audit.jsonl, audit.head and the lock file
    untracked = [ln for ln in run_git(repo, "status", "--porcelain").stdout.splitlines() if ln.startswith("??")]
    assert not any("audit.head" in ln or "audit.jsonl.lock" in ln for ln in untracked), untracked
