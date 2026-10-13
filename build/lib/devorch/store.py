"""Persistent task records for the MCP / approval flow.

Approvals live in a *separate* file from the task record. The MCP tools only ever
read approvals; nothing reachable through a tool call writes one.
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

ID_RE = re.compile(r"^[a-f0-9]{8}$")


class StoreError(ValueError):
    pass


class TaskRecord(BaseModel):
    run: WorkflowRun
    diff: str = ""
    diff_sha: str | None = None
    reviewed_sha: str | None = None
    tested_sha: str | None = None
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

    def save(self, record: TaskRecord) -> None:
        _atomic_write(self._path(record.id), record.model_dump_json(indent=2))

    def load(self, task_id: str) -> TaskRecord:
        path = self._path(task_id)
        if not path.exists():
            raise StoreError(f"task {task_id} 를 찾을 수 없습니다")
        return TaskRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_ids(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json") if ID_RE.match(p.stem))

    # -- approvals (written by the human-facing CLI only) -------------------
    def save_approval(self, approval: Approval) -> None:
        _atomic_write(self._path(approval.task_id, ".approval.json"), approval.model_dump_json(indent=2))

    def load_approval(self, task_id: str) -> Approval | None:
        path = self._path(task_id, ".approval.json")
        if not path.exists():
            return None
        try:
            return Approval.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return None  # unreadable approval counts as no approval
