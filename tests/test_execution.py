import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devorch.cli.main import app
from devorch.config import Policy
from devorch.core.execution.executor import ExecutionError, Executor
from devorch.core.execution.patch import PatchError, extract_diff, validate_diff
from devorch.core.execution.test_runner import detect_test_command, run_tests, safe_env
from devorch.core.orchestrator.engine import WorkflowEngine
from devorch.core.orchestrator.state import State
from devorch.git.manager import GitError, GitManager
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.security.secret_redactor import has_secret_literal

PYTEST_CMD = f"{sys.executable} -m pytest -q"

GOOD_DIFF = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""
BREAKING_DIFF = GOOD_DIFF.replace("return a + b", "return a * b")
WRONG_CONTEXT_DIFF = GOOD_DIFF.replace(" def add(a, b):", " def plus(a, b):")


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


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


def policy():
    return Policy(allowed_commands=[PYTEST_CMD])


def registry(claude_text, reviewer="[]"):
    reg = ProviderRegistry()
    reg.register(MockProvider("claude", [claude_text]))
    reg.register(MockProvider("openai", [reviewer]))
    reg.register(MockProvider("gemini", [reviewer]))
    return reg


def make_engine(repo, reg, executor, approver=None):
    return WorkflowEngine(
        repo, executor.policy, reg, approver=approver,
        apply_hook=executor.apply, test_hook=executor.test, commit_hook=executor.commit,
        context_provider=executor.file_context,
    )


def branches(repo):
    return git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/devorch/").splitlines()


def worktrees(repo):
    return git(repo, "worktree", "list").splitlines()


# ---- patch extraction / validation ---------------------------------------
def test_extract_diff_from_fence_and_raw():
    fenced = f"Here is the fix:\n```diff\n{GOOD_DIFF}```\nRationale: sign bug."
    assert extract_diff(fenced) == GOOD_DIFF
    assert extract_diff("intro\n" + GOOD_DIFF) == GOOD_DIFF
    assert extract_diff("no diff here") is None


def test_validate_accepts_normal_and_lists_paths():
    assert validate_diff(GOOD_DIFF) == ["calc.py"]


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/etc/passwd", "a/../../x", ".git/hooks/pre-commit", ".devorch/policy.yaml", "C:/Windows/x"],
)
def test_validate_rejects_unsafe_paths(path):
    diff = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(PatchError):
        validate_diff(diff)


def test_validate_rejects_binary_symlink_empty_and_huge():
    with pytest.raises(PatchError, match="바이너리"):
        validate_diff("diff --git a/x.png b/x.png\nGIT binary patch\nliteral 1\n")
    with pytest.raises(PatchError, match="심볼릭"):
        validate_diff("diff --git a/l b/l\nnew file mode 120000\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+/etc/passwd\n")
    with pytest.raises(PatchError, match="빈"):
        validate_diff("  \n")
    with pytest.raises(PatchError, match="너무 큼"):
        validate_diff("diff --git a/a b/a\n--- a/a\n+++ b/a\n" + "+" + "x" * 1_100_000 + "\n")


def test_secret_literal_detection():
    assert has_secret_literal("KEY = 'sk-abcdefghijklmnopqrstuv'")
    assert not has_secret_literal("token = request.token  # ordinary code")


# ---- git manager -----------------------------------------------------------
def test_ensure_ready_failures(tmp_path, repo):
    with pytest.raises(GitError, match="저장소가 아닙니다"):
        GitManager(tmp_path / "nothing").ensure_ready() if (tmp_path / "nothing").mkdir() is None else None
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=empty, check=True)
    with pytest.raises(GitError, match="커밋이 하나도"):
        GitManager(empty).ensure_ready()
    sub = repo / "sub"
    sub.mkdir()
    with pytest.raises(GitError, match="최상위"):
        GitManager(sub).ensure_ready()
    GitManager(repo).ensure_ready()


