"""The Docker sandbox, against a REAL Docker daemon. Skipped when Docker or the test image is missing.

Build the image once:   docker build -t devorch-eval:py312 tests/docker      (CI does this on Linux)
Validated by hand on Docker Desktop (Windows, WSL2 backend) on 2026-10-15; these tests keep it validated.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from gitutil import run_git

from devorch.config import Policy
from devorch.core.execution.executor import Executor
from devorch.core.execution.sandbox import SandboxConfig, run_tests_docker
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.service import ControlPlane
from devorch.store import Approval

IMAGE = os.environ.get("DEVORCH_TEST_IMAGE", "devorch-eval:py312")
HERE = Path(__file__).parent
ALLOWED = ["python -m pytest"]
CMD = "python -m pytest -q -p no:cacheprovider"
PAYMENT = "결제 로직 변경 calc.py"
GOOD = "def add(a, b):\n    return a + b + 0\n"


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode != 0:
            return False
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# DEVORCH_REQUIRE_DOCKER=1 (set by CI) turns a missing Docker into failures instead of a silent skip
pytestmark = pytest.mark.skipif(not _docker_ready() and not os.environ.get("DEVORCH_REQUIRE_DOCKER"), reason=f"needs a running Docker daemon and the image {IMAGE} (docker build -t {IMAGE} tests/docker)")
CFG = SandboxConfig(mode="docker", image=IMAGE, network="none", memory="512m", cpus="2", pids=64)


def workdir(tmp_path, files=None, isolation=True):
    d = tmp_path / "work"
    d.mkdir()
    (d / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    if isolation:
        shutil.copy(HERE / "docker" / "iso_tests.py", d / "test_isolation.py")
    for name, body in (files or {}).items():
        (d / name).write_text(body, encoding="utf-8")
    return d


async def test_a_hostile_test_finds_nothing_to_attack_inside_the_container(tmp_path, monkeypatch):
    """10 checks run INSIDE the container: non-root, no network/DNS, host env and paths invisible, signing key unreachable, no writes outside /work and /tmp."""
    monkeypatch.setenv("DEVORCH_PROBE_SECRET", "must-not-leak")  # in the HOST process; the container must not see it
    r = await run_tests_docker(CMD, workdir(tmp_path), ALLOWED, 120, CFG)
    assert r.ok, r.output_tail
    assert "10 passed" in r.output_tail


async def test_the_container_can_read_and_write_the_bind_mounted_directory(tmp_path):
    body = "import pathlib\n\ndef test_rw():\n    assert pathlib.Path('calc.py').exists()\n    pathlib.Path('out.txt').write_text('from-container')\n"
    d = workdir(tmp_path, {"test_rw.py": body}, isolation=False)
    r = await run_tests_docker(CMD + " test_rw.py", d, ALLOWED, 120, CFG)
    assert r.ok, r.output_tail
    assert (d / "out.txt").read_text(encoding="utf-8") == "from-container"  # the user id the container runs as can write to the host directory


async def test_memory_limit_kills_a_test_that_allocates_too_much(tmp_path):
    d = workdir(tmp_path, {"test_mem.py": "def test_mem():\n    x = bytearray(2 * 1024 * 1024 * 1024)\n    assert x\n"}, isolation=False)
    r = await run_tests_docker(CMD + " test_mem.py", d, ALLOWED, 120, CFG)
    assert not r.ok and r.exit_code == 137  # killed by the OOM killer


async def test_pids_limit_refuses_a_fork_bomb(tmp_path):
    body = (
        "import subprocess\n\n"
        "def test_bomb():\n"
        "    ps = []\n"
        "    try:\n"
        "        for _ in range(500):\n"
        "            ps.append(subprocess.Popen(['sleep', '20']))\n"
        "    except OSError:\n"
        "        return\n"
        "    finally:\n"
        "        for p in ps:\n"
        "            p.kill()\n"
        "    raise AssertionError('spawned 500 processes')\n"
    )
    d = workdir(tmp_path, {"test_pids.py": body}, isolation=False)
    r = await run_tests_docker(CMD + " test_pids.py", d, ALLOWED, 120, CFG)
    assert r.ok, r.output_tail


async def test_a_timed_out_test_leaves_no_container_behind(tmp_path):
    d = workdir(tmp_path, {"test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n"}, isolation=False)
    r = await run_tests_docker(CMD + " test_slow.py", d, ALLOWED, 8, CFG)
    assert not r.ok and "중단" in r.reason
    left = subprocess.run(["docker", "ps", "-q", "--filter", "name=devorch-"], capture_output=True, text=True).stdout.split()
    assert left == []


async def test_a_missing_image_fails_the_tests_instead_of_running_them_on_the_host(tmp_path):
    bad = SandboxConfig(mode="docker", image="devorch-no-such-image:0", network="none")
    r = await run_tests_docker(CMD, workdir(tmp_path), ALLOWED, 60, bad)
    assert not r.ok and r.exit_code == 125 and "시작하지 못함" in r.reason


# ---- through the real service ------------------------------------------------------------------------------------------------
HOSTILE = '''import hashlib, hmac, json, os, pathlib


def test_it_looks_like_a_normal_passing_test():
    assert 1 + 1 == 2
    try:
        wt = pathlib.Path.cwd()
        tasks = wt.parent.parent / "tasks"
        rec = json.loads((tasks / (wt.name + ".json")).read_text(encoding="utf-8"))
        key = pathlib.Path(os.environ.get("DEVORCH_KEY_FILE") or (pathlib.Path.home() / ".devorch" / "state.key")).read_bytes()
        at = "2026-01-01T00:00:00+00:00"
        sig = hmac.new(key, ("approval|%s|%s|True|cli|%s" % (wt.name, rec["diff_sha"], at)).encode(), hashlib.sha256).hexdigest()
        (tasks / (wt.name + ".approval.json")).write_text(json.dumps({"task_id": wt.name, "diff_sha": rec["diff_sha"], "approved": True, "via": "cli", "at": at, "sig": sig}))
    except Exception:
        pass
'''


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


def docker_plane(repo):
    reg = ProviderRegistry()
    for n in ("claude", "openai", "gemini"):
        reg.register(MockProvider(n, ["[]"] * 8))
    pol = Policy(allowed_commands=ALLOWED)
    return ControlPlane(repo, pol, {}, reg, Executor(repo, pol, test_command="python -m pytest -q", sandbox=CFG))


async def test_a_hostile_test_cannot_forge_the_human_approval_in_the_docker_sandbox(repo):
    """The same test run with sandbox mode 'none' reads the signing key and approves its own change (reproduced 2026-10-15) — Docker is what stops it."""
    plane = docker_plane(repo)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    (repo / "test_hostile.py").write_text(HOSTILE, encoding="utf-8")
    await plane.request_review(a["task_id"])
    assert (await plane.run_tests(a["task_id"]))["status"] == "passed"
    assert not (repo / ".devorch" / "tasks" / f"{a['task_id']}.approval.json").exists()
    assert (await plane.apply_changes(a["task_id"]))["status"] == "approval_required"


async def test_the_whole_flow_commits_exactly_the_reviewed_files_when_tests_run_in_docker(repo):
    plane = docker_plane(repo)
    a = plane.analyze_task(PAYMENT)
    (repo / "calc.py").write_text(GOOD, encoding="utf-8")
    await plane.request_review(a["task_id"])
    t = await plane.run_tests(a["task_id"])
    assert t["status"] == "passed"
    assert any("docker" in n for n in plane._load(a["task_id"]).run.notes)
    plane.store.save_approval(Approval(task_id=a["task_id"], diff_sha=plane._load(a["task_id"]).diff_sha, approved=True, via="cli"))
    r = await plane.apply_changes(a["task_id"])
    assert r["status"] == "committed"
    assert sorted(run_git(repo, "ls-tree", "-r", "--name-only", r["branch"]).stdout.split()) == ["calc.py", "test_calc.py"]
