"""Docker sandbox for the test step.

Fail-closed: if ``mode: docker`` is configured and Docker can't be used, tests FAIL — they are
never silently run on the host instead.

What the container gets: the throwaway worktree at /work (read-write), no network by default,
no environment variables from the host, all capabilities dropped, no-new-privileges, non-root
uid/gid, memory/cpu/pids limits, a tmpfs /tmp. Never root: the container runs as the host user's uid, and when that is root (uid 0) the tests
are refused unless ``project.sandbox.user`` names a non-root ``uid:gid`` — root inside the container could write outside /work and /tmp
(reproduced 2026-11-24 on Linux as root: 2 of the 10 isolation checks failed). What it does NOT get: the main repo's .git,
the user's home, or any other host path.

Not covered: kernel/Docker escapes (a container is not a VM), and with ``network: bridge`` the
code can reach the network. Dependencies must already be inside the image (no network = no
``npm install``).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path

from pydantic import BaseModel, field_validator

from devorch.core.execution.test_runner import OUTPUT_TAIL_CHARS, TestResult
from devorch.security.command_policy import check_command, split_command


class SandboxConfig(BaseModel):
    mode: str = "none"  # none | docker
    image: str | None = None  # required for docker — no guessed default
    network: str = "none"  # none | bridge
    memory: str = "1g"
    cpus: str = "2"
    pids: int = 256
    user: str | None = None  # "uid:gid" to run as instead of the host user's; never 0 — needed when DevOrch itself runs as root

    @field_validator("user")
    @classmethod
    def _user(cls, v: str | None) -> str | None:
        if v is None:
            return v
        parts = str(v).strip().split(":")
        if len(parts) != 2 or not all(x.isdigit() for x in parts):
            raise ValueError('sandbox.user 는 "uid:gid" 숫자 형식이어야 합니다(예: "1000:1000")')
        if int(parts[0]) == 0 or int(parts[1]) == 0:
            raise ValueError("sandbox.user 에 root(0)는 쓸 수 없습니다 — 컨테이너 안에서 root 면 /work·/tmp 밖에도 쓸 수 있습니다")
        return f"{int(parts[0])}:{int(parts[1])}"

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


FALLBACK_UID_GID = (1000, 1000)
LAUNCH_RETRIES = 3


ROOT_REFUSED = (
    "DevOrch 가 root(uid 0)로 실행 중이라 Docker 샌드박스도 root 로 테스트를 돌리게 됩니다 — 컨테이너 안에서 /work·/tmp 밖에 쓸 수 있어 "
    "격리가 약해지므로 실행하지 않았습니다(호스트 실행으로 대체하지 않음). 일반 사용자로 DevOrch 를 실행하거나, config.yaml 의 "
    'project.sandbox.user 에 root 가 아닌 "uid:gid"(예: "1000:1000")를 정하세요 — 그 uid 가 작업 폴더를 읽고 쓸 수 있어야 합니다.'
)


def host_uid_gid() -> tuple[int, int] | None:
    """The host user's uid/gid, or None where the OS has none (Windows)."""
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return os.getuid(), os.getgid()
    return None


def container_user(cfg: SandboxConfig | None = None) -> tuple[int, int] | str:
    """uid:gid the container runs as — never root. Returns the refusal message instead when the only choice would be root.

    Order: ``project.sandbox.user`` if set (validated non-zero) → the host user's uid/gid → on Windows (no uid/gid) a fixed unprivileged id.
    The host user's id keeps files written in /work owned by that user. When the host user is root that id would make the container root,
    so it is refused rather than silently swapped: a substitute id usually cannot write the root-owned worktree, which would fail the tests
    in a confusing way. Verified 2026-10-15 on Docker Desktop for Windows (WSL2 backend): the fallback id reads files created on the host and
    writes files that appear on the host (tests/test_docker_sandbox.py). Other setups (Docker on macOS, rootless Docker, network drives) have not
    been tried. Rootless Docker maps container root to the host user, but that is not detected here — use ``user`` there too.
    """
    if cfg is not None and cfg.user:
        uid, gid = (int(x) for x in cfg.user.split(":"))
        return uid, gid
    host = host_uid_gid()
    if host is None:
        return FALLBACK_UID_GID
    if host[0] == 0:
        return ROOT_REFUSED
    return host


def build_docker_args(cfg: SandboxConfig, name: str, cwd: Path, command: str, uid: int, gid: int, env: dict[str, str] | None = None) -> list[str]:
    assert cfg.image
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", cfg.network,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", cfg.memory, "--cpus", cfg.cpus, "--pids-limit", str(cfg.pids),
        "--user", f"{uid}:{gid}",
        "--tmpfs", "/tmp:rw,size=256m",
        "-e", "HOME=/tmp",
        *[a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")],  # project.test_env only — never the host's variables
        "-v", f"{Path(cwd).resolve()}:/work", "-w", "/work",
        cfg.image, *split_command(command, posix=True),
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
    user = container_user(cfg)
    checks.append(("컨테이너 사용자", not isinstance(user, str), user if isinstance(user, str) else f"{user[0]}:{user[1]} (root 아님)"))
    if not cfg.image:
        checks.append(("image 설정", False, "config.yaml 의 project.sandbox.image 가 필요함"))
    else:
        code, out = await _docker("image", "inspect", cfg.image, "--format", "{{.Id}}")
        checks.append((f"image {cfg.image}", code == 0, "있음" if code == 0 else "로컬에 없음 (docker pull 필요)"))
    return checks


async def run_tests_docker(
    command: str, cwd: Path, allowed: list[str], timeout_s: float, cfg: SandboxConfig, env: dict[str, str] | None = None
) -> TestResult:
    ok, why = check_command(command, allowed, posix=True)  # the command runs in a Linux container
    if not ok:
        return TestResult(False, command, None, "", f"테스트 명령이 정책에 의해 거부됨: {why}")
    if not cfg.image:
        return TestResult(False, command, None, "", "sandbox.mode=docker 인데 project.sandbox.image 가 설정되지 않음 (호스트 실행으로 대체하지 않음)")
    if not shutil.which("docker"):
        return TestResult(False, command, None, "", "sandbox.mode=docker 인데 docker 를 찾을 수 없음 (호스트 실행으로 대체하지 않음)")

    user = container_user(cfg)
    if isinstance(user, str):
        return TestResult(False, command, None, "", f"[docker] {user}")
    uid, gid = user
    name = f"devorch-{uuid.uuid4().hex[:12]}"
    args = build_docker_args(cfg, name, cwd, command, uid, gid, env)
    proc = None
    for attempt in range(LAUNCH_RETRIES):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            break
        except PermissionError as e:
            # Windows intermittently refuses to START an executable (seen for git.exe and docker.exe, cause unknown). A refused launch means
            # nothing ran, so retrying repeats no side effect; after the last attempt it is a failed run, never a pass and never an exception.
            if attempt == LAUNCH_RETRIES - 1:
                return TestResult(False, command, None, "", f"[docker] docker 를 실행하지 못함(접근 거부): {e}")
            await asyncio.sleep(0.2 * (attempt + 1))
        except OSError as e:
            return TestResult(False, command, None, "", f"[docker] docker 를 실행하지 못함: {e}")
    assert proc is not None
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
