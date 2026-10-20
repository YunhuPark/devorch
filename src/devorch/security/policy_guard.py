"""A human accepts the project's policy and config; DevOrch refuses to act on files that changed behind their back.

Why: `.devorch/**` is left out of every captured diff (it is DevOrch's own folder), and the policy is read when the server starts. An agent that can
write files could therefore delete `protected_paths`, widen `allowed_commands` or point `test_command` at another script, and none of it would
appear in any reviewed diff; the next session would simply use the changed files (reproduced 2026-10-20, tests/test_policy_guard.py).

What this does: when a human runs `devorch policy accept` (and at `devorch init`) the SHA-256 of `policy.yaml` and `config.yaml` is recorded and
signed with the state key, in the key's own folder (`~/.devorch/policies/<project id>/`), OUTSIDE the project — so an agent that only edits project
files cannot update it, and cannot delete it to fall back to "legacy, unchecked". The state-changing tools refuse to run when the files differ from
that record, or when no record exists yet.

What it does NOT do: same limit as the signing key (security/signing.py). Code running as the same OS user (the test command with sandbox mode
"none") can read the key and forge a record; and a human who accepts a changed policy without reading the diff has accepted it. It also does not
notice an edit made AFTER the server started until the next start (the running server keeps what it loaded).
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from devorch.security.signing import SigningError, key_path, sign, verify

FILES = ("policy.yaml", "config.yaml")
RECORD = "record.json"


@dataclass
class PolicyState:
    status: str  # ok | unaccepted | changed | tampered | key_error
    problem: str | None = None
    changed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _project_id(root: Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).lower().encode("utf-8")).hexdigest()[:16]


def record_dir(root: Path) -> Path:
    return key_path().parent / "policies" / _project_id(root)


def _read(root: Path, name: str) -> bytes | None:
    try:
        return (Path(root) / ".devorch" / name).read_bytes()
    except FileNotFoundError:
        return None


def current_hashes(root: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in FILES:
        data = _read(root, name)
        out[name] = hashlib.sha256(data).hexdigest() if data is not None else None
    return out


def _payload(root: Path, hashes: dict[str, str | None], accepted_at: str) -> str:
    return json.dumps({"root": str(Path(root).resolve()), "files": hashes, "accepted_at": accepted_at}, sort_keys=True, ensure_ascii=False)


def accept(root: Path) -> dict[str, str | None]:
    """Record (and sign) the CURRENT policy and config as the one a human accepted. Callers must have shown the human what they are accepting."""
    root = Path(root)
    hashes = current_hashes(root)
    accepted_at = datetime.now(timezone.utc).isoformat()
    d = record_dir(root)
    try:
        d.mkdir(parents=True, exist_ok=True)
        for name in FILES:  # copies only so a later `policy status` can show a diff; the signed hashes are what binds
            data = _read(root, name)
            target = d / name
            if data is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(data)
        (d / RECORD).write_text(
            json.dumps({"files": hashes, "accepted_at": accepted_at, "mac": sign(_payload(root, hashes, accepted_at))}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except OSError as e:
        raise SigningError(f"정책 승인 기록을 저장하지 못했습니다({d}): {e}") from e
    return hashes


def check(root: Path) -> PolicyState:
    root = Path(root)
    path = record_dir(root) / RECORD
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        files, accepted_at, mac = rec["files"], rec["accepted_at"], rec["mac"]
    except FileNotFoundError:
        return PolicyState("unaccepted", "이 프로젝트의 정책·설정(.devorch/policy.yaml, config.yaml)을 사람이 아직 승인하지 않았습니다.")
    except (OSError, ValueError, KeyError, TypeError):
        return PolicyState("tampered", "정책 승인 기록을 읽을 수 없거나 형식이 올바르지 않습니다. 신뢰하지 않습니다.")
    try:
        valid = verify(_payload(root, files, accepted_at), mac)
    except SigningError:
        valid = False
    if not valid:
        return PolicyState("tampered", "정책 승인 기록의 서명이 맞지 않거나 서명 키를 읽을 수 없습니다. 신뢰하지 않습니다.")
    now = current_hashes(root)
    changed = [n for n in FILES if now.get(n) != files.get(n)]
    if changed:
        return PolicyState(
            "changed",
            f"정책·설정 파일이 사람의 마지막 승인 뒤에 바뀌었습니다({', '.join(changed)}). 에이전트가 고쳤을 수 있으니 사람이 내용을 확인해야 합니다.",
            changed,
        )
    return PolicyState("ok")


def risky_changes(root: Path) -> list[str]:
    """Plain-language list of the changes a human should not miss (compared with the accepted copy; empty if there is none to compare with)."""
    import yaml  # local: only the human-facing commands need it

    root, d = Path(root), record_dir(root)

    def load(data: bytes | None) -> dict:
        try:
            got = yaml.safe_load((data or b"").decode("utf-8", "replace")) or {}
        except yaml.YAMLError:
            return {"__unreadable__": True}
        return got if isinstance(got, dict) else {"__unreadable__": True}

    out: list[str] = []
    for name in FILES:
        now = load(_read(root, name))
        try:
            before = load((d / name).read_bytes())
        except FileNotFoundError:
            continue
        if now.get("__unreadable__") or before.get("__unreadable__"):
            out.append(f"{name}: 읽을 수 없는 YAML 입니다")
            continue
        if name == "policy.yaml":
            added = [c for c in (now.get("allowed_commands") or []) if c not in (before.get("allowed_commands") or [])]
            if added:
                out.append("허용 명령(allowed_commands)이 추가됨: " + ", ".join(map(str, added)))
            old_p = {p.get("pattern"): p.get("min_level") for p in (before.get("protected_paths") or []) if isinstance(p, dict)}
            new_p = {p.get("pattern"): p.get("min_level") for p in (now.get("protected_paths") or []) if isinstance(p, dict)}
            gone = [k for k in old_p if k not in new_p]
            if gone:
                out.append("보호 경로(protected_paths)가 제거됨: " + ", ".join(map(str, gone)))
            order = {"medium": 1, "high": 2, "critical": 3}
            lowered = [k for k in old_p if k in new_p and order.get(str(new_p[k]), 0) < order.get(str(old_p[k]), 0)]
            if lowered:
                out.append("보호 경로의 최소 수준이 낮아짐: " + ", ".join(map(str, lowered)))
            if (now.get("approval") or {}) != (before.get("approval") or {}):
                out.append("승인 규칙(approval)이 바뀜")
            if (now.get("rules") or {}) != (before.get("rules") or {}):
                out.append("위험도별 역할 규칙(rules)이 바뀜")
        else:
            bp, np_ = before.get("project") or {}, now.get("project") or {}
            for key, label in (("test_command", "테스트 명령(test_command)"), ("test_env", "테스트 환경변수(test_env)"), ("sandbox", "샌드박스 설정(sandbox)")):
                if bp.get(key) != np_.get(key):
                    out.append(f"{label}이 바뀜: {bp.get(key)!r} → {np_.get(key)!r}")
    return out


def describe_changes(root: Path) -> str:
    """Unified diff of what changed since the accepted copy; for a project never accepted, the full current content."""
    root = Path(root)
    d = record_dir(root)
    parts: list[str] = []
    for name in FILES:
        now = _read(root, name)
        try:
            before = (d / name).read_bytes()
        except FileNotFoundError:
            before = None
        if now == before:
            continue
        a = (before or b"").decode("utf-8", "replace").splitlines()
        b = (now or b"").decode("utf-8", "replace").splitlines()
        label = "(승인된 적 없음)" if not (d / RECORD).exists() else "(마지막 승인본)"
        parts.append("\n".join(difflib.unified_diff(a, b, fromfile=f"{name} {label}", tofile=f"{name} (현재)", lineterm="")))
    return "\n\n".join(p for p in parts if p) or "(변경 없음)"
