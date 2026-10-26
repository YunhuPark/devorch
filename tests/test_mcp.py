import json
import sys

import pytest
from gitutil import run_git
from mcp.client import Client
from typer.testing import CliRunner

from devorch.cli import main as cli_main
from devorch.config import Policy
from devorch.core.execution.executor import Executor
from devorch.mcp_server import build_server
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane, ControlPlaneError
from devorch.store import Approval, TaskStore

PYTEST_CMD = f"{sys.executable} -m pytest -q"
FIX = "def add(a, b):\n    return a + b\n"
BREAK = "def add(a, b):\n    return a * b\n"
SEC_FINDING = json.dumps([{"severity": "high", "category": "security", "title": "권한 검증 누락", "file": "calc.py"}])
ARCH_WARN = json.dumps([{"severity": "medium", "category": "architecture", "title": "구조 개선 권장"}])
LOW_TASK = "calc.py add 버그 수정"
HIGH_TASK = "로그인 JWT 인증 추가 calc.py"


def git(repo, *args):
    return run_git(repo, *args).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (r / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def make_plane(repo, openai=("[]",), gemini=("[]",), max_retries=3, skip_tests=False):
    reg = ProviderRegistry()
    for name, resp in (("claude", ["x"]), ("openai", list(openai)), ("gemini", list(gemini))):
        reg.register(MockProvider(name, resp))
    policy = Policy(allowed_commands=[PYTEST_CMD], limits={"max_retries": max_retries})
    ex = Executor(repo, policy, test_command=PYTEST_CMD, skip_tests=skip_tests)
    return ControlPlane(repo, policy, {}, reg, ex)


def edit(repo, text=FIX):
    (repo / "calc.py").write_text(text, encoding="utf-8")


def branches(repo):
    return git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/devorch/").splitlines()


def nothing_committed(repo):
    """No devorch branch carries a commit that main doesn't have."""
    return all(git(repo, "rev-list", "--count", f"main..{b}") == "0" for b in branches(repo))


def approve(plane, task_id, ok=True):
    rec = plane.store.load(task_id)
    plane.store.save_approval(Approval(task_id=task_id, diff_sha=rec.diff_sha, approved=ok, via="test"))


# ---- happy path (LOW: no reviewers, no approval) -----------------------------
async def test_low_risk_full_flow_commits_to_branch_only(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(LOW_TASK)
    assert a["risk"]["level"] == "low" and a["required_reviews"] == [] and not a["requires_human_approval"]
    tid = a["task_id"]

    edit(repo)
    rv = await plane.request_review(tid)
    assert rv["status"] == "READY" and rv["next_step"].startswith("devorch_run_tests")
    t = await plane.run_tests(tid)
    assert t["status"] == "passed"
    out = await plane.apply_changes(tid)

    assert out["status"] == "committed" and out["branch"] in branches(repo)
    assert git(repo, "show", f"{out['branch']}:calc.py") == FIX.strip()
    assert git(repo, "branch", "--show-current") == "main"
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIX          # client's edit left in place
    assert git(repo, "diff", "--name-only") == "calc.py"                  # still an unstaged edit
    assert git(repo, "diff", "--cached", "--name-only") == ""             # nothing staged in the user's index
    assert (await plane.apply_changes(tid))["status"] == "already_committed"


# ---- diff capture --------------------------------------------------------------
def test_get_diff_includes_new_files_excludes_secrets_and_keeps_index_clean(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    edit(repo)
    (repo / "new_mod.py").write_text("X = 1\n", encoding="utf-8")
    (repo / ".env").write_text("DB_PASSWORD=hunter2\n", encoding="utf-8")
    (repo / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n", encoding="utf-8")
    d = plane.get_diff(tid)
    assert set(d["files"]) == {"calc.py", "new_mod.py"}
    assert "hunter2" not in d["diff"] and "PRIVATE KEY" not in d["diff"]
    assert git(repo, "diff", "--cached", "--name-only") == ""            # real index untouched


def test_empty_diff_and_secret_in_diff_are_refused(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    with pytest.raises(ControlPlaneError, match="변경 사항이 없습니다"):
        plane.get_diff(tid)
    edit(repo, "KEY = 'sk-abcdefghijklmnopqrstuvwx'\n")
    with pytest.raises(ControlPlaneError, match="credential"):
        plane.get_diff(tid)


def test_binary_change_is_refused(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    (repo / "logo.bin").write_bytes(bytes(range(256)) * 4)
    with pytest.raises(ControlPlaneError, match="바이너리"):
        plane.get_diff(tid)


# ---- HIGH risk: findings → fix → approval ---------------------------------------
async def test_high_risk_not_ready_blocks_everything_then_fix_then_human_approval(repo):
    plane = make_plane(repo, openai=[SEC_FINDING, "[]"], gemini=[ARCH_WARN, "[]"])
    a = plane.analyze_task(HIGH_TASK)
    tid = a["task_id"]
    assert a["risk"]["level"] == "high" and a["requires_human_approval"]
    assert {r["role"] for r in a["required_reviews"]} == {"security", "architecture"}

    edit(repo, BREAK)                                    # first (flawed) attempt
    rv = await plane.request_review(tid)
    assert rv["status"] == "NOT_READY" and rv["blocking"][0]["title"] == "권한 검증 누락"
    t = await plane.run_tests(tid)
    assert t["status"] == "blocked" and "NOT_READY" in t["reason"]       # nothing gets executed
    assert (await plane.apply_changes(tid))["status"] == "blocked"

    edit(repo, FIX)                                      # agent fixes it
    rv2 = await plane.request_review(tid)
    assert rv2["status"] == "READY_WITH_WARNINGS" or rv2["status"] == "READY"
    assert rv2["rounds_used"] == 2
    assert (await plane.run_tests(tid))["status"] == "passed"

    gate = await plane.apply_changes(tid)                # needs a human
    assert gate["status"] == "approval_required" and f"devorch approve {tid}" in gate["how_to_approve"]
    assert nothing_committed(repo)

    approve(plane, tid)
    out = await plane.apply_changes(tid)
    assert out["status"] == "committed"
    msg = git(repo, "log", "-1", "--format=%B", out["branch"])
    assert "Human-Approved: yes" in msg and f"DevOrch-Run: {tid}" in msg


async def test_approval_is_bound_to_the_exact_diff(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    edit(repo)
    await plane.request_review(tid)
    await plane.run_tests(tid)
    approve(plane, tid)

    # agent sneaks in an extra change after the human approved
    (repo / "calc.py").write_text(FIX + "\n# extra\n", encoding="utf-8")
    assert (await plane.request_review(tid))["status"] in ("READY", "READY_WITH_WARNINGS")
    assert (await plane.apply_changes(tid))["status"] == "blocked"        # not tested for the new diff
    assert (await plane.run_tests(tid))["status"] == "passed"
    gate = await plane.apply_changes(tid)
    assert gate["status"] == "approval_required" and "stale" in gate["reason"]
    assert nothing_committed(repo)


async def test_human_rejection_blocks(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    edit(repo)
    await plane.request_review(tid)
    await plane.run_tests(tid)
    approve(plane, tid, ok=False)
    out = await plane.apply_changes(tid)
    assert out["status"] == "blocked" and "거부" in out["reason"]
    assert branches(repo) == []                     # rejected work leaves no worktree/branch behind
    assert len(git(repo, "worktree", "list").splitlines()) == 1


# ---- tests are mandatory ----------------------------------------------------------
async def test_apply_requires_passing_tests(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    edit(repo)
    await plane.request_review(tid)
    out = await plane.apply_changes(tid)                  # tests never run
    assert out["status"] == "blocked" and "run_tests" in out["reason"]

    edit(repo, BREAK)
    await plane.request_review(tid)
    t = await plane.run_tests(tid)
    assert t["status"] == "failed" and "assert" in t["output_tail"]
    assert (await plane.apply_changes(tid))["status"] == "blocked"
    assert all(git(repo, "rev-list", "--count", f"main..{b}") == "0" for b in branches(repo))   # no commit anywhere
    assert git(repo, "rev-list", "--count", "main") == "1"


# ---- review mechanics --------------------------------------------------------------
async def test_identical_diff_review_is_cached_and_rounds_are_limited(repo):
    plane = make_plane(repo, max_retries=1)               # 2 review rounds max
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    openai = plane.providers.get("openai")

    edit(repo, FIX)
    await plane.request_review(tid)
    calls = len(openai.calls)
    again = await plane.request_review(tid)
    assert again["cached"] is True and len(openai.calls) == calls and again["rounds_used"] == 1

    edit(repo, FIX + "# a\n")
    assert (await plane.request_review(tid))["rounds_used"] == 2
    edit(repo, FIX + "# b\n")
    out = await plane.request_review(tid)
    assert out["status"] == "blocked" and "한도" in out["reason"]


async def test_failed_reviewer_call_is_not_a_pass_and_retry_is_not_cached(repo):
    class Boom(MockProvider):
        async def generate(self, request):
            self.calls.append(request)
            if len(self.calls) == 1:
                raise TimeoutError("upstream")
            return await MockProvider.generate(self, request)

    plane = make_plane(repo)
    plane.providers.register(Boom("openai", ["[]"]))
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    edit(repo)
    rv = await plane.request_review(tid)
    assert rv["status"] == "NOT_READY" and rv["missing_reviews"] == ["security"]
    assert (await plane.run_tests(tid))["status"] == "blocked"
    rv2 = await plane.request_review(tid)
    assert rv2["cached"] is False and rv2["status"] != "NOT_READY"


async def test_unavailable_provider_is_reported_before_spending(repo):
    plane = make_plane(repo)
    plane.providers = ProviderRegistry()
    plane.providers.register(MockProvider("claude", ["x"]))
    plane.provider_notes = {"openai": "환경변수 OPENAI_API_KEY 가 비어 있음"}
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    edit(repo)
    with pytest.raises(ControlPlaneError, match="OPENAI_API_KEY"):
        await plane.request_review(tid)


# ---- ids, persistence, committed branches -----------------------------------------
@pytest.mark.parametrize("bad", ["../etc/passwd", "ABCDEFGH", "abc", "a" * 9, "", "12345678/../x"])
def test_task_id_validation(repo, bad):
    plane = make_plane(repo)
    with pytest.raises(ControlPlaneError, match="task_id"):
        plane.get_risk(bad)
    with pytest.raises(ControlPlaneError, match="찾을 수 없습니다"):
        plane.get_risk("deadbeef")


async def test_state_survives_a_server_restart(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    edit(repo)
    await plane.request_review(tid)
    await plane.run_tests(tid)

    restarted = make_plane(repo)                          # new process, same project dir
    assert (await restarted.apply_changes(tid))["status"] == "committed"


async def test_committed_branch_is_never_deleted_when_the_diff_changes_later(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    edit(repo)
    await plane.request_review(tid)
    await plane.run_tests(tid)
    branch = (await plane.apply_changes(tid))["branch"]

    edit(repo, FIX + "# later\n")
    await plane.request_review(tid)
    assert branch in branches(repo)
    t = await plane.run_tests(tid)
    assert t["status"] == "passed"                  # a second attempt gets its own branch name
    assert branch in branches(repo) and len(branches(repo)) == 2
    assert git(repo, "show", f"{branch}:calc.py") == FIX.strip()   # the committed branch is untouched


async def test_docs_task_skips_tests_but_still_commits(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task("README 문서 업데이트")["task_id"]
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")
    await plane.request_review(tid)
    assert (await plane.run_tests(tid))["status"] == "skipped"
    out = await plane.apply_changes(tid)
    assert out["status"] == "committed"


def test_inspect_and_resources(repo):
    plane = make_plane(repo)
    info = plane.inspect_project()
    assert info["git"]["ready"] and "claude" in info["providers"]["available"]
    assert info["policy"]["max_review_rounds"] == 4
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    assert plane.task_summary(tid)["gates"]["approval"] == "not_required"


# ---- approval CLI --------------------------------------------------------------------
runner = CliRunner()


async def reviewed_and_tested_high_task(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    edit(repo)
    await plane.request_review(tid)
    await plane.run_tests(tid)
    return plane, tid


async def test_cli_approve_refuses_non_interactive_use(repo):
    plane, tid = await reviewed_and_tested_high_task(repo)
    r = runner.invoke(cli_main.app, ["approve", tid, "-p", str(repo)], input="y\n")   # stdin is not a TTY
    assert r.exit_code == 2 and "비대화형" in r.output
    assert plane.store.load_approval(tid) is None


async def test_cli_approve_records_decision_for_current_diff(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    plane, tid = await reviewed_and_tested_high_task(repo)
    prefix = plane.store.load(tid).diff_sha[:6]
    r = runner.invoke(cli_main.app, ["approve", tid, "-p", str(repo), "--diff"], input=f"{prefix}\n")
    assert r.exit_code == 0 and "승인됨" in r.output and "return a + b" in r.output
    ap = plane.store.load_approval(tid)
    assert ap.approved and ap.via == "cli" and ap.diff_sha == plane.store.load(tid).diff_sha
    assert (await plane.apply_changes(tid))["status"] == "committed"


async def test_cli_approve_no_means_rejection(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    plane, tid = await reviewed_and_tested_high_task(repo)
    r = runner.invoke(cli_main.app, ["approve", tid, "-p", str(repo)], input="n\n")
    assert "거부" in r.output and plane.store.load_approval(tid).approved is False


def test_cli_approve_not_yet_reviewed_and_unknown_id(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    plane = make_plane(repo)
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    r = runner.invoke(cli_main.app, ["approve", tid, "-p", str(repo)], input="y\n")
    assert r.exit_code == 1 and "아직 승인 단계가 아닙니다" in r.output
    assert TaskStore(repo).load_approval(tid) is None
    r = runner.invoke(cli_main.app, ["approve", "../x", "-p", str(repo)], input="y\n")
    assert r.exit_code == 2


def test_cli_tasks_lists_gate_state(repo):
    plane = make_plane(repo)
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    r = runner.invoke(cli_main.app, ["tasks", "-p", str(repo)])
    assert tid in r.output and "HIGH" in r.output and "approval=pending" in r.output


# ---- real MCP protocol (in-memory client ↔ server) ---------------------------------------
EXPECTED_TOOLS = {
    "devorch_inspect_project", "devorch_analyze_task", "devorch_analyze_risk", "devorch_analyze_impact",
    "devorch_get_diff", "devorch_request_review", "devorch_run_tests", "devorch_apply_changes",
}


async def test_mcp_protocol_tools_resources_and_error_messages(repo):
    plane = make_plane(repo)
    async with Client(build_server(plane)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == EXPECTED_TOOLS
        assert not any("approve" in n for n in names)          # agents get no approval tool

        res = await c.call_tool("devorch_analyze_task", {"task": HIGH_TASK})
        assert not res.is_error and res.structured_content["risk"]["level"] == "high"
        tid = res.structured_content["task_id"]

        err = await c.call_tool("devorch_analyze_risk", {"task_id": "../x"})
        assert err.is_error and "잘못된 task_id" in err.content[0].text   # the model sees WHY, not a crash

        blocked = await c.call_tool("devorch_run_tests", {"task_id": tid})
        assert not blocked.is_error and blocked.structured_content["status"] == "blocked"

        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        assert {"devorch://policy", "devorch://project"} <= uris
        task_res = await c.read_resource(f"devorch://task/{tid}")
        assert json.loads(task_res.contents[0].text)["gates"]["approval"] == "pending"


async def test_mcp_end_to_end_over_the_protocol(repo):
    plane = make_plane(repo)
    async with Client(build_server(plane)) as c:
        tid = (await c.call_tool("devorch_analyze_task", {"task": LOW_TASK})).structured_content["task_id"]
        edit(repo)
        assert (await c.call_tool("devorch_request_review", {"task_id": tid})).structured_content["status"] == "READY"
        assert (await c.call_tool("devorch_run_tests", {"task_id": tid})).structured_content["status"] == "passed"
        done = (await c.call_tool("devorch_apply_changes", {"task_id": tid})).structured_content
        assert done["status"] == "committed" and done["branch"] in branches(repo)


@pytest.mark.parametrize("answer", ["y", "yes", "", "wrong1", "ABCDEF"])
async def test_cli_approve_needs_the_diff_hash_so_a_reflexive_y_or_enter_records_nothing(repo, monkeypatch, answer):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    plane, tid = await reviewed_and_tested_high_task(repo)
    r = runner.invoke(cli_main.app, ["approve", tid, "-p", str(repo)], input=f"{answer}\n")
    assert r.exit_code == 1 and "아무것도 기록하지 않았습니다" in r.output
    assert plane.store.load_approval(tid) is None and not plane.store._path(tid, ".approval.json").exists()
    assert (await plane.apply_changes(tid))["status"] == "approval_required"


async def test_cli_approve_prompt_shows_the_exact_hash_prefix_to_type(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    plane, tid = await reviewed_and_tested_high_task(repo)
    sha = plane.store.load(tid).diff_sha
    r = runner.invoke(cli_main.app, ["approve", tid, "-p", str(repo)], input="\n")
    assert f"`{sha[:6]}`" in r.output and f"Diff {sha}" in r.output
