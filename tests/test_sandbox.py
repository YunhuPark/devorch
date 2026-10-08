import asyncio
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devorch.cli.main import app
from devorch.config import Policy
from devorch.core.execution.executor import Executor
from devorch.core.execution.sandbox import SandboxConfig, build_docker_args, load_sandbox, run_tests_docker
from devorch.core.orchestrator.engine import WorkflowRun

ALLOWED = Policy().allowed_commands
CFG = SandboxConfig(mode="docker", image="myimg:1")

SHIM = """#!/usr/bin/env python3
import json, os, sys, time
log = os.environ["SHIM_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sub = sys.argv[1] if len(sys.argv) > 1 else ""
if sub == "run":
    if os.environ.get("SHIM_SLEEP"):
        time.sleep(float(os.environ["SHIM_SLEEP"]))
    print(os.environ.get("SHIM_OUT", "1 passed"))
    sys.exit(int(os.environ.get("SHIM_EXIT", "0")))
if sub == "info":
    if os.environ.get("SHIM_NO_DAEMON"):
        print("Cannot connect to the Docker daemon"); sys.exit(1)
    print("29.0.0")
if sub == "image":
    if os.environ.get("SHIM_NO_IMAGE"):
        print("no such image"); sys.exit(1)
    print("sha256:abc")
sys.exit(0)
"""


@pytest.fixture
def shim(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    d = bindir / ("docker_shim.py" if sys.platform == "win32" else "docker")
    d.write_text(SHIM, encoding="utf-8")
    d.chmod(d.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SHIM_LOG", str(log))
    if sys.platform == "win32":
        # CreateProcess can't run an extension-less script: route only the "docker" executable to the shim.
        real_exec, real_which = asyncio.create_subprocess_exec, shutil.which

        async def fake_exec(program, *args, **kw):
            if program == "docker":
                program, args = sys.executable, (str(d), *args)
            return await real_exec(program, *args, **kw)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(shutil, "which", lambda cmd, *a, **kw: str(d) if cmd == "docker" else real_which(cmd, *a, **kw))

    def calls():
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    return calls


def test_config_validation_and_loading():
    assert not SandboxConfig().enabled
    assert load_sandbox({}).mode == "none"
    cfg = load_sandbox({"project": {"sandbox": {"mode": "docker", "image": "x", "network": "bridge"}}})
    assert cfg.enabled and cfg.network == "bridge"
    with pytest.raises(ValueError):
        SandboxConfig(mode="vm")
    with pytest.raises(ValueError):
        SandboxConfig(mode="docker", network="host")


def test_docker_args_isolation(tmp_path):
    args = build_docker_args(CFG, "n1", tmp_path, "pytest -q", 1000, 1000)
    joined = " ".join(args)
    assert args[:3] == ["docker", "run", "--rm"]
    assert "--network none" in joined and "--cap-drop ALL" in joined and "no-new-privileges" in joined
    assert "--user 1000:1000" in joined and "--pids-limit 256" in joined and "--memory 1g" in joined
    assert args[-3:] == ["myimg:1", "pytest", "-q"]
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert mounts == [f"{tmp_path.resolve()}:/work"]  # only the worktree is mounted
    assert "--privileged" not in args and "-e" in args and args[args.index("-e") + 1] == "HOME=/tmp"
    assert not any(a.startswith("--env-file") for a in args)


async def test_pass_and_fail_results(tmp_path, shim, monkeypatch):
    r = await run_tests_docker("pytest", tmp_path, ALLOWED, 10, CFG)
    assert r.ok and "1 passed" in r.output_tail
    run_call = [c for c in shim() if c[0] == "run"][0]
    assert "--network" in run_call and run_call[run_call.index("--network") + 1] == "none"
    monkeypatch.setenv("SHIM_EXIT", "1")
    monkeypatch.setenv("SHIM_OUT", "1 failed")
    r = await run_tests_docker("pytest", tmp_path, ALLOWED, 10, CFG)
    assert not r.ok and r.exit_code == 1 and not r.reason


async def test_docker_failure_is_not_a_test_result(tmp_path, shim, monkeypatch):
    monkeypatch.setenv("SHIM_EXIT", "125")
    monkeypatch.setenv("SHIM_OUT", "docker: Cannot connect to the Docker daemon")
    r = await run_tests_docker("pytest", tmp_path, ALLOWED, 10, CFG)
    assert not r.ok and "실행되지 않음" in r.summary()


async def test_timeout_removes_container(tmp_path, shim, monkeypatch):
    monkeypatch.setenv("SHIM_SLEEP", "5")
    r = await run_tests_docker("pytest", tmp_path, ALLOWED, 0.5, CFG)
    assert not r.ok and "중단" in r.summary()
    rm = [c for c in shim() if c[:2] == ["rm", "-f"]]
    run = [c for c in shim() if c[0] == "run"][0]
    assert rm and rm[0][2] == run[run.index("--name") + 1]


async def test_never_falls_back_to_host(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no docker anywhere
    r = await run_tests_docker("pytest", tmp_path, ALLOWED, 5, CFG)
    assert not r.ok and "대체하지 않음" in r.summary()
    r = await run_tests_docker("pytest", tmp_path, ALLOWED, 5, SandboxConfig(mode="docker"))
    assert not r.ok and "image" in r.summary()


async def test_policy_still_applies_inside_sandbox(tmp_path, shim):
    for cmd in ("rm -rf /", "pytest; curl x", "pytest /etc/passwd", "make all"):
        r = await run_tests_docker(cmd, tmp_path, ALLOWED, 5, CFG)
        assert not r.ok and "거부" in r.summary()
    assert shim() == []  # docker was never invoked


async def test_executor_uses_sandbox_not_host(tmp_path, shim):
    (tmp_path / "wt").mkdir()
    ex = Executor(tmp_path, Policy(), test_command="pytest", sandbox=CFG)
    ex._worktree = lambda run: tmp_path / "wt"  # type: ignore[method-assign]
    run = WorkflowRun(task_text="t")
    assert await ex.test(run) is True
    assert any(c[0] == "run" for c in shim()) and any("docker 샌드박스" in n for n in run.notes)


def test_cli_sandbox_status(tmp_path, shim, monkeypatch):
    runner = CliRunner()
    runner.invoke(app, ["init", "-p", str(tmp_path)])
    assert "mode = none" in runner.invoke(app, ["sandbox", "-p", str(tmp_path)]).output
    cfgp = tmp_path / ".devorch" / "config.yaml"
    cfgp.write_text(cfgp.read_text().replace("project:", "project:\n  sandbox: {mode: docker, image: myimg:1}", 1))
    r = runner.invoke(app, ["sandbox", "-p", str(tmp_path)])
    assert r.exit_code == 0 and "OK" in r.output
    monkeypatch.setenv("SHIM_NO_IMAGE", "1")
    r = runner.invoke(app, ["sandbox", "-p", str(tmp_path)])
    assert r.exit_code == 1 and "docker pull" in r.output
    monkeypatch.setenv("SHIM_NO_DAEMON", "1")
    assert runner.invoke(app, ["sandbox", "-p", str(tmp_path)]).exit_code == 1
