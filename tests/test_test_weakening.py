"""Changes that WEAKEN the tests are spotted from the diff's lines (core/analyzer/test_weakening.py).

An agent whose change makes a test fail has a cheap way out: delete the test, skip it, or make its assertion trivially true — and the test step then
passes. The path-based re-assessment cannot see that; this reads the changed lines of test files. Rules were fixed before they were measured on real history.
"""

import json
import sys

import pytest
from gitutil import run_git

from devorch.config import Policy
from devorch.core.analyzer.test_weakening import detect, floor_for, is_test_file
from devorch.core.execution.executor import Executor
from devorch.models import RiskLevel
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane


def mk(path, removed=(), added=(), deleted=False):
    """A unified diff for one file with the given removed/added lines (context is irrelevant to the detector)."""
    head = f"diff --git a/{path} b/{path}\n" + ("deleted file mode 100644\n" if deleted else "") + f"index 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
    body = f"@@ -1,{len(removed)} +1,{len(added)} @@\n" + "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return head + body


def kinds(diff):
    return {s.kind for s in detect(diff)}


# ---- each signal -------------------------------------------------------------------------------------------------------------------
def test_deleting_a_test_file_is_flagged_and_it_also_removes_its_tests():
    d = mk("tests/test_a.py", removed=["def test_one():", "    assert f() == 1"], deleted=True)
    assert kinds(d) == {"deleted_test_file", "tests_removed"} and floor_for(detect(d)) is RiskLevel.HIGH


def test_removing_a_test_function_is_flagged():
    d = mk("tests/test_a.py", removed=["def test_one():", "    assert f() == 1"])
    assert kinds(d) == {"tests_removed"} and floor_for(detect(d)) is RiskLevel.MEDIUM


@pytest.mark.parametrize("marker", [
    "@pytest.mark.skip(reason='x')", "@pytest.mark.skipif(True, reason='x')", "@pytest.mark.xfail", "    pytest.skip('later')",
    "@unittest.skip('x')", "it.skip('works', () => {})", "xit('works', () => {})", "describe.skip('suite', () => {})",
    "@Disabled", "@Ignore", "    t.Skip(\"later\")", "#[ignore]",
])
def test_adding_a_skip_marker_is_flagged(marker):
    assert kinds(mk("tests/test_a.py", added=[marker])) == {"skip_added"}


@pytest.mark.parametrize("line", [
    "    assert True", "    assert 1", "    assert not False", "    self.assertTrue(True)",
    "    expect(true).toBe(true)", "    expect(1).toEqual(1)", "    expect(true).toBeTruthy()",
])
def test_adding_an_always_true_assertion_is_flagged(line):
    assert kinds(mk("tests/test_a.py", added=[line])) == {"trivial_assert_added"}


@pytest.mark.parametrize("line", ["    assert flag is True", "    assert x == 1", "    assert result", "    expect(value).toBe(true)"])
def test_a_real_assertion_is_not_called_trivial(line):
    assert kinds(mk("tests/test_a.py", added=[line])) == set()


def test_removing_two_assertions_inside_a_test_that_stays_is_flagged():
    d = mk("tests/test_a.py", removed=["    assert a == 1", "    assert b == 2"], added=["    assert a == 1"])
    d2 = mk("tests/test_a.py", removed=["    assert a == 1", "    assert b == 2", "    assert c == 3"], added=["    assert a == 1"])
    assert kinds(d) == set()  # net one: not enough
    assert kinds(d2) == {"asserts_removed"}


def test_the_assertion_drop_that_comes_with_a_removed_test_is_not_counted_twice():
    d = mk("tests/test_a.py", removed=["def test_one():", "    assert a == 1", "    assert b == 2", "    assert c == 3"])
    assert kinds(d) == {"tests_removed"}


# ---- what must NOT be flagged (false-positive guards) ----------------------------------------------------------------------------
def test_renaming_a_test_nets_to_zero():
    assert kinds(mk("tests/test_a.py", removed=["def test_old_name():"], added=["def test_new_name():"])) == set()


def test_moving_a_test_to_another_file_nets_to_zero():
    d = mk("tests/test_a.py", removed=["def test_one():", "    assert f() == 1"]) + mk("tests/test_b.py", added=["def test_one():", "    assert f() == 1"])
    assert kinds(d) == set()


def test_adding_tests_and_assertions_is_never_flagged():
    assert kinds(mk("tests/test_a.py", added=["def test_new():", "    assert f() == 2", "    assert g() == 3"])) == set()


def test_removing_a_skip_marker_is_strengthening_not_weakening():
    assert kinds(mk("tests/test_a.py", removed=["@pytest.mark.skip"])) == set()


