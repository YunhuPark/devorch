"""The MCP server as a real child process over stdio (what Claude Code etc. launch), not in-memory.

This proves the transport and the process boundary work on this OS; it does NOT prove a particular
client application (Claude Code, Cursor) can connect — that still has to be tried in that application.
"""

import os
import subprocess
import sys

import pytest
import yaml
from gitutil import run_git
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from devorch.security import policy_guard
from devorch.service import finish_command

PYTEST_CMD = f"{sys.executable} -m pytest -q"
EXPECTED_TOOLS = {
    "devorch_inspect_project", "devorch_analyze_task", "devorch_analyze_risk", "devorch_analyze_impact",
    "devorch_get_diff", "devorch_request_review", "devorch_run_tests", "devorch_apply_changes",
}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    run_git(r, "config", "user.email", "t@example.com")
    run_git(r, "config", "user.name", "T")
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (r / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    d = r / ".devorch"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({"project": {"test_command": PYTEST_CMD}}), encoding="utf-8")
    (d / "policy.yaml").write_text(yaml.safe_dump({"allowed_commands": [PYTEST_CMD]}), encoding="utf-8")
    (d / ".gitignore").write_text("*\n", encoding="utf-8")
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "init")
    policy_guard.accept(r)  # a human ran `devorch policy accept`; without it the server's state-changing tools refuse to run
    return r


def server_params(repo):
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", "from devorch.cli.main import app; app()", "mcp", "-p", str(repo), "--mock"],
        env={"DEVORCH_KEY_FILE": os.environ["DEVORCH_KEY_FILE"]},  # the client passes almost no environment: without this the server would use ~/.devorch
    )


async def test_real_stdio_process_lists_tools_and_runs_the_full_low_risk_flow(repo):
    async with Client(server_params(repo)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == EXPECTED_TOOLS and not any("approve" in n for n in names)

        a = (await c.call_tool("devorch_analyze_task", {"task": "calc.py add 버그 수정"})).structured_content
        assert a["risk"]["level"] == "low"  # Korean text survived the process boundary
        tid = a["task_id"]

        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        assert (await c.call_tool("devorch_request_review", {"task_id": tid})).structured_content["status"] == "READY"
        assert (await c.call_tool("devorch_run_tests", {"task_id": tid})).structured_content["status"] == "passed"
        done = (await c.call_tool("devorch_apply_changes", {"task_id": tid})).structured_content

    assert done["status"] == "committed"
    assert run_git(repo, "branch", "--show-current").stdout.strip() == "main"  # the user's branch is untouched
    assert run_git(repo, "show", f"{done['branch']}:calc.py").stdout.strip() == "def add(a, b):\n    return a + b".strip()
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

    # What the user is handed after the commit comes out of the REAL server process too (a stale server once answered without it) and works.
    assert finish_command(repo, tid) in done["how_to_finish"] and done["merge_command"] == f"git merge {done['branch']}"
    fin = subprocess.run([sys.executable, "-m", "devorch", "finish", tid, "-p", str(repo), "--yes"], capture_output=True, text=True, encoding="utf-8")
    assert fin.returncode == 0, fin.stderr
    assert "calc.py" not in run_git(repo, "status", "--porcelain").stdout  # the agent's leftover edit is gone
    assert run_git(repo, "merge", "--no-edit", done["branch"], check=False).returncode == 0


async def test_real_stdio_high_risk_apply_is_refused_without_a_human_approval(repo):
    async with Client(server_params(repo)) as c:
        tid = (await c.call_tool("devorch_analyze_task", {"task": "로그인 JWT 인증 추가 calc.py"})).structured_content["task_id"]
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        assert (await c.call_tool("devorch_request_review", {"task_id": tid})).structured_content["status"] in ("READY", "READY_WITH_WARNINGS")
        assert (await c.call_tool("devorch_run_tests", {"task_id": tid})).structured_content["status"] == "passed"
        out = (await c.call_tool("devorch_apply_changes", {"task_id": tid})).structured_content
    assert out["status"] == "approval_required"
    # The test step leaves a branch at HEAD for its worktree, but no devorch branch may carry a commit main lacks.
    branches = run_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/devorch/").stdout.split()
    assert all(run_git(repo, "rev-list", "--count", f"main..{b}").stdout.strip() == "0" for b in branches)
    assert run_git(repo, "log", "-1", "--format=%s", "main").stdout.strip() == "init"


async def test_real_stdio_server_refuses_state_changing_tools_until_a_human_accepts_the_policy(repo):
    """What the agent actually receives: an error telling the human what to run, and read-only inspection still works."""
    (policy_guard.record_dir(repo) / policy_guard.RECORD).unlink()  # never accepted / record gone
    async with Client(server_params(repo)) as c:
        info = (await c.call_tool("devorch_inspect_project", {})).structured_content
        assert info["policy_accepted"] == "unaccepted" and "policy accept" in info["policy_how_to_accept"]
        res = await c.call_tool("devorch_analyze_task", {"task": "calc.py add 버그 수정"})
        text = " ".join(getattr(part, "text", "") for part in res.content)
        assert res.is_error and "policy accept" in text and "-p" in text
