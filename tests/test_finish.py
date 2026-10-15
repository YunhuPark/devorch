"""After a task is committed, the agent's own edit is still sitting uncommitted in the user's working tree, and `git merge <branch>` then refuses
("local changes would be overwritten") — seen in the user's real IDE run. `devorch finish` cleans exactly that, and only that:
a file is put back to HEAD only when its content is identical to what DevOrch committed to the branch (so nothing can be lost); if the user
edited ANY of the files further, nothing is touched.
"""

import json
import sys

import pytest
from gitutil import run_git
from typer.testing import CliRunner

import devorch.cli.main as cli_main
from devorch.config import Policy
from devorch.core.execution.executor import Executor
from devorch.mcp_server import INSTRUCTIONS, build_server
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane, finish_command

PYTEST_CMD = f"{sys.executable} -m pytest -q"
FIX = "def add(a, b):\n    return a + b\n"
LOW_TASK = "calc.py add 버그 수정"


def git(repo, *args):
    return run_git(repo, *args).stdout.strip()


def dirty(repo):
    """`git status` lines, ignoring DevOrch's own state folder (this fixture never ran `devorch init`, so it is untracked here)."""
    return [l for l in git(repo, "status", "--porcelain").splitlines() if ".devorch" not in l]


def git_rc(repo, *args):
    return run_git(repo, *args, check=False)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        git(r, *a)
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (r / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def make_plane(repo):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 8))
    pol = Policy(allowed_commands=[PYTEST_CMD])
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command=PYTEST_CMD))


async def committed_task(repo, extra_files=None):
    """The agent edits calc.py (and optionally adds files), and the task goes all the way to a branch commit."""
    plane = make_plane(repo)
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    (repo / "calc.py").write_text(FIX, encoding="utf-8")
    for name, text in (extra_files or {}).items():
        (repo / name).write_text(text, encoding="utf-8")
    await plane.request_review(tid)
    await plane.run_tests(tid)
    out = await plane.apply_changes(tid)
    assert out["status"] == "committed", out
    return plane, tid, out


# ---- what the user is told -------------------------------------------------------------------------------------------------------
async def test_the_committed_response_hands_over_the_finish_and_merge_commands(repo):
    _, tid, out = await committed_task(repo)
    assert finish_command(repo, tid) in out["how_to_finish"]
    assert out["merge_command"] == f"git merge {out['branch']}"
    assert "devorch finish" not in INSTRUCTIONS and "how_to_finish" in INSTRUCTIONS and "Never run" in INSTRUCTIONS


async def test_the_agent_has_no_tool_to_finish(repo):
    tools = await build_server(make_plane(repo)).list_tools()
    names = {t.name for t in tools}
    assert len(names) == 8 and not any("finish" in n for n in names)


# ---- the problem and the cure ----------------------------------------------------------------------------------------------------
async def test_merge_is_blocked_before_finish_and_works_after(repo):
    plane, tid, out = await committed_task(repo)
    assert git_rc(repo, "merge", "--no-edit", out["branch"]).returncode != 0  # the reported friction, reproduced
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIX

    r = plane.finish(tid)
    assert r["status"] == "cleaned" and r["restored"] == ["calc.py"]
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"  # back to HEAD
    assert dirty(repo) == []

    assert git_rc(repo, "merge", "--no-edit", out["branch"]).returncode == 0
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIX  # the committed work arrived via merge, not lost


async def test_finish_leaves_the_branch_and_its_commit_untouched(repo):
    plane, tid, out = await committed_task(repo)
    before = git(repo, "rev-parse", out["branch"])
    plane.finish(tid)
    assert git(repo, "rev-parse", out["branch"]) == before and git(repo, "branch", "--show-current") == "main"


async def test_a_new_file_the_agent_added_is_removed_too(repo):
    plane, tid, out = await committed_task(repo, {"helpers.py": "VALUE = 1\n"})
    assert "helpers.py" in git(repo, "show", "--name-only", "--format=", out["branch"])
    assert git_rc(repo, "merge", "--no-edit", out["branch"]).returncode != 0  # untracked file would be overwritten
    r = plane.finish(tid)
    assert r["status"] == "cleaned" and sorted(r["restored"]) == ["calc.py", "helpers.py"]
    assert not (repo / "helpers.py").exists()
    assert git_rc(repo, "merge", "--no-edit", out["branch"]).returncode == 0
    assert (repo / "helpers.py").read_text(encoding="utf-8") == "VALUE = 1\n"


