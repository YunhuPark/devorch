"""Shared git helper for tests: UTF-8 output and the same WinError 5 launch retry as GitManager."""

import subprocess

from devorch.git.manager import _run_git


def run_git(repo, *args, check=True) -> subprocess.CompletedProcess:
    return _run_git(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=check)
