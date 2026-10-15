"""Persistent task records for the MCP / approval flow.

Approvals live in a *separate* file from the task record. The MCP tools only ever
read approvals; nothing reachable through a tool call writes one.

Both files are SIGNED (HMAC, key outside the project: security/signing.py). Before that, writing one JSON file by hand was enough to approve
a change or to mark it tested. A record or approval whose signature is missing or wrong is rejected / ignored, never trusted.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from devorch.config import devorch_dir
from devorch.core.orchestrator.engine import WorkflowRun
from devorch.security.signing import SigningError, sign, verify

ID_RE = re.compile(r"^[a-f0-9]{8}$")


class StoreError(ValueError):
    pass


class TaskRecord(BaseModel):
    run: WorkflowRun
    diff: str = ""
    diff_sha: str | None = None
    reviewed_sha: str | None = None
    tested_sha: str | None = None
    tested_tree: str | None = None  # content hash of the tested worktree: any change after the tests invalidates them
    review_rounds: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def id(self) -> str:
        return self.run.id


class Approval(BaseModel):
    task_id: str
    diff_sha: str
    approved: bool
    via: str
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    sig: str = ""

    def payload(self) -> str:
        return f"approval|{self.task_id}|{self.diff_sha}|{self.approved}|{self.via}|{self.at}"


def new_task_id() -> str:
    return uuid.uuid4().hex[:8]


def check_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not ID_RE.match(task_id):
        raise StoreError(f"잘못된 task_id 형식입니다: {task_id!r}")
    return task_id


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.dir = devorch_dir(Path(root)) / "tasks"

    def _path(self, task_id: str, suffix: str = ".json") -> Path:
        return self.dir / f"{check_id(task_id)}{suffix}"

    @staticmethod
    def _canonical(data: dict) -> str:
        return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def save(self, record: TaskRecord) -> None:
        data = json.loads(record.model_dump_json())
        try:
            data["_sig"] = sign(self._canonical(data))
        except SigningError as e:
            raise StoreError(str(e)) from e
        _atomic_write(self._path(record.id), json.dumps(data, indent=2, ensure_ascii=False))

    def load(self, task_id: str) -> TaskRecord:
        path = self._path(task_id)
        if not path.exists():
            raise StoreError(f"task {task_id} 를 찾을 수 없습니다")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise StoreError(f"task {task_id} 의 기록을 읽을 수 없습니다: {e}") from e
        sig = data.pop("_sig", "") if isinstance(data, dict) else ""
        if not isinstance(data, dict) or not verify(self._canonical(data), sig):
            raise StoreError(
                f"task {task_id} 의 기록 서명이 없거나 맞지 않습니다 — DevOrch 밖에서 수정됐거나 이전 버전의 기록입니다. "
                "신뢰하지 않으므로 analyze_task 로 새로 시작하세요."
            )
        return TaskRecord.model_validate(data)

    def list_ids(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json") if ID_RE.match(p.stem))

    # -- approvals (written by the human-facing CLI only) -------------------
    def save_approval(self, approval: Approval) -> None:
        try:
            signed = approval.model_copy(update={"sig": sign(approval.payload())})
        except SigningError as e:
            raise StoreError(f"승인을 서명하지 못해 저장하지 않았습니다: {e}") from e
        _atomic_write(self._path(approval.task_id, ".approval.json"), signed.model_dump_json(indent=2))

    def _read_approval(self, task_id: str) -> Approval | None:
        path = self._path(task_id, ".approval.json")
        if not path.exists():
            return None
        try:
            return Approval.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return None

    def load_approval(self, task_id: str) -> Approval | None:
        """The human's approval, or None. A file whose signature does not verify (written by hand, e.g. by the agent) counts as NO approval."""
        ap = self._read_approval(task_id)
        return ap if ap is not None and verify(ap.payload(), ap.sig) else None

    def approval_file_rejected(self, task_id: str) -> bool:
        """True when an approval file exists but is not trusted (unreadable or the signature does not verify)."""
        return self._path(task_id, ".approval.json").exists() and self.load_approval(task_id) is None
