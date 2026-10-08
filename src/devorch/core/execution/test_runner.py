"""Runs an allowlisted test command inside a worktree.

NOT a sandbox: the process still has the user's network and filesystem access.
What it does guarantee: no shell, allowlist check, timeout (process group
killed), output cap, and API keys / tokens stripped from the environment.
A Docker sandbox is a later phase.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from devorch.security.command_policy import check_command, split_command

OUTPUT_TAIL_CHARS = 4000
_SENSITIVE_ENV = re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|credential|authorization)")


@dataclass
class TestResult:
    __test__ = False  # not a pytest class

    ok: bool
    command: str
    exit_code: int | None
    output_tail: str
    reason: str = ""

    def summary(self) -> str:
        if self.reason:
            return self.reason
        return f"`{self.command}` exit={self.exit_code}"


def detect_test_command(root: Path) -> str | None:
    root = Path(root)
    pkg = root / "package.json"
    if pkg.exists() and '"test"' in pkg.read_text(encoding="utf-8", errors="ignore"):
        return "npm test"
    if (
        (root / "pytest.ini").exists()
        or (root / "tests").is_dir()
        or (root / "conftest.py").exists()
        or any(root.glob("test_*.py"))
        or any(root.glob("*_test.py"))
    ):
        return "pytest"
    if (root / "pyproject.toml").exists() and "pytest" in (root / "pyproject.toml").read_text(encoding="utf-8", errors="ignore"):
        return "pytest"
    if (root / "pom.xml").exists():
        return "mvn test"
    if (root / "gradlew").exists():
        return "./gradlew test"
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return "gradle test"
    return None


def safe_env(extra_blocked: set[str] | None = None) -> dict[str, str]:
    blocked = extra_blocked or set()
    return {k: v for k, v in os.environ.items() if k not in blocked and not _SENSITIVE_ENV.search(k)}


def _new_group_kwargs() -> dict:
    """Put the test process in its own group/session so a timeout can take its children down too."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and its descendants (best effort; the process itself is always killed)."""
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            pass
        try:
            proc.kill()  # taskkill missing/failed, or only the child tree was left
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def run_tests(
    command: str,
    cwd: Path,
    allowed: list[str],
    timeout_s: float,
    extra_blocked_env: set[str] | None = None,
) -> TestResult:
    ok, why = check_command(command, allowed)
    if not ok:
        return TestResult(False, command, None, "", f"테스트 명령이 정책에 의해 거부됨: {why}")

    argv = split_command(command)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=safe_env(extra_blocked_env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_new_group_kwargs(),
        )
    except FileNotFoundError:
        return TestResult(False, command, None, "", f"실행 파일을 찾을 수 없음: {argv[0]}")
    except OSError as e:  # e.g. PermissionError — the tests did not run, which must never read as "passed"
        return TestResult(False, command, None, "", f"테스트 명령을 실행하지 못함: {e}")

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        await proc.wait()
        return TestResult(False, command, None, "", f"테스트가 {timeout_s:.0f}초 내에 끝나지 않아 중단됨")

    text = out.decode("utf-8", errors="replace")
    return TestResult(proc.returncode == 0, command, proc.returncode, text[-OUTPUT_TAIL_CHARS:])
