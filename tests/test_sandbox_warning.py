"""With sandbox mode "none" the agent's own tests run on this PC, and a hostile one can read the signing key and approve itself (reproduced 2026-10-15).
Not every user can run Docker, so by default this is a LOUD WARNING wherever a human approval is involved; policy.approval.require_sandbox_for_approval
turns it into a refusal for teams that want it."""

import sys

import pytest
from gitutil import run_git
from typer.testing import CliRunner

import devorch.cli.main as cli_main
from devorch.config import ApprovalPolicy, Policy, init_project, load_policy
from devorch.core.execution.executor import Executor
from devorch.core.execution.sandbox import SandboxConfig
from devorch.doctor import run_checks
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import SANDBOX_WARNING, ControlPlane

PYTEST_CMD = f"{sys.executable} -m pytest -q"
PAYMENT = "결제 로직 변경 calc.py"  # needs a human approval
SMALL = "calc.py add 버그 수정"  # does not
GOOD = "def add(a, b):\n    return a + b + 0\n"


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


def make_plane(repo, strict=False, sandbox=None):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 8))
    pol = Policy(allowed_commands=[PYTEST_CMD], approval=ApprovalPolicy(require_sandbox_for_approval=strict))
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command=PYTEST_CMD, sandbox=sandbox))


DOCKER = SandboxConfig(mode="docker", image="devorch-eval:py312")


# ---- the warning (default) ------------------------------------------------------------------------------------------------------
def test_a_change_that_needs_approval_carries_the_warning_when_tests_run_unsandboxed(repo):
    a = make_plane(repo).analyze_task(PAYMENT)
    assert a["requires_human_approval"] and a["sandbox_warning"] == SANDBOX_WARNING and "스스로 승인" in SANDBOX_WARNING


def test_no_warning_when_nothing_needs_approval_or_when_docker_is_configured(repo):
    assert make_plane(repo).analyze_task(SMALL)["sandbox_warning"] is None  # no approval involved
    assert make_plane(repo, sandbox=DOCKER).analyze_task(PAYMENT)["sandbox_warning"] is None  # tests would run in the container


async def test_approval_required_response_repeats_the_warning(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    await plane.request_review(a["task_id"])
    assert (await plane.run_tests(a["task_id"]))["status"] == "passed"  # default: still allowed
    r = await plane.apply_changes(a["task_id"])
    assert r["status"] == "approval_required" and r["sandbox_warning"] == SANDBOX_WARNING


async def test_the_human_sees_the_warning_on_the_approval_screen(repo, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    plane = make_plane(repo)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    await plane.request_review(a["task_id"])
    await plane.run_tests(a["task_id"])
    r = CliRunner().invoke(cli_main.app, ["approve", a["task_id"], "-p", str(repo)], input="n\n")
    assert "스스로 승인할 수 있습니다" in r.output and "거부" in r.output


# ---- the opt-in refusal --------------------------------------------------------------------------------------------------------------
async def test_strict_policy_refuses_to_run_the_tests_of_a_change_that_needs_approval(repo):
    plane = make_plane(repo, strict=True)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    await plane.request_review(a["task_id"])
    r = await plane.run_tests(a["task_id"])
    assert r["status"] == "blocked" and "샌드박스" in r["reason"] and "아무것도 실행하지 않았습니다" in r["reason"]
    rec = plane._load(a["task_id"])
    assert rec.run.worktree is None and rec.tested_sha is None  # nothing was created, nothing ran
    assert not (repo / ".devorch" / "worktrees").exists() or not any((repo / ".devorch" / "worktrees").iterdir())
    assert (await plane.apply_changes(a["task_id"]))["status"] == "blocked"  # and without tests nothing can land


async def test_strict_policy_leaves_changes_without_approval_alone(repo):
    plane = make_plane(repo, strict=True)
    a = plane.analyze_task(SMALL)
    assert not a["requires_human_approval"]
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    await plane.request_review(a["task_id"])
    assert (await plane.run_tests(a["task_id"]))["status"] == "passed"


def test_strict_policy_does_not_block_when_docker_is_configured(repo):
    plane = make_plane(repo, strict=True, sandbox=DOCKER)
    a = plane.analyze_task(PAYMENT)
    assert plane._sandbox_required_problem(plane._load(a["task_id"]).run.plan) is None


# ---- configuration and doctor --------------------------------------------------------------------------------------------------------------
def test_the_option_is_off_by_default_and_readable_from_policy_yaml(tmp_path):
    assert Policy().approval.require_sandbox_for_approval is False
    init_project(tmp_path)
    (tmp_path / ".devorch" / "policy.yaml").write_text("approval:\n  require_sandbox_for_approval: true\n", encoding="utf-8")
    assert load_policy(tmp_path).approval.require_sandbox_for_approval is True


def test_doctor_says_what_the_strict_option_does_without_a_sandbox(tmp_path):
    init_project(tmp_path)
    run_git(tmp_path, "init", "-q", "-b", "main")
    sandbox_check = lambda: next(c for c in run_checks(tmp_path, {}) if c.name == "샌드박스")  # noqa: E731
    assert sandbox_check().status == "info" and "스스로 승인" in sandbox_check().detail
    (tmp_path / ".devorch" / "policy.yaml").write_text("approval:\n  require_sandbox_for_approval: true\n", encoding="utf-8")
    c = sandbox_check()
    assert c.status == "warn" and "거부됩니다" in c.detail
