"""Executor: turns an approved implementation into a branch with a tested commit.

Pipeline (all inside a throw-away git worktree, never the user's working tree):
    extract diff → validate paths → git worktree add → git apply → run tests → commit

The user's checked-out branch is never modified; the result is a branch
``devorch/<run>-<slug>`` that the user merges (or deletes) themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from devorch.config import Policy, devorch_dir
from devorch.core.execution.patch import PatchError, extract_diff, validate_diff
from devorch.core.execution.sandbox import SandboxConfig, run_tests_docker
from devorch.core.execution.test_runner import detect_test_command, run_tests
from devorch.git.manager import GitError, GitManager
from devorch.security.path_guard import PathEscapeError, resolve_inside
from devorch.security.secret_redactor import has_secret_literal

if TYPE_CHECKING:
    from devorch.core.orchestrator.engine import WorkflowRun

MAX_CONTEXT_FILES = 8
MAX_FILE_BYTES = 12_000
MAX_CONTEXT_BYTES = 48_000


class ExecutionError(RuntimeError):
    pass


class Executor:
    def __init__(
        self,
        root: Path,
        policy: Policy,
        test_command: str | None = None,
        skip_tests: bool = False,
        blocked_env: set[str] | None = None,
        sandbox: SandboxConfig | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = policy
        self.git = GitManager(self.root)
        self.test_command = test_command
        self.skip_tests = skip_tests
        self.blocked_env = blocked_env or set()
        self.sandbox = sandbox or SandboxConfig()
        self._worktrees: dict[str, Path] = {}

    def _worktree(self, run: "WorkflowRun") -> Path | None:
        path = self._worktrees.get(run.id)
        if path is None and run.worktree and Path(run.worktree).is_dir():
            path = Path(run.worktree)  # created by an earlier process
            self._worktrees[run.id] = path
        return path

    def discard(self, run: "WorkflowRun") -> None:
        """Remove a run's temporary worktree and branch (no-op if none)."""
        path = self._worktree(run)
        if path is not None:
            self.git.remove_worktree(path)
        if run.branch and not run.commit:  # a committed branch belongs to the user — never delete it
            self.git.delete_branch(run.branch)
        self._worktrees.pop(run.id, None)
        run.worktree = None
        if not run.commit:
            run.branch = None

    # ---------------------------------------------------------- preflight
    def ensure_ready(self) -> None:
        try:
            self.git.ensure_ready()
        except GitError as e:
            raise ExecutionError(str(e)) from e

    # ------------------------------------------------------------ context
    def file_context(self, run: "WorkflowRun") -> str:
        """Contents (as of HEAD) of the files the change will most likely touch."""
        if not run.impact:
            return ""
        parts: list[str] = []
        skipped: list[str] = []
        total = 0
        for rel in run.impact.direct_files[:MAX_CONTEXT_FILES]:
            try:
                resolve_inside(self.root, rel)
            except PathEscapeError:
                continue
            text = self.git.read_at_head(rel)
            if text is None:
                continue
            if has_secret_literal(text):
                skipped.append(f"{rel} (credential 형태의 문자열 포함 — 제외)")
                continue
            if len(text.encode("utf-8")) > MAX_FILE_BYTES:
                skipped.append(f"{rel} (너무 큼 — 제외)")
                continue
            if total + len(text) > MAX_CONTEXT_BYTES:
                skipped.append(f"{rel} (컨텍스트 한도 — 제외)")
                continue
            total += len(text)
            parts.append(f"=== {rel} ===\n{text}")
        out = "\n".join(parts)
        if skipped:
            out += "\n\n(Not shown: " + "; ".join(skipped) + ")"
        return out

    # -------------------------------------------------------------- apply
    async def apply(self, run: "WorkflowRun") -> None:
        diff = extract_diff(run.implementation)
        if diff is None:
            raise ExecutionError("구현 결과에서 unified diff를 찾지 못함")
        try:
            paths = validate_diff(diff)
        except PatchError as e:
            raise ExecutionError(f"diff 검증 실패: {e}") from e

        # make sure worktrees never show up as untracked noise in the user's repo
        gi = devorch_dir(self.root) / ".gitignore"
        gi.parent.mkdir(parents=True, exist_ok=True)
        if not gi.exists():
            gi.write_text("audit.jsonl\naudit.head\naudit.jsonl.lock\nruns/\nworktrees/\n", encoding="utf-8")

        branch = self.git.unique_branch(GitManager.branch_name(run.id, run.task_text))
        path = devorch_dir(self.root) / "worktrees" / run.id
        try:
            self.git.add_worktree(branch, path)
        except GitError as e:
            raise ExecutionError(str(e)) from e
        self._worktrees[run.id] = path
        run.branch, run.worktree = branch, str(path)

        try:
            changed = self.git.apply_diff(path, diff)
        except GitError as e:
            self.git.remove_worktree(path)
            self.git.delete_branch(branch)
            run.worktree = None
            raise ExecutionError(f"diff를 적용하지 못함: {e}") from e
        run.notes.append(f"worktree {branch}: {len(changed)}개 파일 변경 ({', '.join(paths[:5])}{'…' if len(paths) > 5 else ''})")

    # --------------------------------------------------------------- test
    async def test(self, run: "WorkflowRun") -> bool:
        if self.skip_tests:
            run.notes.append("테스트 생략: 사용자가 --no-tests 로 요청")
            return True
        path = self._worktree(run)
        if path is None:
            run.test_summary = "적용된 worktree가 없음"
            return False
        command = self.test_command or detect_test_command(path)
        if not command:
            run.test_summary = "테스트 명령을 찾지 못함 (config.yaml의 project.test_command 설정 또는 --no-tests)"
            return False
        timeout = float(self.policy.limits.command_timeout_s)
        if self.sandbox.enabled:
            result = await run_tests_docker(command, path, self.policy.allowed_commands, timeout, self.sandbox)
            run.notes.append(f"테스트는 docker 샌드박스에서 실행 (image={self.sandbox.image}, network={self.sandbox.network})")
        else:
            result = await run_tests(command, path, self.policy.allowed_commands, timeout, self.blocked_env)
        run.test_summary = result.summary()
        run.test_output = result.output_tail
        if result.ok:
            touched = self._reviewed_files_changed(path, run)
            if touched:
                # test code is the agent's own code: a passing run on files the test itself rewrote proves nothing about the reviewed change
                shown = ", ".join(touched[:5]) + (f" 외 {len(touched) - 5}개" if len(touched) > 5 else "")
                run.test_summary = f"테스트 실행 중에 검토된 파일이 바뀌었습니다({shown}) — 통과로 인정하지 않음"
                run.notes.append(run.test_summary)
                return False
        if not result.ok:
            run.notes.append(f"테스트 실패 — 확인용으로 worktree 보존: {path} (branch {run.branch}, 커밋 없음)")
        return result.ok

    def _reviewed_files_changed(self, path: Path, run: "WorkflowRun") -> list[str]:
        """Paths of the reviewed change whose content differs from the reviewed snapshot right now (untracked artifacts do not count)."""
        diff = extract_diff(run.implementation)
        if diff is None:
            return []
        try:
            expected = self.git.expected_tree(path, diff)
            return self.git.changed_paths(expected, self.git.tracked_tree(path, expected))
        except GitError:
            return ["(확인하지 못함)"]  # cannot tell: do not vouch for it

    # ------------------------------------------------------------- commit
    def tree_hash(self, run: "WorkflowRun") -> str | None:
        """Content hash of the run's worktree (None if it is gone). Taken right after the tests passed and compared again before the commit:
        a worktree that changed in between is NOT what was tested or approved."""
        path = self._worktree(run)
        if path is None:
            return None
        try:
            return self.git.tree_hash(path)
        except GitError:
            return None

    async def commit(self, run: "WorkflowRun") -> None:
        path = self._worktree(run)
        if path is None:
            raise ExecutionError("커밋할 worktree가 없음")
        # Commit exactly the reviewed change, not whatever the test run left behind (compiled caches, files a test wrote or rewrote):
        # rebuild the worktree from the base commit and re-apply the reviewed diff.
        diff = extract_diff(run.implementation)
        if diff is None:
            raise ExecutionError("구현 결과에서 unified diff를 찾지 못함")
        try:
            self.git.reset_clean(path)
            self.git.apply_diff(path, diff)
        except GitError as e:
            raise ExecutionError(f"검토된 diff 로 커밋 내용을 다시 만들지 못함: {e}") from e
        title = run.task_text.strip().splitlines()[0][:60]
        kind = {"feature": "feat", "bugfix": "fix", "refactor": "refactor", "test": "test", "docs": "docs"}.get(
            run.analysis.task_type.value if run.analysis else "", "chore"
        )
        reviewers = ", ".join(f"{s.role}={s.provider}" for s in run.plan.review_steps) if run.plan else "-"
        message = (
            f"{kind}: {title}\n\n"
            f"DevOrch-Run: {run.id}\n"
            f"Risk: {run.risk.level.value.upper() if run.risk else '?'}\n"
            f"Reviews: {reviewers}\n"
            f"Evaluation: {run.evaluation.status if run.evaluation else '-'}\n"
            f"Human-Approved: {'yes' if run.approved else 'not required'}\n"
        )
        try:
            run.commit = self.git.commit(path, message)
        except GitError as e:
            raise ExecutionError(str(e)) from e
        # the branch keeps the commit; the temporary checkout is no longer needed
        self.git.remove_worktree(path)
        run.worktree = None
        self._worktrees.pop(run.id, None)
