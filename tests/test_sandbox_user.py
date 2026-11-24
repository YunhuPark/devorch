"""The Docker sandbox never runs the agent's tests as root.

The container runs as the host user's uid. When DevOrch itself runs as root (common on Linux servers and in containers) that made the container
root too — reproduced 2026-11-24 on Linux: 2 of the 10 isolation checks failed (not an unprivileged user; could write outside /work and /tmp).
Now: host root + no ``project.sandbox.user`` → the tests are refused before Docker starts; ``user: "uid:gid"`` (never 0) is the way out.
No Docker needed here — the real container run with a configured user is in tests/test_docker_sandbox.py.
"""

import asyncio
import sys

import pytest
import yaml
from pydantic import ValidationError

from devorch.core.execution import sandbox as sb
from devorch.core.execution.sandbox import FALLBACK_UID_GID, ROOT_REFUSED, SandboxConfig, build_docker_args, container_user, run_tests_docker
from devorch.doctor import run_checks

CMD = "python -m pytest -q"
ALLOWED = ["python -m pytest"]
DOCKER = SandboxConfig(mode="docker", image="img:x")


@pytest.fixture
def host(monkeypatch):
    """Pretend the host user is ``uid:gid`` (None = an OS without uids, i.e. Windows)."""

    def set_host(ids):
        monkeypatch.setattr(sb, "host_uid_gid", lambda: ids)

    return set_host


@pytest.fixture
def launches(monkeypatch):
    """Record every process launch instead of starting docker; the launch then 'fails' so nothing runs."""
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        raise OSError("not starting docker in a unit test")

    monkeypatch.setattr(sb.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sb.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def run(cfg, tmp_path):
    return asyncio.run(run_tests_docker(CMD, tmp_path, ALLOWED, 30, cfg))


def test_a_root_host_without_a_configured_user_is_refused_before_docker_starts(host, launches, tmp_path):
    host((0, 0))
    r = run(DOCKER, tmp_path)
    assert not r.ok and launches == []  # nothing launched — not a container run as root, not a host run instead
    assert "root" in r.reason and "project.sandbox.user" in r.reason and "호스트 실행으로 대체하지 않음" in r.reason


def test_a_root_host_with_a_configured_user_runs_the_container_as_that_user(host, launches, tmp_path):
    host((0, 0))
    run(SandboxConfig(mode="docker", image="img:x", user="1000:1000"), tmp_path)
    assert len(launches) == 1
    args = launches[0]
    assert args[args.index("--user") + 1] == "1000:1000"


def test_a_normal_host_user_is_used_as_is(host, launches, tmp_path):
    host((1234, 5678))
    run(DOCKER, tmp_path)
    args = launches[0]
    assert args[args.index("--user") + 1] == "1234:5678"


def test_a_configured_user_wins_over_a_normal_host_user(host):
    host((1234, 5678))
    assert container_user(SandboxConfig(mode="docker", image="i", user="2000:2001")) == (2000, 2001)


def test_windows_keeps_the_fixed_unprivileged_id(host):
    host(None)
    assert container_user(DOCKER) == FALLBACK_UID_GID and 0 not in FALLBACK_UID_GID


def test_a_root_host_gives_the_refusal_not_an_id(host):
    host((0, 1000))
    assert container_user(DOCKER) == ROOT_REFUSED
    assert container_user(None) == ROOT_REFUSED  # callers without a config get the same answer


@pytest.mark.parametrize("bad", ["0:0", "0:1000", "1000:0", "root", "1000", "1000:1000:1", "-1:1000", "a:b", ""])
def test_the_configured_user_must_be_a_non_root_uid_gid(bad):
    with pytest.raises(ValidationError):
        SandboxConfig(mode="docker", image="i", user=bad)


def test_the_configured_user_is_normalised():
    assert SandboxConfig(user=" 01000:1000 ").user == "1000:1000"
    assert SandboxConfig().user is None


def test_the_docker_command_line_never_contains_user_zero(host):
    for ids in [(1, 1), (1000, 1000)]:
        host(ids)
        uid, gid = container_user(DOCKER)
        args = build_docker_args(DOCKER, "n", ".", CMD, uid, gid)
        assert not args[args.index("--user") + 1].startswith("0:")


def _docker_project(tmp_path, sandbox):
    r = tmp_path / "p"
    (r / ".devorch").mkdir(parents=True)
    cfg = {"version": 1, "project": {"test_command": f"{sys.executable} -m pytest -q", "sandbox": sandbox}, "providers": {}}
    (r / ".devorch" / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (r / ".devorch" / "policy.yaml").write_text("{}\n", encoding="utf-8")
    return r


def _sandbox_check(root, monkeypatch):
    monkeypatch.setattr("devorch.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    return next(c for c in run_checks(root, {}) if c.name == "샌드박스")


def test_doctor_fails_the_sandbox_check_for_a_root_host_and_names_the_fix(host, tmp_path, monkeypatch):
    host((0, 0))
    c = _sandbox_check(_docker_project(tmp_path, {"mode": "docker", "image": "img:x"}), monkeypatch)
    assert c.status == "fail" and "root" in c.detail and "project.sandbox.user" in c.hint


def test_doctor_is_fine_for_a_root_host_with_a_configured_user(host, tmp_path, monkeypatch):
    host((0, 0))
    c = _sandbox_check(_docker_project(tmp_path, {"mode": "docker", "image": "img:x", "user": "1000:1000"}), monkeypatch)
    assert c.status == "ok" and "1000:1000" in c.detail
