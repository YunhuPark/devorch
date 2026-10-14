"""The gates (tests, reviewers, approval) must not rest on the task DESCRIPTION alone: it is written by the agent making the change.

Reproduced 2026-10-14: "update the documentation wording" + a diff that makes check_password() always return True was classified docs / LOW,
needed no tests, no reviewers, no approval — and was committed. The diff is now re-assessed (raise-only) once it exists.
"""

import json
import subprocess
import sys

import pytest

from devorch.config import Policy
from devorch.core.analyzer.diff_reassess import is_doc_file, reassess_from_diff
from devorch.core.analyzer.risk_engine import assess_risk
from devorch.core.analyzer.task_analyzer import analyze_task
from devorch.core.execution.executor import Executor
from devorch.models import ImpactReport, RiskLevel, TaskType
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane

PYTEST_CMD = f"{sys.executable} -m pytest -q"
DOCS_TASK = "update the documentation wording in README"


def base(task):
    a = analyze_task(task)
    i = ImpactReport()
    return a, assess_risk(a, i), i


# ---- the pure rules --------------------------------------------------------------------------------------------------
def test_a_docs_task_whose_diff_touches_code_becomes_a_code_change():
    a, r, i = base(DOCS_TASK)
    assert a.task_type is TaskType.DOCS
    na, nr, ni, why = reassess_from_diff(a, r, i, ["README.md", "src/app/render.py"])
    assert na.task_type is TaskType.CHANGE and any("코드 변경으로 취급" in x for x in why)
    assert "src/app/render.py" in ni.direct_files


@pytest.mark.parametrize("files", [["README.md"], ["docs/guide.rst", "CHANGELOG", "notes.txt", "LICENSE"], ["docs/a.md", "docs/b.adoc"]])
def test_a_real_docs_only_diff_is_left_alone(files):
    a, r, i = base(DOCS_TASK)
    assert reassess_from_diff(a, r, i, files)[3] == []
    assert all(is_doc_file(f) for f in files)


def test_a_docs_directory_does_not_make_a_python_file_documentation():
    assert not is_doc_file("docs/conf.py") and not is_doc_file("docs/_static/app.js") and not is_doc_file("Makefile")


def test_a_security_looking_path_raises_the_impact_and_never_lowers_anything():
    a, r, i = base("fix a typo in a message")
    na, nr, ni, why = reassess_from_diff(a, r, i, ["src/app/auth/passwords.py"])
    assert ni.security_impact and nr.score > r.score and why
    # idempotent: reassessing the already-raised state changes nothing
    assert reassess_from_diff(na, nr, ni, ["src/app/auth/passwords.py"])[3] == []
    # never lowered: a CRITICAL task stays CRITICAL against a trivial diff
    crit = r.model_copy(update={"level": RiskLevel.CRITICAL, "floor_reason": "set by the user"})
    out = reassess_from_diff(a, crit, i, ["README.md"])
    assert out[1] is crit and out[3] == []


# ---- through the real service ---------------------------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)

    r = tmp_path / "proj"
    r.mkdir()
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@example.com"); git("config", "user.name", "T")
    (r / "README.md").write_text("# demo\n", encoding="utf-8")
    (r / "auth.py").write_text("def check_password(supplied, stored):\n    return supplied == stored\n", encoding="utf-8")
    (r / "test_auth.py").write_text(
        "from auth import check_password\n\n\ndef test_accepts_the_right_password():\n    assert check_password('a', 'a')\n\n\n"
        "def test_rejects_a_wrong_password():\n    assert not check_password('a', 'b')\n", encoding="utf-8",
    )
    git("add", "-A"); git("commit", "-q", "-m", "init")
    return r


def make_plane(repo):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 6))
    pol = Policy(allowed_commands=[PYTEST_CMD])
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command=PYTEST_CMD))


async def test_a_mislabelled_docs_task_cannot_commit_a_broken_auth_check_without_tests(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(DOCS_TASK)
    assert a["task_type"] == "docs" and a["run_tests"] is False and a["required_reviews"] == []  # what the description alone gives
    (repo / "auth.py").write_text("def check_password(supplied, stored):\n    return True\n", encoding="utf-8")

    r = await plane.request_review(a["task_id"])
    assert any(x.startswith("security:") for x in r["reviewers"])  # a security reviewer is now required
    blocked = await plane.apply_changes(a["task_id"])
    assert blocked["status"] == "blocked" and "테스트" in blocked["reason"]  # tests are now required before anything lands
    t = await plane.run_tests(a["task_id"])
    assert t["status"] == "failed"  # the project's own tests catch the broken check
    still = await plane.apply_changes(a["task_id"])
    assert still["status"] == "blocked"

    events = [json.loads(line) for line in (repo / ".devorch" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    re = next(e for e in events if e["event"] == "mcp:diff_reassessed")
    assert re["data"]["run_tests"] == [False, True] and re["data"]["reasons"]


async def test_a_genuine_docs_only_change_still_needs_no_tests_and_no_reviewers(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(DOCS_TASK)
    (repo / "README.md").write_text("# demo\n\nNow with a clearer sentence.\n", encoding="utf-8")
    r = await plane.request_review(a["task_id"])
    assert r["reviewers"] == [] and r["status"] == "READY"
    assert (await plane.run_tests(a["task_id"]))["status"] == "skipped"
    assert (await plane.apply_changes(a["task_id"]))["status"] == "committed"


async def test_preview_review_shows_the_reviewers_of_the_raised_plan(repo):
    plane = make_plane(repo)
    a = plane.analyze_task(DOCS_TASK)
    (repo / "auth.py").write_text("def check_password(supplied, stored):\n    return True\n", encoding="utf-8")
    prev = plane.preview_review(a["task_id"])
    assert any(x.startswith("security:") for x in prev["reviewers"])