async def test_finishing_twice_has_nothing_left_to_do(repo):
    plane, tid, _ = await committed_task(repo)
    assert plane.finish(tid)["status"] == "cleaned"
    again = plane.finish(tid)
    assert again["status"] == "nothing_to_clean" and again["already_clean"] == ["calc.py"]


# ---- the safety rule: never throw away something the branch does not contain ---------------------------------------------------
async def test_a_file_the_user_edited_after_the_commit_is_never_touched(repo):
    plane, tid, _ = await committed_task(repo)
    mine = FIX + "\n# my own note\n"
    (repo / "calc.py").write_text(mine, encoding="utf-8")
    r = plane.finish(tid)
    assert r["status"] == "blocked" and r["changed_since_commit"] == ["calc.py"]
    assert (repo / "calc.py").read_text(encoding="utf-8") == mine


async def test_it_is_all_or_nothing_when_only_one_of_two_files_was_edited_further(repo):
    plane, tid, _ = await committed_task(repo, {"helpers.py": "VALUE = 1\n"})
    (repo / "helpers.py").write_text("VALUE = 2  # mine\n", encoding="utf-8")
    r = plane.finish(tid)
    assert r["status"] == "blocked" and r["changed_since_commit"] == ["helpers.py"]
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIX  # the clean one was NOT restored either
    assert (repo / "helpers.py").read_text(encoding="utf-8") == "VALUE = 2  # mine\n"


async def test_a_staged_new_file_is_unstaged_and_removed(repo):
    plane, tid, _ = await committed_task(repo, {"helpers.py": "VALUE = 1\n"})
    git(repo, "add", "helpers.py")
    assert plane.finish(tid)["status"] == "cleaned"
    assert not (repo / "helpers.py").exists() and "helpers.py" not in git(repo, "ls-files")


async def test_preview_changes_nothing(repo):
    plane, tid, _ = await committed_task(repo)
    p = plane.finish_preview(tid)
    assert p["status"] == "ready" and p["will_restore"] == ["calc.py"]
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIX


async def test_a_task_that_was_never_committed_cannot_be_finished(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    (repo / "calc.py").write_text(FIX, encoding="utf-8")
    await plane.request_review(tid)
    r = plane.finish(tid)
    assert r["status"] == "blocked" and "커밋되지 않았습니다" in r["reason"]
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIX  # the agent's work is still there


async def test_finish_is_written_to_the_audit_log(repo):
    plane, tid, _ = await committed_task(repo)
    plane.finish(tid)
    events = [json.loads(l) for l in (repo / ".devorch" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "mcp:finish" and e["data"]["restored"] == ["calc.py"] for e in events)


# ---- the command -------------------------------------------------------------------------------------------------------------------
async def test_cli_asks_first_and_does_nothing_on_no(repo, monkeypatch):
    _, tid, _ = await committed_task(repo)
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    res = CliRunner().invoke(cli_main.app, ["finish", tid, "-p", str(repo)], input="n\n")
    assert res.exit_code == 1 and "calc.py" in res.output
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIX


async def test_cli_cleans_on_yes(repo, monkeypatch):
    _, tid, out = await committed_task(repo)
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    res = CliRunner().invoke(cli_main.app, ["finish", tid, "-p", str(repo)], input="y\n")
    assert res.exit_code == 0 and out["branch"] in res.output
    assert dirty(repo) == []


async def test_cli_without_a_terminal_refuses_unless_told_with_yes(repo, monkeypatch):
    _, tid, _ = await committed_task(repo)
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)
    refused = CliRunner().invoke(cli_main.app, ["finish", tid, "-p", str(repo)])
    assert refused.exit_code == 2 and (repo / "calc.py").read_text(encoding="utf-8") == FIX
    forced = CliRunner().invoke(cli_main.app, ["finish", tid, "-p", str(repo), "--yes"])
    assert forced.exit_code == 0 and dirty(repo) == []


async def test_cli_reports_the_blocked_case_and_exits_nonzero(repo, monkeypatch):
    _, tid, _ = await committed_task(repo)
    (repo / "calc.py").write_text(FIX + "# mine\n", encoding="utf-8")
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    res = CliRunner().invoke(cli_main.app, ["finish", tid, "-p", str(repo), "--yes"])
    assert res.exit_code == 1 and "calc.py" in res.output
    assert (repo / "calc.py").read_text(encoding="utf-8").endswith("# mine\n")


def test_cli_unknown_task_is_one_line_not_a_traceback(repo):
    res = CliRunner().invoke(cli_main.app, ["finish", "deadbeef", "-p", str(repo), "--yes"])
    assert res.exit_code == 2 and "Traceback" not in res.output
