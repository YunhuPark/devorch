"""HMAC signatures for DevOrch's own state (task records, human approvals).

Why: the agent DevOrch is meant to constrain can usually write files in the project. Approvals and task records used to be plain JSON in
`.devorch/tasks/`, so writing one file turned `approval_required` into `committed` (reproduced 2026-10-14). The key lives OUTSIDE the project
(default `~/.devorch/state.key`, override with the DEVORCH_KEY_FILE environment variable), so forging a record takes more than editing a file
in the project folder, and a write to it shows up as a visible file edit in the agent host.

What this does NOT do (be honest about it): anything running as the same OS user can read the key. The test command runs the agent's own code on
this machine (sandbox mode "none"), so a deliberately malicious agent that can get code executed can read the key and forge state. Containing that
needs the Docker sandbox or a separate OS user. This protects against a careless, confused or prompt-injected agent that edits files, which is the
realistic case, not against a determined attacker.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

KEY_BYTES = 32


class SigningError(RuntimeError):
    """The signing key can't be read or created: callers must fail closed (nothing is trusted, nothing is approved)."""


def key_path() -> Path:
    env = os.environ.get("DEVORCH_KEY_FILE")
    return Path(env) if env else Path.home() / ".devorch" / "state.key"


def load_key(create: bool = True) -> bytes:
    path = key_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise SigningError(f"서명 키가 없습니다: {path}") from None
        data = b""
    except OSError as e:
        raise SigningError(f"서명 키를 읽지 못했습니다({path}): {e}") from e
    if len(data) >= KEY_BYTES:
        return data
    if not create:
        raise SigningError(f"서명 키가 손상됐습니다: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = os.urandom(KEY_BYTES)
        try:
            with open(path, "xb") as fh:  # exclusive: never overwrite a key another process just made
                fh.write(fresh)
            try:
                os.chmod(path, 0o600)  # best effort; Windows ignores most of it
            except OSError:
                pass
            return fresh
        except FileExistsError:
            return load_key(create=False)
    except OSError as e:
        raise SigningError(f"서명 키를 만들지 못했습니다({path}): {e}") from e


def sign(payload: str) -> str:
    return hmac.new(load_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(payload: str, signature: str) -> bool:
    """False for a missing or wrong signature, and also when the key is unavailable (fail closed)."""
    if not isinstance(signature, str) or not signature:
        return False
    try:
        expected = sign(payload)
    except SigningError:
        return False
    return hmac.compare_digest(expected, signature)
