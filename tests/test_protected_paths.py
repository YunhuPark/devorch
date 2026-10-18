"""`protected_paths`: the owner declares which files are sensitive.

Found by running DevOrch with a real agent on a real repo (algo-pipeline, 2026-10-18): a change to the publish queue's state guard
(`src/db.py`, a dashboard route, a Telegram command) went through as LOW — no reviewers, no approval — because neither the task text nor the
file PATHS looked risky to DevOrch's keyword guesses. DevOrch cannot know what `src/db.py` holds; the owner can. Raise-only, like the rest of the
re-assessment: a declaration can raise the level, never lower it.
"""

import json
import sys

import pytest
import yaml
from gitutil import run_git
from test_doctor import by, project

from devorch.config import ConfigError, Policy, ProtectedPath, load_policy
from devorch.core.analyzer.diff_reassess import path_matches, protected_floor, reassess_from_diff
from devorch.core.execution.executor import Executor
from devorch.doctor import run_checks
from devorch.models import RiskLevel
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane
from devorch.store import Approval

PYTEST_CMD = f"{sys.executable} -m pytest -q"
TASK = "설명 문구 정리"  # nothing in the text suggests risk


# ---- matching --------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path,pattern,expected", [
    ("src/db.py", "src/db.py", True),
    ("src/db.py", "db.py", True),                 # no '/': the file NAME, anywhere
    ("a/b/db.py", "db.py", True),
    ("src/mydb.py", "db.py", False),              # whole name, not a substring
    ("src/billing/x.py", "src/billing/", True),   # trailing '/': a folder and everything below
    ("src/billing/deep/x.py", "src/billing/", True),
    ("src/billingx/x.py", "src/billing/", False),
    ("src/publish_queue.py", "src/*publish*", True),
    ("lib/db.py", "src/db.py", False),
    ("src\\db.py", "src/db.py", True),            # Windows separators in either
    ("src/db.py", ".\\src\\db.py", True),
])
def test_pattern_matching(path, pattern, expected):
    assert path_matches(path, pattern) is expected


def test_the_highest_declared_level_wins_and_only_its_files_are_named():
    level, hits = protected_floor(["src/ui.py", "src/db.py"], [("ui.py", "medium"), ("nothing.py", "critical"), ("db.py", "high")])  # lower level listed FIRST
    assert level is RiskLevel.HIGH and len(hits) == 1 and "src/db.py" in hits[0]
    assert protected_floor(["a.py"], []) == (None, [])
    assert protected_floor(["a.py"], [("b.py", "high")]) == (None, [])


# ---- validation ------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"pattern": ""}, {"pattern": "   "}, {"pattern": "/etc/passwd"}, {"pattern": "../outside"}, {"pattern": "a/../b"},
    {"pattern": "x.py", "min_level": "low"}, {"pattern": "x.py", "min_level": "urgent"},
])
def test_bad_declarations_are_rejected(bad):
    with pytest.raises(ValueError):
        ProtectedPath(**bad)


def test_policy_yaml_is_read_and_a_bad_entry_is_one_clear_config_error(tmp_path):
    d = tmp_path / ".devorch"
    d.mkdir()
    (d / "policy.yaml").write_text(yaml.safe_dump({"protected_paths": [{"pattern": "src/db.py", "min_level": "critical"}, {"pattern": "queue/"}]}), encoding="utf-8")
    got = load_policy(tmp_path).protected_paths
    assert [(p.pattern, p.min_level) for p in got] == [("src/db.py", "critical"), ("queue/", "high")]  # level defaults to high
    (d / "policy.yaml").write_text(yaml.safe_dump({"protected_paths": [{"pattern": "/abs", "min_level": "high"}]}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_policy(tmp_path)


# ---- the gates ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "src").mkdir(parents=True)
    for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        run_git(r, *a)
    (r / "src" / "store.py").write_text("def skip(row):\n    return row\n", encoding="utf-8")
    (r / "test_store.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "init")
    return r


def plane_for(repo, protected):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 8))
    pol = Policy(allowed_commands=[PYTEST_CMD], protected_paths=protected)
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command=PYTEST_CMD))


def edit_store(repo):
    (repo / "src" / "store.py").write_text("def skip(row):\n    return None\n", encoding="utf-8")


