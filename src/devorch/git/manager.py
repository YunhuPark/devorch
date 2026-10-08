"""Thin, shell-free wrapper around the git CLI used by the executor."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

GIT_TIMEOUT_S = 60


LAUNCH_RETRIES = 3


class GitError(RuntimeError):
    pass


def _run_git(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` that retries when Windows refuses to *start* git (WinError 5).

    Observed intermittently (~0.5-1% of launches) for git.exe on one Windows machine, never for python.exe;
    root cause not established (antivirus scanning is a guess). A refused launch means git never ran, so a
    retry cannot repeat a side effect. Anything else — including the final refusal — is raised as is.
    """
    for attempt in range(LAUNCH_RETRIES):
        try:
            return subprocess.run(argv, **kwargs)
        except PermissionError as e:
            if os.name != "nt" or getattr(e, "winerror", None) != 5 or attempt == LAUNCH_RETRIES - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")


class GitManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _git(self, args: list[str], cwd: Path | None = None, input_text: str | None = None, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        try:
            # Bytes in/out, decoded as UTF-8 here: text=True would use the locale codec (cp949 on Korean
            # Windows) and translate \n <-> \r\n, so the diff that gets applied would not be the diff that was reviewed.
            raw = _run_git(
                ["git", *args],
                cwd=str(cwd or self.root),
                input=input_text.encode("utf-8") if input_text is not None else None,
                env=env,
                capture_output=True,
                timeout=GIT_TIMEOUT_S,
            )
            proc = subprocess.CompletedProcess(
                raw.args, raw.returncode,
                raw.stdout.decode("utf-8", errors="replace"), raw.stderr.decode("utf-8", errors="replace"),
            )
        except PermissionError as e:
            raise GitError(f"git 을 실행하지 못함 (접근 거부): {e}") from e
        except FileNotFoundError as e:
            raise GitError("git 실행 파일을 찾을 수 없음") from e
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {' '.join(args[:2])} 시간 초과") from e
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])} 실패: {(proc.stderr or proc.stdout).strip()}")
        return proc

    # ------------------------------------------------------------- checks
    def ensure_ready(self) -> None:
        """Project root must be the top level of a git repo that has at least one commit."""
        top = self._git(["rev-parse", "--show-toplevel"], check=False)
        if top.returncode != 0:
            raise GitError("git 저장소가 아닙니다 (git init 후 첫 커밋이 필요)")
        if Path(top.stdout.strip()).resolve() != self.root:
            raise GitError(f"프로젝트 루트가 저장소 최상위가 아닙니다: {top.stdout.strip()}")
        if self._git(["rev-parse", "--verify", "HEAD"], check=False).returncode != 0:
            raise GitError("커밋이 하나도 없습니다 (첫 커밋이 필요)")

    def is_dirty(self) -> bool:
        return bool(self._git(["status", "--porcelain"]).stdout.strip())

    def head(self, cwd: Path | None = None) -> str:
        return self._git(["rev-parse", "--short", "HEAD"], cwd=cwd).stdout.strip()

    def read_at_head(self, rel_path: str) -> str | None:
        proc = self._git(["show", f"HEAD:{rel_path}"], check=False)
        return proc.stdout if proc.returncode == 0 else None

    # Never part of a captured diff: our own state and files that commonly hold credentials.
    DIFF_EXCLUDES = (
        ".devorch/**", "**/.env", "**/.env.*", ".env", ".env.*",
        "**/*.pem", "**/*.key", "*.pem", "*.key", "**/id_rsa*", "id_rsa*",
    )

    def working_diff(self) -> str:
        """Diff of the working tree (incl. new files) against HEAD, using a throw-away index so the
        user's real index is never touched."""
        specs = [f":(exclude,glob){p}" for p in self.DIFF_EXCLUDES]
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}
            self._git(["read-tree", "HEAD"], env=env)
            self._git(["add", "-A", "--", ".", *specs], env=env)
            return self._git(
                ["diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", "HEAD", "--", ".", *specs],
                env=env,
            ).stdout

    # ---------------------------------------------------------- worktrees
    @staticmethod
    def branch_name(run_id: str, task: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:30].strip("-") or "task"
        return f"devorch/{run_id}-{slug}"

    def branch_exists(self, branch: str) -> bool:
        return self._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0

    def unique_branch(self, base: str) -> str:
        """``base`` if free, else ``base-2``, ``base-3`` … so a new attempt never collides with (or clobbers) an old branch."""
        name, n = base, 1
        while self.branch_exists(name):
            n += 1
            name = f"{base}-{n}"
        return name

    def add_worktree(self, branch: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(["worktree", "add", "-b", branch, str(path), "HEAD"])

    def remove_worktree(self, path: Path) -> None:
        self._git(["worktree", "remove", "--force", str(path)], check=False)

    def delete_branch(self, branch: str) -> None:
        self._git(["branch", "-D", branch], check=False)

    # -------------------------------------------------------- change ops
    def apply_diff(self, worktree: Path, diff: str) -> list[str]:
        """``git apply`` the diff inside ``worktree`` (checked first). Returns changed paths."""
        self._git(["apply", "--check", "--whitespace=nowarn", "-"], cwd=worktree, input_text=diff)
        self._git(["apply", "--whitespace=nowarn", "-"], cwd=worktree, input_text=diff)
        status = self._git(["status", "--porcelain"], cwd=worktree).stdout.splitlines()
        return [line[3:] for line in status]

    def commit(self, worktree: Path, message: str) -> str:
        self._git(["add", "-A"], cwd=worktree)
        if not self._git(["status", "--porcelain"], cwd=worktree).stdout.strip():
            raise GitError("커밋할 변경이 없음")
        identity: list[str] = []
        if not self._git(["config", "user.email"], cwd=worktree, check=False).stdout.strip():
            identity = ["-c", "user.name=DevOrch", "-c", "user.email=devorch@localhost"]
        self._git([*identity, "commit", "-q", "-m", message], cwd=worktree)
        return self.head(cwd=worktree)
