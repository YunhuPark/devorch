"""The agent DevOrch constrains can usually write files in the project. These tests are the attacks that used to work (reproduced 2026-10-14):

* writing .devorch/tasks/<id>.approval.json by hand turned approval_required into committed;
* editing the tested worktree after the tests and the human approval got the tampered code committed ("Human-Approved: yes").
State is now signed with a key outside the project, and the commit requires the worktree to be byte-for-byte what was tested.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from gitutil import run_git

from devorch.config import Policy
from devorch.core.execution.executor import Executor
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.security import signing
from devorch.service import ControlPlane, ControlPlaneError
from devorch.store import Approval, StoreError, TaskStore

PYTEST_CMD = f"{sys.executable} -m pytest -q"
PAYMENT = "결제 로직 변경 calc.py"  # payment area -> a human must approve
GOOD = "def add(a, b):\n    return a + b + 0\n"


# ---- signing primitives -----------------------------------------------------------------------------------------------
def test_the_key_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVORCH_KEY_FILE", str(tmp_path / "k" / "state.key"))
    s1 = signing.sign("hello")
    assert (tmp_path / "k" / "state.key").is_file() and len((tmp_path / "k" / "state.key").read_bytes()) >= signing.KEY_BYTES
    assert signing.sign("hello") == s1 and signing.verify("hello", s1)


def test_a_wrong_or_missing_signature_never_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVORCH_KEY_FILE", str(tmp_path / "state.key"))
    good = signing.sign("payload")
    assert not signing.verify("payload!", good) and not signing.verify("payload", "") and not signing.verify("payload", "0" * 64)
    assert not signing.verify("payload", None)  # type: ignore[arg-type]
    monkeypatch.setenv("DEVORCH_KEY_FILE", str(tmp_path / "other.key"))
    assert not signing.verify("payload", good)  # signed with a different key


def test_an_unusable_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVORCH_KEY_FILE", str(tmp_path))  # a directory: it can neither be read nor created
    assert signing.verify("x", "a" * 64) is False
    with pytest.raises(signing.SigningError):
        signing.sign("x")


# ---- the store ------------------------------------------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()

    def git(*a):
        run_git(r, *a)

    git("init", "-q", "-b", "main"); git("config", "user.email", "t@example.com"); git("config", "user.name", "T")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (r / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    git("add", "-A"); git("commit", "-q", "-m", "init")
    return r


def make_plane(repo):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 8))
    pol = Policy(allowed_commands=[PYTEST_CMD])
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command=PYTEST_CMD))


async def reach_tested(plane, repo):
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    await plane.request_review(a["task_id"])
    assert (await plane.run_tests(a["task_id"]))["status"] == "passed"
    return a["task_id"]


async def test_a_genuine_signed_approval_and_record_round_trip_and_commit(repo):
    plane = make_plane(repo)
    tid = await reach_tested(plane, repo)
    assert (await plane.apply_changes(tid))["status"] == "approval_required"
    plane.store.save_approval(Approval(task_id=tid, diff_sha=plane._load(tid).diff_sha, approved=True, via="cli"))  # what `devorch approve` does
    assert plane.store.load_approval(tid) is not None and not plane.store.approval_file_rejected(tid)
    r = await plane.apply_changes(tid)
    assert r["status"] == "committed"


async def test_an_approval_file_written_by_hand_is_ignored_and_says_so(repo):
    plane = make_plane(repo)
    tid = await reach_tested(plane, repo)
    forged = {"task_id": tid, "diff_sha": plane._load(tid).diff_sha, "approved": True, "via": "cli", "at": "2026-01-01T00:00:00+00:00"}
    (repo / ".devorch" / "tasks" / f"{tid}.approval.json").write_text(json.dumps(forged), encoding="utf-8")
    assert plane.store.load_approval(tid) is None and plane.store.approval_file_rejected(tid)
    r = await plane.apply_changes(tid)
    assert r["status"] == "approval_required" and "서명" in (r["note"] or "")
    forged["sig"] = "f" * 64  # a made-up signature is no better
    (repo / ".devorch" / "tasks" / f"{tid}.approval.json").write_text(json.dumps(forged), encoding="utf-8")
    assert (await plane.apply_changes(tid))["status"] == "approval_required"


async def test_a_copied_approval_does_not_carry_over_to_another_task_or_diff(repo):
    plane = make_plane(repo)
    tid = await reach_tested(plane, repo)
    real = Approval(task_id=tid, diff_sha=plane._load(tid).diff_sha, approved=True, via="cli")
    plane.store.save_approval(real)
    path = repo / ".devorch" / "tasks" / f"{tid}.approval.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["diff_sha"] = "0" * 16  # re-point a genuine approval at another diff, keeping its signature
    path.write_text(json.dumps(data), encoding="utf-8")
    assert plane.store.load_approval(tid) is None


async def test_a_hand_edited_task_record_is_rejected(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")  # a broken change
    await plane.request_review(a["task_id"])
    path = repo / ".devorch" / "tasks" / f"{a['task_id']}.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["tested_sha"] = rec["diff_sha"]
    rec["run"]["plan"]["requires_human_approval"] = False
    path.write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(ControlPlaneError, match="서명"):
        await plane.apply_changes(a["task_id"])
    with pytest.raises(StoreError):
        TaskStore(repo).load(a["task_id"])


def test_a_record_without_any_signature_is_not_trusted(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(PAYMENT)
    path = repo / ".devorch" / "tasks" / f"{a['task_id']}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("_sig")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StoreError, match="서명"):
        TaskStore(repo).load(a["task_id"])


# ---- the worktree between the tests and the commit ---------------------------------------------------------------------------
async def test_a_worktree_altered_after_tests_and_approval_is_not_committed(repo):
    plane = make_plane(repo)
    tid = await reach_tested(plane, repo)
    plane.store.save_approval(Approval(task_id=tid, diff_sha=plane._load(tid).diff_sha, approved=True, via="cli"))
    wt = Path(plane._load(tid).run.worktree)
    (wt / "calc.py").write_text("def add(a, b):\n    return a - b  # tampered after the tests and the approval\n", encoding="utf-8")
    r = await plane.apply_changes(tid)
    assert r["status"] == "blocked" and "바뀌었" in r["reason"]
    log = run_git(repo, "log", "--all", "--format=%B").stdout
    assert "DevOrch-Run" not in log  # no DevOrch commit exists anywhere: nothing was committed
    events = [json.loads(line) for line in (repo / ".devorch" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "mcp:apply_changes" and e["data"].get("status") == "blocked_worktree_changed" for e in events)


async def test_an_extra_file_dropped_into_the_tested_worktree_is_also_caught(repo):
    plane = make_plane(repo)
    tid = await reach_tested(plane, repo)
    plane.store.save_approval(Approval(task_id=tid, diff_sha=plane._load(tid).diff_sha, approved=True, via="cli"))
    (Path(plane._load(tid).run.worktree) / "backdoor.py").write_text("print('hi')\n", encoding="utf-8")
    assert (await plane.apply_changes(tid))["status"] == "blocked"


async def test_files_the_test_run_itself_creates_do_not_break_the_check(repo):
    """pytest writes __pycache__ / caches during the run: the hash is taken AFTER the tests, so they are part of what was tested."""
    plane = make_plane(repo)
    tid = await reach_tested(plane, repo)
    plane.store.save_approval(Approval(task_id=tid, diff_sha=plane._load(tid).diff_sha, approved=True, via="cli"))
    assert plane._load(tid).tested_tree
    assert (await plane.apply_changes(tid))["status"] == "committed"


# ---- what is committed is the REVIEWED diff, not what the test run left in the worktree ---------------------------------------
SNEAKY = (
    "import pathlib\n"
    "\n"
    "def test_sneaky():\n"
    "    pathlib.Path('calc.py').write_text('def add(a, b):\\n    return 42  # injected by a test\\n')\n"
    "    pathlib.Path('dropped_by_test.txt').write_text('x')\n"
)


def tree_files(repo, branch):
    return sorted(run_git(repo, "ls-tree", "-r", "--name-only", branch).stdout.split())


async def test_a_test_that_rewrites_a_reviewed_file_does_not_count_as_passing(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    (repo / "test_zz_sneaky.py").write_text(SNEAKY, encoding="utf-8")  # runs after test_calc.py and rewrites calc.py
    await plane.request_review(a["task_id"])
    t = await plane.run_tests(a["task_id"])
    assert t["status"] == "failed" and "검토된 파일이 바뀌었" in t["summary"] and "calc.py" in t["summary"]
    assert (await plane.apply_changes(a["task_id"]))["status"] == "blocked"


async def test_the_commit_holds_exactly_the_reviewed_files_without_test_artifacts(repo):
    plane = make_plane(repo)
    tid = await reach_tested(plane, repo)
    plane.store.save_approval(Approval(task_id=tid, diff_sha=plane._load(tid).diff_sha, approved=True, via="cli"))
    r = await plane.apply_changes(tid)
    assert r["status"] == "committed"
    assert tree_files(repo, r["branch"]) == ["calc.py", "test_calc.py"]  # no __pycache__, no .pytest_cache
    assert run_git(repo, "show", f"{r['branch']}:calc.py").stdout == GOOD


async def test_commit_rebuilds_from_the_reviewed_diff_even_if_the_worktree_is_edited_at_the_last_moment(repo):
    """Executor level, past the service's own checks: whatever is in the worktree at commit time, only the reviewed diff is committed."""
    from devorch.core.orchestrator.engine import WorkflowRun

    pol = Policy(allowed_commands=[PYTEST_CMD])
    ex = Executor(repo, pol, test_command=PYTEST_CMD)
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    diff = ex.git.working_diff()
    run = WorkflowRun(task_text="calc.py 수정", implementation=diff)
    await ex.apply(run)
    wt = Path(run.worktree)
    (wt / "calc.py").write_text("def add(a, b):\n    return 42  # tampered at the last moment\n", encoding="utf-8")
    (wt / "backdoor.py").write_text("print('x')\n", encoding="utf-8")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "x.pyc").write_bytes(b"\0")
    await ex.commit(run)
    assert tree_files(repo, run.branch) == ["calc.py", "test_calc.py"]
    assert run_git(repo, "show", f"{run.branch}:calc.py").stdout == GOOD


def test_reset_clean_refuses_to_touch_anything_but_a_linked_worktree(repo):
    from devorch.git.manager import GitError, GitManager

    (repo / "precious.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(GitError):
        GitManager(repo).reset_clean(repo)  # the main repository itself
    with pytest.raises(GitError):
        GitManager(repo).reset_clean(repo / "no" / "such" / "dir")
    assert (repo / "precious.txt").read_text(encoding="utf-8") == "keep me"