def events(repo):
    return [json.loads(l) for l in (repo / ".devorch" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


async def test_without_a_declaration_a_neutral_file_stays_low_the_gap_that_was_found(repo):
    plane = plane_for(repo, [])
    a = plane.analyze_task(TASK)
    edit_store(repo)
    await plane.request_review(a["task_id"])
    plan = plane.store.load(a["task_id"]).run.plan
    assert a["risk"]["level"] == "low" and not plan.requires_human_approval and not plan.review_steps


async def test_a_declared_path_raises_the_gates_reviewers_approval_and_says_why(repo):
    plane = plane_for(repo, [ProtectedPath(pattern="store.py", min_level="high")])
    a = plane.analyze_task(TASK)
    assert a["risk"]["level"] == "low"  # the text alone still says low; the DIFF is what raises it
    edit_store(repo)
    await plane.request_review(a["task_id"])
    rec = plane.store.load(a["task_id"])
    assert rec.run.risk.level is RiskLevel.HIGH and rec.run.plan.requires_human_approval and rec.run.plan.review_steps
    assert "보호 경로" in (rec.run.risk.floor_reason or "") and "src/store.py" in rec.run.risk.floor_reason
    ev = next(e for e in events(repo) if e["event"] == "mcp:diff_reassessed")
    assert any("보호 경로" in r for r in ev["data"]["reasons"]) and ev["data"]["approval"] == [False, True]


async def test_the_raised_change_stops_at_approval_and_commits_only_after_a_human_approves_that_diff(repo):
    plane = plane_for(repo, [ProtectedPath(pattern="src/store.py", min_level="high")])
    tid = plane.analyze_task(TASK)["task_id"]
    edit_store(repo)
    await plane.request_review(tid)
    await plane.run_tests(tid)
    stopped = await plane.apply_changes(tid)
    assert stopped["status"] == "approval_required"
    rec = plane.store.load(tid)
    plane.store.save_approval(Approval(task_id=tid, diff_sha=rec.diff_sha, approved=True, via="test"))
    assert (await plane.apply_changes(tid))["status"] == "committed"


async def test_a_file_that_is_not_declared_is_untouched_by_the_declaration(repo):
    plane = plane_for(repo, [ProtectedPath(pattern="somewhere_else.py", min_level="critical")])
    tid = plane.analyze_task(TASK)["task_id"]
    edit_store(repo)
    await plane.request_review(tid)
    rec = plane.store.load(tid)
    assert rec.run.risk.level is RiskLevel.LOW and not rec.run.plan.requires_human_approval
    assert not any(e["event"] == "mcp:diff_reassessed" for e in events(repo))


async def test_a_declaration_never_lowers_a_higher_level(repo):
    plane = plane_for(repo, [ProtectedPath(pattern="store.py", min_level="medium")])
    tid = plane.analyze_task("결제 로직 변경 store.py")["task_id"]  # payment keywords: already above medium
    run = plane.store.load(tid).run
    before = run.risk.level
    assert before.rank > RiskLevel.MEDIUM.rank
    _, risk, _, reasons = reassess_from_diff(run.analysis, run.risk, run.impact, ["src/store.py"], protected=[("store.py", "medium")])
    assert risk.level is before and not any("보호 경로" in r for r in reasons)


# ---- doctor ------------------------------------------------------------------------------------------------------------------------
def test_doctor_lists_the_declarations_and_flags_a_pattern_that_matches_no_tracked_file(tmp_path):
    r = project(tmp_path)  # tracked: a.py
    pol_path = r / ".devorch" / "policy.yaml"
    pol_path.write_text(yaml.safe_dump({"protected_paths": [{"pattern": "a.py", "min_level": "high"}]}), encoding="utf-8")
    ok = by(run_checks(r, env={}), "보호 경로")
    assert ok.status == "ok" and "a.py→high" in ok.detail
    pol_path.write_text(yaml.safe_dump({"protected_paths": [{"pattern": "a.py"}, {"pattern": "src/typo.py"}]}), encoding="utf-8")
    warn = by(run_checks(r, env={}), "보호 경로")
    assert warn.status == "warn" and "src/typo.py" in warn.detail and "a.py," not in warn.detail.split("일치하지 않는 패턴:")[1]
