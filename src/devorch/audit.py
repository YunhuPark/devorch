"""Append-only JSONL audit log, tamper-EVIDENT. Everything is passed through the secret redactor first.

Each record carries ``prev`` (the previous record's ``mac``) and its own ``mac`` = HMAC(key outside the project, prev + the record). Editing,
deleting or reordering a record breaks the chain at that point, and a record added by hand cannot be signed without the key. The latest ``mac`` and
the record count are also kept in a signed ``audit.head`` file, so cutting records off the END of the log is noticed too.

What this is NOT: prevention. Anything running as the same OS user can still delete the whole log or both files (no log, nothing to verify), and
can read the key unless tests run in the Docker sandbox. It makes quiet tampering with an existing log detectable by ``devorch audit``.
Old logs without ``mac`` are accepted only as a legacy prefix before the first chained record.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devorch.security.secret_redactor import redact_obj
from devorch.security.signing import SigningError, sign, verify


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _chain_payload(prev: str, record: dict[str, Any]) -> str:
    return f"audit|{prev}|{_canonical(record)}"


def _head_payload(count: int, mac: str) -> str:
    return f"audit-head|{count}|{mac}"


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Exclusive cross-process lock (the MCP server and the CLI may both append), so two writers can never fork the chain."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fh:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # blocks (retrying) until it holds the lock
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _last_line(path: Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block, data = 8192, b""
            pos = size
            while pos > 0:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
                stripped = data.rstrip(b"\r\n")
                if b"\n" in stripped:
                    return stripped.rsplit(b"\n", 1)[1]
            return data.rstrip(b"\r\n")
    except FileNotFoundError:
        return b""


class AuditLogger:
    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.parent.name == ".devorch":
                from devorch.config import ensure_gitignore  # an older project's .gitignore does not know audit.head / the lock file

                ensure_gitignore(self.path.parent)
            self.head_path = self.path.with_name(self.path.stem + ".head")
            self.lock_path = self.path.with_name(self.path.name + ".lock")

    def log(self, run_id: str, event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": run_id,
            "event": event,
            "data": redact_obj(data or {}),
        }
        if self.path:
            with _locked(self.lock_path):
                self._append(record)
        return record

    def _append(self, record: dict[str, Any]) -> None:
        last = _last_line(self.path)
        prev, seq = "", 0
        if last:
            try:
                obj = json.loads(last.decode("utf-8"))
                prev = str(obj.get("mac", ""))
                seq = obj["seq"] if isinstance(obj.get("seq"), int) else self._count_lines()  # first chained record after a legacy log: count once
            except (ValueError, AttributeError, KeyError):
                seq = self._count_lines()
        seq += 1  # = this record's line number
        # round-trip so the signed text is exactly what a reader will parse back (default=str, unicode)
        body = json.loads(json.dumps({**record, "seq": seq}, ensure_ascii=False, default=str))
        try:
            mac = sign(_chain_payload(prev, body))
        except SigningError:
            mac = ""  # never lose an event because the key is unavailable; `devorch audit` reports unsigned records
        line = json.dumps({**body, "prev": prev, "mac": mac}, ensure_ascii=False)
        with open(self.path, "ab") as fh:
            fh.write(line.encode("utf-8") + b"\n")
        if mac:
            self._write_head(seq, mac)

    def _count_lines(self) -> int:
        with open(self.path, "rb") as fh:
            return sum(1 for ln in fh if ln.strip())

    def _write_head(self, count: int, mac: str) -> None:
        try:
            sig = sign(_head_payload(count, mac))
        except SigningError:
            return
        tmp = self.head_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"count": count, "mac": mac, "sig": sig}), encoding="utf-8")
        os.replace(tmp, self.head_path)


@dataclass
class AuditReport:
    total: int = 0
    legacy: int = 0  # lines before the first chained record (older versions wrote no signature)
    chained: int = 0
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def verify_audit(path: Path) -> AuditReport:
    """Check the whole chain. ``ok`` is False if any record was edited, removed (from the middle or the end), reordered, added without the key or left
    unsigned after the chain started. A missing log is not a failure (there is nothing to verify)."""
    rep = AuditReport()
    path = Path(path)
    if not path.exists():
        rep.warnings.append("감사 로그가 없습니다(검증할 것이 없음)")
        return rep
    try:
        raw = path.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError as e:
        rep.ok = False
        rep.problems.append(f"감사 로그를 읽지 못했습니다: {e}")
        return rep
    prev, started, last_mac = "", False, ""
    for n, line in enumerate((ln for ln in raw if ln.strip()), 1):
        rep.total = n
        try:
            rec = json.loads(line)
            assert isinstance(rec, dict)
        except (ValueError, AssertionError):
            rep.ok = False
            rep.problems.append(f"{n}번째 줄: JSON 이 아님(손상 또는 변조)")
            continue
        mac = rec.get("mac")
        if mac is None and not started:
            rep.legacy += 1
            continue
        started = True
        if not mac:
            rep.ok = False
            rep.problems.append(f"{n}번째 줄: 서명이 없는 기록(사슬이 시작된 뒤에는 허용되지 않음 — 키를 쓸 수 없었거나 손으로 추가됨)")
            continue
        body = {k: v for k, v in rec.items() if k not in ("mac", "prev")}
        if rec.get("prev", "") != prev:
            rep.ok = False
            rep.problems.append(f"{n}번째 줄: 앞 기록과 이어지지 않음(앞의 기록이 지워지거나 순서가 바뀜)")
        if rec.get("seq") != n:
            rep.ok = False
            rep.problems.append(f"{n}번째 줄: 순번이 {rec.get('seq')} 로 적혀 있음(기록이 빠지거나 끼어들거나 중복됨)")
        if not verify(_chain_payload(rec.get("prev", ""), body), mac):
            rep.ok = False
            rep.problems.append(f"{n}번째 줄: 서명이 맞지 않음(내용이 수정됐거나 키 없이 만든 기록)")
        prev, last_mac = mac, mac
        rep.chained += 1
    head = path.with_name(path.stem + ".head")
    if rep.chained:
        try:
            h = json.loads(head.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rep.warnings.append("audit.head 가 없거나 읽을 수 없음 — 끝부분이 잘렸는지 확인할 수 없습니다")
        else:
            if not verify(_head_payload(int(h.get("count", -1)), str(h.get("mac", ""))), str(h.get("sig", ""))):
                rep.ok = False
                rep.problems.append("audit.head 의 서명이 맞지 않음")
            elif h.get("mac") != last_mac or int(h.get("count", -1)) != rep.total:
                rep.ok = False
                rep.problems.append(f"기록 수/마지막 서명이 audit.head 와 다름(로그 {rep.total}건, head {h.get('count')}건) — 끝부분이 잘렸거나 추가됨")
    return rep
