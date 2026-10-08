"""Docker sandbox for the test step.

Fail-closed: if ``mode: docker`` is configured and Docker can't be used, tests FAIL — they are
never silently run on the host instead.

What the container gets: the throwaway worktree at /work (read-write), no network by default,
no environment variables from the host, all capabilities dropped, no-new-privileges, non-root
uid/gid, memory/cpu/pids limits, a tmpfs /tmp. What it does NOT get: the main repo's .git,
the user's home, or any other host path.

Not covered: kernel/Docker escapes (a container is not a VM), and with ``network: bridge`` the
code can reach the network. Dependencies must already be inside the image (no network = no
``npm install``).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import uuid
from pathlib import Path

from pydantic import BaseModel, field_validator

from devorch.core.execution.test_runner import OUTPUT_TAIL_CHARS, TestResult
from devorch.security.command_policy import check_command


class SandboxConfig(BaseModel):
    mode: str = "none"  # none | docker
    image: str | None = None  # required for docker — no guessed default
    network: str = "none"  # none | bridge
    memory: str = "1g"
    cpus: str = "2"
    pids: int = 256

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in ("none", "docker"):
            raise ValueError("sandbox.mode 는 none | docker 여야 합니다")
        return v

    @field_validator("network")
    @classmethod
    def _net(cls, v: str) -> str:
        if v not in ("none", "bridge"):
            raise ValueError("sandbox.network 는 none | bridge 여야 합니다")
        return v

    @property
    def enabled(self) -> bool:
        return self.mode == "docker"


def load_sandbox(config: dict) -> SandboxConfig:
    return SandboxConfig.model_validate((config.get("project") or {}).get("sandbox") or {})


def build_docker_args(cfg: SandboxConfig, name: str, cwd: Path, command: str, uid: int, gid: int) -> list[str]:
    assert cfg.image
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", cfg.network,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", cfg.memory, "--cpus", cfg.cpus, "--pids-limit", str(cfg.pids),
        "--user", f"{uid}:{gid}",
        "--tmpfs", "/tmp:rw,size=256m",
        "-e", "HOME=/tmp",
        "-v", f"{Path(cwd).resolve()}:/work", "-w", "/work",
        cfg.image, *shlex.split(command),
    ]


async def _docker(*args: str, timeout: float = 20) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timeout"
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


async def sandbox_status(cfg: SandboxConfig) -> list[tuple[str, bool, str]]:
    """(check, ok, detail) — no code is run inside the image."""
    checks: list[tuple[str, bool, str]] = []
    if not shutil.which("docker"):
        return [("docker CLI", False, "docker 실행 파일을 찾을 수 없음")]
    checks.append(("docker CLI", True, shutil.which("docker") or ""))
    code, out = await _docker("info", "--format", "{{.ServerVersion}}")
    checks.append(("docker daemon", code == 0, out.splitlines()[-1] if out else ""))
    if code != 0:
        return checks
    if not cfg.image:
        checks.append(("image 설정", False, "config.yaml 의 project.sandbox.image 가 필요함"))
    else:
        code, out = await _docker("image", "inspect", cfg.image, "--format", "{{.Id}}")
        checks.append((f"image {cfg.image}", code == 0, "있음" if code == 0 else "로컬에 없음 (docker pull 필요)"))
    return checks


async def run_tests_docker(
    command: str, cwd: Path, allowed: list[str], timeout_s: float, cfg: SandboxConfig
) -> TestResult:
    ok, why = check_command(command, allowed)
    if not ok:
        return TestResult(False, command, None, "", f"테스트 명령이 정책에 의해 거부됨: {why}")
    if not cfg.image:
        return TestResult(False, command, None, "", "sandbox.mode=docker 인데 project.sandbox.image 가 설정되지 않음 (호스트 실행으로 대체하지 않음)")
    if not shutil.which("docker"):
        return TestResult(False, command, None, "", "sandbox.mode=docker 인데 docker 를 찾을 수 없음 (호스트 실행으로 대체하지 않음)")

    name = f"devorch-{uuid.uuid4().hex[:12]}"
    args = build_docker_args(cfg, name, cwd, command, os.getuid(), os.getgid())
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        await _docker("rm", "-f", name)  # killing the CLI alone would leave the container running
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return TestResult(False, command, None, "", f"[docker] 테스트가 {timeout_s:.0f}초 내에 끝나지 않아 컨테이너를 중단함")

    text = out.decode("utf-8", errors="replace")
    if proc.returncode == 125 or (proc.returncode in (126, 127) and "docker:" in text[:200]):
        # 125 = docker itself failed (daemon down, bad image, ...) — not a test result
        return TestResult(False, command, proc.returncode, text[-OUTPUT_TAIL_CHARS:], "[docker] 컨테이너를 시작하지 못함 — 테스트가 실행되지 않음")
    return TestResult(proc.returncode == 0, command, proc.returncode, text[-OUTPUT_TAIL_CHARS:])
