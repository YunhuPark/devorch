"""`devorch review`: run the policy's reviewers on a working tree, a git range or a diff file — with the cost shown before
and compared after, and every refusal happening before any LLM call."""

import json
import sys

import pytest
import yaml
from gitutil import run_git
from typer.testing import CliRunner

from devorch.cli.main import app
from devorch.config import Policy
from devorch.core.execution.executor import Executor
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane, ControlPlaneError

PYTEST_CMD = f"{sys.executable} -m pytest -q"
OA = {"input_per_mtok": 2.0, "output_per_mtok": 10.0}
GE = {"input_per_mtok": 0.75, "output_per_mtok": 3.75}
HIGH_TASK = "로그인 JWT 인증 추가"
BLOCKING = json.dumps([{"severity": "high", "category": "security", "title": "권한 검증 누락", "detail": "x", "file": "calc.py"}])
SECRET_DIFF = (
    "diff --git a/x.py b/x.py\nnew file mode 100644\n--- /dev/null\n+++ b/x.py\n@@ -0,0 +1 @@\n+KEY = 'sk-abcdefghijklmnopqrstuvwx'\n"
)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    run_git(r, "config", "user.email", "t@example.com")
    run_git(r, "config", "user.name", "T")
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "init")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    run_git(r, "commit", "-q", "-am", "fix add")
    return r


class Plane:
    """A ControlPlane with scripted reviewers, patched in where the CLI would build the real one."""

    def __init__(self, root, openai=("[]",), gemini=("[]",), config=None, policy=None):
        self.openai, self.gemini = MockProvider("openai", list(openai)), MockProvider("gemini", list(gemini))
        reg = ProviderRegistry()
        for p in (MockProvider("claude", ["x"]), self.openai, self.gemini):
            reg.register(p)
        pol = policy or Policy(allowed_commands=[PYTEST_CMD])
        self.plane = ControlPlane(root, pol, config if config is not None else {}, reg, Executor(root, pol, test_command=PYTEST_CMD))

    def install(self, monkeypatch):
        monkeypatch.setattr(ControlPlane, "from_project", classmethod(lambda cls, path, mock=False, skip_tests=False: self.plane))
        return self

    @property
    def calls(self):
        return len(self.openai.calls) + len(self.gemini.calls)


def review(*args):
    return CliRunner().invoke(app, ["review", *args])


# ---- what gets reviewed -----------------------------------------------------------------------------
def test_git_range_runs_the_reviewers_the_level_requires_and_prints_status_and_cost(repo, monkeypatch):
    p = Plane(repo).install(monkeypatch)
    r = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--yes")
    assert r.exit_code == 0, r.output
    assert "READY" in r.output and "openai" in r.output and "gemini" in r.output
    assert len(p.openai.calls) == 1 and len(p.gemini.calls) == 1  # HIGH: security (openai) + architecture (gemini)
    assert "return a + b" in p.openai.calls[0].prompt  # the range's diff, not the working tree


def test_default_target_is_the_working_tree_against_head(repo, monkeypatch):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # uncommitted\n", encoding="utf-8")
    p = Plane(repo).install(monkeypatch)
    r = review("-p", str(repo), "--level", "medium", "--yes")
    assert r.exit_code == 0, r.output
    assert "uncommitted" in p.openai.calls[0].prompt and not p.gemini.calls  # MEDIUM: one general reviewer


def test_a_diff_file_works_without_a_git_repository(tmp_path, monkeypatch):
    p = Plane(tmp_path).install(monkeypatch)
    f = tmp_path / "change.diff"
    f.write_text("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n", encoding="utf-8")
    r = review("-p", str(tmp_path), "--diff-file", str(f), "--level", "medium", "--yes")
    assert r.exit_code == 0, r.output
    assert "x = 2" in p.openai.calls[0].prompt


def test_a_low_risk_change_needs_no_reviewer_and_says_how_to_ask_for_one(repo, monkeypatch):
    p = Plane(repo).install(monkeypatch)
    r = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", "버튼 텍스트 변경", "--yes")
    assert r.exit_code == 0 and p.calls == 0
    assert "--level" in r.output


# ---- the result -----------------------------------------------------------------------------------------
def test_a_blocking_finding_exits_1_so_ci_can_gate_on_it(repo, monkeypatch):
    Plane(repo, openai=(BLOCKING,)).install(monkeypatch)
    r = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--yes")
    assert r.exit_code == 1 and "NOT_READY" in r.output and "권한 검증 누락" in r.output


