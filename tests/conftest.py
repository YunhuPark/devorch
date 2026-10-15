"""Shared test setup: every test (and every server/CLI subprocess it starts) signs DevOrch state with a throw-away key, never the user's."""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("DEVORCH_KEY_FILE", str(Path(tempfile.mkdtemp(prefix="devorch_testkey_")) / "state.key"))