def test_branch_name_is_safe():
    assert GitManager.branch_name("ab12", "로그인 JWT; rm -rf /") == "devorch/ab12-jwt-rm-rf"
    assert GitManager.branch_name("ab12", "결제") == "devorch/ab12-task"


# ---- test runner -----------------------------------------------------------
def test_detect_test_command(tmp_path):
    assert detect_test_command(tmp_path) is None
    (tmp_path / "pom.xml").write_text("<project/>")
    assert detect_test_command(tmp_path) == "mvn test"
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert detect_test_command(tmp_path) == "npm test"


def test_detect_flat_pytest_layout(tmp_path):
    (tmp_path / "test_calc.py").write_text("def test_x(): pass\n")
    assert detect_test_command(tmp_path) == "pytest"


def test_safe_env_strips_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("MY_SECRET", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("CUSTOM_VAR", "x")
    monkeypatch.setenv("HARMLESS", "ok")
    env = safe_env({"CUSTOM_VAR"})
    assert {"OPENAI_API_KEY", "MY_SECRET", "GITHUB_TOKEN", "CUSTOM_VAR"}.isdisjoint(env) and env["HARMLESS"] == "ok"


async def test_runner_rejects_disallowed_and_handles_timeout(tmp_path):
    r = await run_tests("curl http://x", tmp_path, [PYTEST_CMD], 5)
    assert not r.ok and "거부" in r.reason
    (tmp_path / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(30)\n")
    r = await run_tests(PYTEST_CMD, tmp_path, [PYTEST_CMD], 1.5)
    assert not r.ok and "중단" in r.reason


# ---- executor end-to-end ---------------------------------------------------
async def test_full_pipeline_creates_tested_commit_on_branch_only(repo):
    ex = Executor(repo, policy(), test_command=PYTEST_CMD)
    claude = MockProvider("claude", [f"```diff\n{GOOD_DIFF}```\nfixed sign"])
    reg = ProviderRegistry()
    reg.register(claude)
    before_head = git(repo, "rev-parse", "HEAD")

    run = await make_engine(repo, reg, ex).run("calc.py add 버그 수정")

    assert run.state is State.COMPLETED, run.failure_reason
    # the AI saw the real file contents from HEAD
    assert "return a - b" in claude.calls[0].prompt and "=== calc.py ===" in claude.calls[0].prompt
    # result lives on a branch with a traceable commit
    assert run.branch in branches(repo) and run.commit
    msg = git(repo, "log", "-1", "--format=%B", run.branch)
    assert msg.startswith("fix: calc.py add") and f"DevOrch-Run: {run.id}" in msg
    assert git(repo, "show", f"{run.branch}:calc.py").endswith("return a + b")
    # the user's branch, HEAD and working tree are untouched; temp worktree is gone
    assert git(repo, "rev-parse", "HEAD") == before_head
    assert git(repo, "branch", "--show-current") == "main"
    assert (repo / "calc.py").read_text(encoding="utf-8").endswith("return a - b\n")
    assert len(worktrees(repo)) == 1
    # worktrees/ and audit.jsonl are ignored; only the ignore file itself is new
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == "?? .devorch/.gitignore"


async def test_secret_literal_file_is_not_sent_to_the_model(repo):
    (repo / "secrets.py").write_text("KEY = 'sk-abcdefghijklmnopqrstuvwx'\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add secrets")
    reg = registry("no diff")
    ex = Executor(repo, policy(), test_command=PYTEST_CMD)
    await make_engine(repo, reg, ex).run("secrets.py 와 calc.py 정리")
    prompt = reg.get("claude").calls[0].prompt
    assert "sk-abcdef" not in prompt and "secrets.py (credential" in prompt and "=== calc.py ===" in prompt


@pytest.mark.parametrize(
    "diff,needle",
    [
        ("no diff at all", "unified diff를 찾지 못함"),
        ("diff --git a/../x b/../x\n--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n", "허용되지 않는 경로"),
        (WRONG_CONTEXT_DIFF, "적용하지 못함"),
    ],
)
async def test_bad_diffs_fail_cleanly_and_leave_nothing_behind(repo, diff, needle):
    ex = Executor(repo, policy(), test_command=PYTEST_CMD)
    run = await make_engine(repo, registry(f"```diff\n{diff}\n```" if "diff --git" in diff else diff), ex).run("calc.py add 버그 수정")
    assert run.state is State.FAILED and needle in run.failure_reason
    assert branches(repo) == [] and len(worktrees(repo)) == 1 and run.commit is None
    assert (repo / "calc.py").read_text(encoding="utf-8").endswith("return a - b\n")


async def test_failing_tests_block_the_commit_and_keep_worktree_for_inspection(repo):
    ex = Executor(repo, policy(), test_command=PYTEST_CMD)
    run = await make_engine(repo, registry(BREAKING_DIFF), ex).run("calc.py add 버그 수정")
    assert run.state is State.FAILED and "테스트 실패" in run.failure_reason
    assert run.commit is None and git(repo, "rev-list", "--count", "main") == "1"
    assert run.worktree and Path(run.worktree).exists() and run.branch in branches(repo)
    assert any("worktree 보존" in n for n in run.notes)
    assert "passed" not in run.test_output or "failed" in run.test_output


async def test_no_test_command_fails_closed_unless_explicitly_skipped(repo):
    (repo / "test_calc.py").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "drop tests")
    run = await make_engine(repo, registry(GOOD_DIFF), Executor(repo, policy())).run("calc.py add 버그 수정")
    assert run.state is State.FAILED and "테스트 명령을 찾지 못함" in run.failure_reason

    run = await make_engine(repo, registry(GOOD_DIFF), Executor(repo, policy(), skip_tests=True)).run("calc.py add 버그 수정")
    assert run.state is State.COMPLETED and any("--no-tests" in n for n in run.notes)


async def test_nothing_is_applied_when_human_rejects(repo):
    ex = Executor(repo, policy(), test_command=PYTEST_CMD)
    run = await make_engine(repo, registry(GOOD_DIFF), ex, approver=lambda r: False).run("로그인 JWT 인증 추가 calc.py")
    assert run.state is State.REJECTED
    assert branches(repo) == [] and len(worktrees(repo)) == 1


async def test_tests_run_without_credentials_in_env(repo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "leak-me")
    monkeypatch.setenv("CUSTOM_VAR", "leak-me-too")
    (repo / "test_env.py").write_text(
        "import os\n\ndef test_env():\n    assert 'OPENAI_API_KEY' not in os.environ\n    assert 'CUSTOM_VAR' not in os.environ\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "env test")
    ex = Executor(repo, policy(), test_command=PYTEST_CMD, blocked_env={"CUSTOM_VAR"})
    run = await make_engine(repo, registry(GOOD_DIFF), ex).run("calc.py add 버그 수정")
    assert run.state is State.COMPLETED, run.test_summary + run.test_output


def test_executor_requires_a_git_repo(tmp_path):
    with pytest.raises(ExecutionError, match="저장소가 아닙니다"):
        Executor(tmp_path, policy()).ensure_ready()


# ---- CLI ---------------------------------------------------------------------
def test_cli_apply_refuses_non_git_dir_before_any_call(tmp_path):
    r = CliRunner().invoke(app, ["run", "버튼 텍스트 변경", "-p", str(tmp_path), "--mock", "--apply"])
    assert r.exit_code == 2 and "--apply 불가" in r.output


def test_cli_apply_with_mock_fails_closed_without_a_diff(repo):
    r = CliRunner().invoke(app, ["run", "calc.py add 버그 수정", "-p", str(repo), "--mock", "--apply", "--no-tests"])
    assert r.exit_code == 1 and "unified diff를 찾지 못함" in r.output
    assert branches(repo) == []
