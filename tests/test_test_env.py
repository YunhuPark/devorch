"""`project.test_env`: found by trying DevOrch on a real repo (algo-pipeline, 2026-10-17) whose tests only START when TAVILY_API_KEY is set to
*any* value. DevOrch strips key/token/secret-looking variables from test runs, so those tests could not even be collected.

The stripping stays; `project.test_env` is the explicit way to give a test run DUMMY values (never real keys).
"""

import sys

import pytest
import yaml
from gitutil import run_git
from test_doctor import by, project

from devorch.config import ConfigError, Policy, load_test_env
from devorch.core.execution.executor import Executor
from devorch.core.execution.sandbox import SandboxConfig, build_docker_args
from devorch.doctor import run_checks
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane

PYTEST_CMD = f"{sys.executable} -m pytest -q"
LOW_TASK = "calc.py add 버그 수정"
NEEDS_ENV = (
    "import os\n\n\ndef test_the_dummy_key_is_there_and_the_host_key_is_not():\n"
    "    assert os.environ.get('DUMMY_API_KEY') == 'test-key'\n"
    "    assert 'HOST_ONLY_TOKEN' not in os.environ\n"
)


# ---- validation ------------------------------------------------------------------------------------------------------------------
def test_plain_dummy_values_are_accepted_and_numbers_become_strings():
    cfg = {"project": {"test_env": {"TAVILY_API_KEY": "test-key", "ALGO_ENV": "test", "RETRIES": 3}}}
    assert load_test_env(cfg, host_env={}) == {"TAVILY_API_KEY": "test-key", "ALGO_ENV": "test", "RETRIES": "3"}


def test_no_test_env_is_empty():
    assert load_test_env({}, host_env={}) == {} and load_test_env({"project": {"test_env": {}}}, host_env={}) == {}


@pytest.mark.parametrize("bad", [
    {"1BAD": "x"}, {"has space": "x"}, {"OK_NAME": ["a"]}, {"OK_NAME": {"a": 1}}, {"OK_NAME": True}, ["A=b"], "A=b",
])
def test_malformed_entries_are_rejected_with_a_config_error(bad):
    with pytest.raises(ConfigError):
        load_test_env({"project": {"test_env": bad}}, host_env={})


@pytest.mark.parametrize("real_looking", ["sk-" + "a" * 30, "AIza" + "b" * 30, "ghp_" + "c" * 30, "AKIA" + "D" * 16])
def test_a_value_in_a_known_credential_format_is_rejected(real_looking):
    with pytest.raises(ConfigError) as e:
        load_test_env({"project": {"test_env": {"SOME_API_KEY": real_looking}}}, host_env={})
    assert real_looking not in str(e.value)  # the message never repeats the value


def test_a_value_equal_to_the_real_variable_of_the_same_name_is_rejected():
    """A copied real key whose format DevOrch does not know (e.g. a Tavily key) is still caught when it is the one in the environment."""
    with pytest.raises(ConfigError):
        load_test_env({"project": {"test_env": {"TAVILY_API_KEY": "tvly-realvalue123"}}}, host_env={"TAVILY_API_KEY": "tvly-realvalue123"})
    assert load_test_env({"project": {"test_env": {"TAVILY_API_KEY": "test-key"}}}, host_env={"TAVILY_API_KEY": "tvly-realvalue123"})


# ---- docker ----------------------------------------------------------------------------------------------------------------------
def test_docker_gets_only_the_test_env_never_the_host_variables(monkeypatch):
    monkeypatch.setenv("HOST_ONLY_TOKEN", "do-not-pass")
    args = build_docker_args(SandboxConfig(mode="docker", image="img"), "n", __import__("pathlib").Path("."), "pytest", 1000, 1000, {"DUMMY_API_KEY": "test-key"})
    assert "DUMMY_API_KEY=test-key" in args and not any("HOST_ONLY_TOKEN" in a or "do-not-pass" in a for a in args)
    assert not any("DUMMY" in a for a in build_docker_args(SandboxConfig(mode="docker", image="img"), "n", __import__("pathlib").Path("."), "pytest", 1000, 1000))


# ---- the real test run -----------------------------------------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        run_git(r, *a)
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (r / "test_envvars.py").write_text(NEEDS_ENV, encoding="utf-8")
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "init")
    return r


def plane_for(repo, test_env):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 4))
    pol = Policy(allowed_commands=[PYTEST_CMD])
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command=PYTEST_CMD, test_env=test_env))


async def go(plane, repo):
    tid = plane.analyze_task(LOW_TASK)["task_id"]
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    await plane.request_review(tid)
    return await plane.run_tests(tid), tid


async def test_without_test_env_the_tests_cannot_start_the_way_the_real_repo_could_not(repo, monkeypatch):
    monkeypatch.setenv("HOST_ONLY_TOKEN", "do-not-pass")
    result, _ = await go(plane_for(repo, {}), repo)
    assert result["status"] == "failed"


async def test_with_test_env_the_run_passes_and_the_host_key_looking_variable_is_still_stripped(repo, monkeypatch):
    monkeypatch.setenv("HOST_ONLY_TOKEN", "do-not-pass")
    plane = plane_for(repo, {"DUMMY_API_KEY": "test-key"})
    result, tid = await go(plane, repo)
    assert result["status"] == "passed", result
    assert (await plane.apply_changes(tid))["status"] == "committed"


# ---- wiring and doctor -----------------------------------------------------------------------------------------------------------
def test_from_project_reads_test_env_and_refuses_a_real_looking_value(tmp_path):
    r = project(tmp_path)
    cfg_path = r / ".devorch" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["project"]["test_env"] = {"DUMMY_API_KEY": "test-key"}
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert ControlPlane.from_project(r, mock=True).executor.test_env == {"DUMMY_API_KEY": "test-key"}
    cfg["project"]["test_env"] = {"DUMMY_API_KEY": "sk-" + "z" * 30}
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ConfigError):
        ControlPlane.from_project(r, mock=True)


def test_doctor_lists_the_names_but_never_the_values_and_fails_on_a_real_looking_one(tmp_path):
    r = project(tmp_path)
    cfg_path = r / ".devorch" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["project"]["test_env"] = {"DUMMY_API_KEY": "test-key-value-xyz"}
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    ok = by(run_checks(r, env={}), "테스트 환경변수")
    assert ok.status == "ok" and "DUMMY_API_KEY" in ok.detail and "test-key-value-xyz" not in ok.detail
    cfg["project"]["test_env"] = {"DUMMY_API_KEY": "sk-" + "z" * 30}
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    bad = by(run_checks(r, env={}), "테스트 환경변수")
    assert bad.status == "fail" and "z" * 30 not in bad.detail
