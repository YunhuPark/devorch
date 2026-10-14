"""Invisible control characters in source files (a backspace where a regex word boundary was meant, 2026-10-14) break patterns silently."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".json", ".ps1", ".cfg", ".ini"}
ALLOWED = {9, 10, 13, 12}  # tab, LF, CR, form feed


def stray_control_bytes(data: bytes) -> list[int]:
    return [i for i, b in enumerate(data) if b < 32 and b not in ALLOWED]


def test_the_detector_finds_a_backspace_in_a_regex_literal(tmp_path):
    f = tmp_path / "x.py"
    f.write_bytes(b'import re\nPATTERN = re.compile(r"' + bytes([8]) + b'(a|b)' + bytes([8]) + b'")\n')
    assert stray_control_bytes(f.read_bytes()) != []
    g = tmp_path / "y.py"
    wb = bytes([92]) + b"b"  # a real backslash + b, built from character codes so no escape processing can turn it into a backspace
    g.write_bytes(b'import re' + bytes([10]) + b'PATTERN = re.compile(r"' + wb + b'(a|b)' + wb + b'")' + bytes([9]) + b'tab' + bytes([13, 10]))
    assert stray_control_bytes(g.read_bytes()) == []


def test_no_tracked_text_file_contains_stray_control_characters():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, encoding="utf-8")
    if out.returncode:  # not a git checkout (e.g. an sdist): nothing to scan
        return
    bad = {}
    for rel in out.stdout.splitlines():
        p = ROOT / rel
        if p.suffix.lower() in TEXT_SUFFIXES and p.is_file():
            hits = stray_control_bytes(p.read_bytes())
            if hits:
                bad[rel] = hits[:3]
    assert not bad, f"control characters (offsets) in: {bad}"
