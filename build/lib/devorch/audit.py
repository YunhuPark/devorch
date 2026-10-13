"""Append-only JSONL audit log. Everything is passed through the secret redactor first."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devorch.security.secret_redactor import redact_obj


class AuditLogger:
    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, run_id: str, event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": run_id,
            "event": event,
            "data": redact_obj(data or {}),
        }
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record