def test_only_test_files_are_looked_at():
    d = mk("src/calc.py", removed=["def test_helper():", "    assert x", "    assert y", "    assert z"], added=["@pytest.mark.skip", "assert True"])
    assert kinds(d) == set()
    assert is_test_file("tests/x.py") and is_test_file("test_calc.py") and is_test_file("web/a.test.ts") and not is_test_file("src/db.py")


def test_diff_headers_and_a_pure_rename_are_not_content():
    rename = "diff --git a/tests/test_a.py b/tests/test_b.py\nsimilarity index 100%\nrename from tests/test_a.py\nrename to tests/test_b.py\n"
    assert kinds(rename) == set() and kinds("") == set()


# ---- languages ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path,line", [
    ("web/cart.test.ts", "  it('adds an item', () => {"), ("web/cart.spec.js", "test('adds an item', async () => {"),
    ("src/test/java/CartTest.java", "    @Test"), ("pkg/cart_test.go", "func TestAdd(t *testing.T) {"), ("tests/cart.rs", "#[test]"),
])
def test_removing_a_test_is_recognised_in_other_languages(path, line):
    assert kinds(mk(path, removed=[line])) == {"tests_removed"}


# ---- levels ---------------------------------------------------------------------------------------------------------------------------
def test_one_kind_is_medium_two_kinds_are_high_none_is_nothing():
    one = detect(mk("tests/test_a.py", removed=["def test_one():"]))
    two = detect(mk("tests/test_a.py", removed=["def test_one():"], added=["@pytest.mark.skip"]))
    assert floor_for(one) is RiskLevel.MEDIUM and floor_for(two) is RiskLevel.HIGH and floor_for([]) is None


# ---- the gates (through the service) -----------------------------------------------------------------------------------------------
PYTEST_CMD = f"{sys.executable} -m pytest -q"
TASK = "설명 문구 정리"  # nothing in the text suggests risk


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "tests").mkdir(parents=True)
    for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        run_git(r, *a)
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")  # buggy
    (r / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n", encoding="utf-8")
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "init")
    return r


def plane_for(repo, **policy):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 8))
    pol = Policy(allowed_commands=[PYTEST_CMD], **policy)
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command=PYTEST_CMD))


def events(repo):
    return [json.loads(l) for l in (repo / ".devorch" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


async def review(plane, repo, mutate):
    tid = plane.analyze_task(TASK)["task_id"]
    mutate(repo)
    await plane.request_review(tid)
    return plane.store.load(tid)


def delete_the_failing_test(repo):  # the cheap way out: the bug stays, the failing test goes
    (repo / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n", encoding="utf-8")


def delete_and_skip(repo):
    (repo / "tests" / "test_calc.py").write_text("import pytest\nfrom calc import add\n\n\n@pytest.mark.skip\ndef test_zero():\n    assert add(0, 0) == 0\n", encoding="utf-8")


def fix_the_bug(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


def add_a_test(repo):
    p = repo / "tests" / "test_calc.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n\ndef test_neg():\n    assert add(-1, 1) == 0\n", encoding="utf-8")


async def test_deleting_the_failing_test_is_raised_to_medium_with_a_reviewer_and_says_why(repo):
    rec = await review(plane_for(repo), repo, delete_the_failing_test)
    assert rec.run.risk.level is RiskLevel.MEDIUM and rec.run.plan.review_steps and not rec.run.plan.requires_human_approval
    assert "테스트를 약하게" in (rec.run.risk.floor_reason or "") and "순수하게 1개" in rec.run.risk.floor_reason
    assert any("테스트를 약하게" in r for e in events(repo) if e["event"] == "mcp:diff_reassessed" for r in e["data"]["reasons"])


async def test_two_kinds_of_weakening_need_a_human_approval(repo):
    rec = await review(plane_for(repo), repo, delete_and_skip)
    assert rec.run.risk.level is RiskLevel.HIGH and rec.run.plan.requires_human_approval


async def test_a_real_fix_that_adds_a_test_is_not_touched(repo):
    def fix_and_test(r):
        fix_the_bug(r)
        add_a_test(r)

    rec = await review(plane_for(repo), repo, fix_and_test)
    assert rec.run.risk.level is RiskLevel.LOW and not rec.run.plan.review_steps
    assert not any(e["event"] == "mcp:diff_reassessed" for e in events(repo))


async def test_the_check_can_be_switched_off_in_the_policy(repo):
    rec = await review(plane_for(repo, detect_weakened_tests=False), repo, delete_the_failing_test)
    assert rec.run.risk.level is RiskLevel.LOW


async def test_it_never_lowers_a_level_that_is_already_higher(repo):
    plane = plane_for(repo)
    tid = plane.analyze_task("결제 로직 변경 calc.py")["task_id"]
    before = plane.store.load(tid).run.risk.level
    delete_the_failing_test(repo)
    await plane.request_review(tid)
    after = plane.store.load(tid).run.risk.level
    assert after.rank >= before.rank