def test_json_output_has_status_cost_and_estimate(repo, monkeypatch):
    Plane(repo, config={"providers": {"openai": OA, "gemini": GE}}).install(monkeypatch)
    r = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--yes", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.output[r.output.index("{"):])
    assert out["status"] == "READY" and out["estimated_review_cost_usd"] > 0 and "usage" in out and out["reviewers"] == ["security:openai", "architecture:gemini"]


def test_the_estimate_is_shown_before_the_call_and_compared_with_the_actual_cost_after(repo, monkeypatch):
    Plane(repo, config={"providers": {"openai": OA, "gemini": GE}}).install(monkeypatch)
    r = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--yes")
    assert r.exit_code == 0
    assert "예상 비용" in r.output and "실제 비용" in r.output  # before and after


# ---- every refusal happens before any LLM call -------------------------------------------------------------
def test_a_diff_with_credentials_is_refused_before_any_call(tmp_path, monkeypatch):
    p = Plane(tmp_path).install(monkeypatch)
    f = tmp_path / "s.diff"
    f.write_text(SECRET_DIFF, encoding="utf-8")
    r = review("-p", str(tmp_path), "--diff-file", str(f), "--level", "medium", "--yes")
    assert r.exit_code == 2 and "credential" in r.output and p.calls == 0


def test_an_oversized_diff_is_refused_before_any_call(tmp_path, monkeypatch):
    p = Plane(tmp_path).install(monkeypatch)
    f = tmp_path / "big.diff"
    f.write_text("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,30000 @@\n" + "+xxxxxxxxxx\n" * 30000, encoding="utf-8")
    r = review("-p", str(tmp_path), "--diff-file", str(f), "--level", "medium", "--yes")
    assert r.exit_code == 2 and "너무 큽니다" in r.output and p.calls == 0


def test_a_git_range_cannot_smuggle_an_option(repo, monkeypatch):
    p = Plane(repo).install(monkeypatch)
    r = review("-p", str(repo), f"--git-range=--output={repo / 'evil.txt'}", "--level", "medium", "--yes")
    assert r.exit_code == 2 and not (repo / "evil.txt").exists() and p.calls == 0


def test_a_bad_range_is_a_clear_error_not_a_traceback(repo, monkeypatch):
    p = Plane(repo).install(monkeypatch)
    r = review("-p", str(repo), "--git-range", "nope123..alsonope", "--level", "medium", "--yes")
    assert r.exit_code == 2 and "Traceback" not in r.output and p.calls == 0


def test_only_one_source_and_a_valid_level_that_can_only_be_raised(repo, tmp_path, monkeypatch):
    p = Plane(repo).install(monkeypatch)
    f = tmp_path / "a.diff"
    f.write_text("x", encoding="utf-8")
    assert review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--diff-file", str(f), "--yes").exit_code == 2
    assert review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--level", "extreme", "--yes").exit_code == 2
    lower = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--level", "low", "--yes")
    assert lower.exit_code == 2 and "낮출 수 없" in lower.output and p.calls == 0


def test_declining_the_confirmation_makes_no_call(repo, monkeypatch):
    p = Plane(repo).install(monkeypatch)
    r = CliRunner().invoke(app, ["review", "-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK], input="n\n")
    assert r.exit_code == 1 and p.calls == 0 and "예상 비용" in r.output


def test_a_review_expected_to_exceed_the_cost_cap_is_refused_before_any_call(repo, monkeypatch):
    pol = Policy.model_validate({"allowed_commands": [PYTEST_CMD], "limits": {"max_cost_usd": 0.0001}})
    p = Plane(repo, config={"providers": {"openai": OA, "gemini": GE}}, policy=pol).install(monkeypatch)
    r = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--yes")
    assert r.exit_code == 2 and "상한" in r.output and p.calls == 0


# ---- the service underneath ----------------------------------------------------------------------------------
async def test_service_reviews_an_injected_diff_without_git_and_can_only_raise_the_level(tmp_path):
    pl = Plane(tmp_path).plane
    with pytest.raises(ControlPlaneError, match="낮출 수 없"):
        pl.analyze_task(HIGH_TASK, level="low")
    a = pl.analyze_task("버튼 텍스트 변경", level="high")
    assert a["risk"]["level"] == "high" and "사용자 지정" in a["risk"]["floor_reason"]
    res = await pl.request_review(a["task_id"], diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n")
    assert res["status"] == "READY"


def test_config_file_prices_flow_into_the_review_estimate(repo):
    cfg = {"providers": {"openai": OA, "gemini": GE}}
    assert yaml.safe_load(yaml.safe_dump(cfg)) == cfg  # plain data: nothing exotic needed in config.yaml


def test_a_ready_result_says_it_is_not_a_safety_guarantee_and_a_blocked_one_does_not(repo, monkeypatch):
    """READY only means the reviewers found nothing that blocks; how well AI reviewers find real bugs has not been validated."""
    Plane(repo).install(monkeypatch)
    ok = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--yes")
    assert ok.exit_code == 0 and "안전 보증이 아닙니다" in ok.output and "검증되지 않았" in ok.output
    Plane(repo).install(monkeypatch)  # fresh scripted reviewers for the second call
    as_json = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--yes", "--json")
    out = json.loads(as_json.output[as_json.output.index("{"):])
    assert out["status"] == "READY" and "안전 보증이 아닙니다" in out["caveat"]

    Plane(repo, openai=(BLOCKING,)).install(monkeypatch)
    blocked = review("-p", str(repo), "--git-range", "HEAD~1..HEAD", "--task", HIGH_TASK, "--yes")
    assert blocked.exit_code == 1 and "안전 보증이 아닙니다" not in blocked.output


# ---- the refusal says WHERE the credential-shaped string is (never the string itself) -------------------------------------------
def _diff_with_key(key):
    return (
        "diff --git a/app/conf.py b/app/conf.py\n--- a/app/conf.py\n+++ b/app/conf.py\n@@ -1,3 +1,5 @@\n"
        " import os\n \n+TOKEN = os.environ.get('X')\n+API = '" + key + "'\n DEBUG = False\n"
    )


def test_credential_refusal_names_file_and_line_but_not_the_secret(tmp_path, monkeypatch):
    from devorch.service import secret_locations

    key = "sk-" + "a1B2c3D4e5" * 3  # assembled at run time: the literal would make THIS commit unreviewable
    diff = _diff_with_key(key)
    assert secret_locations(diff) == ["app/conf.py:4"]  # new-file line number: 1 import, 2 blank, 3 TOKEN, 4 API
    assert secret_locations(_diff_with_key("not-a-key")) == []

    f = tmp_path / "c.diff"
    f.write_text(diff, encoding="utf-8")
    Plane(tmp_path).install(monkeypatch)
    r = review("-p", str(tmp_path), "--diff-file", str(f), "--level", "medium", "--yes")
    assert r.exit_code != 0 and "app/conf.py:4" in r.output and "가짜 키" in r.output
    assert key not in r.output and key[:12] not in r.output  # location only
