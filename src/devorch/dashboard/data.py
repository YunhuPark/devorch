"""Read-only data layer for the dashboard. Never writes, never calls an LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from devorch.config import devorch_dir
from devorch.service import ControlPlane, ControlPlaneError
from devorch.store import StoreError, check_id

MAX_AUDIT = 500


class Data:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.plane = ControlPlane.from_project(self.root, mock=True)  # mock registry: no keys needed, never called

    def tasks(self) -> list[dict[str, Any]]:
        out = []
        for tid in self.plane.store.list_ids():
            try:
                s = self.plane.task_summary(tid)
                rec = self.plane.store.load(tid)
            except (StoreError, ControlPlaneError, ValueError, AssertionError) as e:
                out.append({"task_id": tid, "error": f"읽을 수 없음: {type(e).__name__}"})
                continue
            s["created_at"] = rec.created_at
            s["state"] = rec.run.state.value
            out.append(s)
        return sorted(out, key=lambda t: t.get("created_at", ""), reverse=True)

    def task(self, task_id: str) -> dict[str, Any]:
        check_id(task_id)
        s = self.plane.task_summary(task_id)
        rec = self.plane.store.load(task_id)
        run = rec.run
        ev = run.evaluation
        s.update(
            state=run.state.value,
            created_at=rec.created_at,
            risk_score=run.risk.score if run.risk else None,
            risk_factors=[f.model_dump() for f in (run.risk.factors if run.risk else [])],
            plan=[f"{x.role}={x.provider}" for x in (run.plan.steps if run.plan else [])],
            approval_reasons=run.plan.approval_reasons if run.plan else [],
            evaluation=ev.model_dump(mode="json") if ev else None,
            test_summary=run.test_summary,
            notes=run.notes,
            diff=rec.diff,
        )
        return s

    def audit(self, task_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        path = devorch_dir(self.root) / "audit.jsonl"
        if not path.exists():
            return []
        limit = max(1, min(int(limit), MAX_AUDIT))
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # a corrupt line never hides the rest
            if task_id is None or ev.get("run_id") == task_id:
                events.append(ev)
        return events[-limit:][::-1]

    def bench_runs(self) -> list[dict[str, Any]]:
        base = devorch_dir(self.root) / "bench"
        if not base.exists():
            return []
        runs = []
        for d in sorted(base.iterdir(), reverse=True):
            rp, jp = d / "report.md", d / "results.json"
            if not (rp.exists() and jp.exists()):
                continue
            try:
                meta = json.loads(jp.read_text(encoding="utf-8"))
            except ValueError:
                continue
            runs.append({"id": d.name, "mock": meta.get("mock"), "modes": meta.get("modes"), "summaries": meta.get("summaries"), "report": rp.read_text(encoding="utf-8")})
        return runs
