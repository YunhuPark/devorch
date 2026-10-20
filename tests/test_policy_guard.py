"""Policy/config are accepted by a human; DevOrch refuses to act on files that changed behind their back.

Reproduced 2026-10-20: `.devorch/**` is excluded from every captured diff and nothing checked the files, so an agent that could only write files
could drop `protected_paths`, add to `allowed_commands` and repoint `test_command`; none of it showed up in a reviewed diff and the next session
used it (the same kind of change was then LOW, needed no approval, and the test step ran the replaced command).
"""

import json
import sys

import pytest
import yaml
from gitutil import run_git
from test_doctor import by
from typer.testing import CliRunner

import devorch.cli.main as cli_main
from devorch.config import Policy, devorch_dir, init_project
from devorch.core.execution.executor import Executor
from devorch.doctor import run_checks
from devorch.providers.base import ProviderRegistry
from devorch.providers.mock import MockProvider
from devorch.security import policy_guard
from devorch.service import ControlPlane, ControlPlaneError, policy_accept_command

PY = sys.executable
TASK = "설명 문구 정리"


def write_yaml(path, data):
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def read_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "src").mkdir(parents=True)
    for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        run_git(r, *a)
    (r / "src" / "store.py").write_text("def skip():\n    return 1\n", encoding="utf-8")
    (r / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    init_project(r)
    pol = read_yaml(devorch_dir(r) / "policy.yaml")
    pol["protected_paths"] = [{"pattern": "src/store.py", "min_level": "high"}]
    pol["allowed_commands"] = [f"{PY} -m pytest -q"]
    write_yaml(devorch_dir(r) / "policy.yaml", pol)
    cfg = read_yaml(devorch_dir(r) / "config.yaml")
    cfg["project"]["test_command"] = f"{PY} -m pytest -q"
    write_yaml(devorch_dir(r) / "config.yaml", cfg)
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "init")
    return r


def plane(repo):
    return ControlPlane.from_project(repo, mock=True)


# ---- the reproduced attack, now stopped ------------------------------------------------------------------------------------------
async def test_an_agent_that_edits_policy_and_config_cannot_use_them_in_the_next_session(repo):
    policy_guard.accept(repo)
    assert policy_guard.check(repo).ok and plane(repo).analyze_task(TASK)["task_id"]

    pol = read_yaml(devorch_dir(repo) / "policy.yaml")  # what the agent does with plain file writes
    pol["protected_paths"] = []
    pol["allowed_commands"].append(f"{PY} marker.py")
    write_yaml(devorch_dir(repo) / "policy.yaml", pol)
    cfg = read_yaml(devorch_dir(repo) / "config.yaml")
    cfg["project"]["test_command"] = f"{PY} marker.py"
    write_yaml(devorch_dir(repo) / "config.yaml", cfg)

    nxt = plane(repo)  # "the next time the MCP server starts"
    assert nxt.policy_state.status == "changed" and set(nxt.policy_state.changed) == {"policy.yaml", "config.yaml"}
    for call in (lambda: nxt.analyze_task(TASK), lambda: nxt.preview_review("x")):
        with pytest.raises(ControlPlaneError) as e:
            call()
        assert policy_accept_command(repo) in str(e.value) and "policy.yaml" in str(e.value)
    for coro in (nxt.request_review("x"), nxt.run_tests("x"), nxt.apply_changes("x")):
        with pytest.raises(ControlPlaneError, match="policy accept"):
            await coro


async def test_a_human_accepting_the_changed_files_unblocks_it(repo):
    policy_guard.accept(repo)
    pol = read_yaml(devorch_dir(repo) / "policy.yaml")
    pol["limits"]["max_retries"] = 5
    write_yaml(devorch_dir(repo) / "policy.yaml", pol)
    assert plane(repo).policy_state.status == "changed"
    policy_guard.accept(repo)
    assert plane(repo).policy_state.ok and plane(repo).analyze_task(TASK)["task_id"]


def test_even_a_comment_counts_and_restoring_the_accepted_content_clears_it(repo):
    policy_guard.accept(repo)
    p = devorch_dir(repo) / "policy.yaml"
    original = p.read_bytes()
    p.write_bytes(original + b"\n# looks harmless\n")
    assert policy_guard.check(repo).status == "changed"
    p.write_bytes(original)
    assert policy_guard.check(repo).ok


# ---- never accepted / record tampering -----------------------------------------------------------------------------------------------
def test_a_project_nobody_accepted_is_blocked_not_silently_trusted(repo):
    st = plane(repo).policy_state
    assert st.status == "unaccepted"
    with pytest.raises(ControlPlaneError, match="policy accept"):
        plane(repo).analyze_task(TASK)


def test_deleting_the_record_does_not_fall_back_to_unchecked(repo):
    policy_guard.accept(repo)
    (policy_guard.record_dir(repo) / policy_guard.RECORD).unlink()
    assert policy_guard.check(repo).status == "unaccepted"


def test_a_forged_record_with_matching_hashes_but_no_valid_signature_is_rejected(repo):
    policy_guard.accept(repo)
    pol = read_yaml(devorch_dir(repo) / "policy.yaml")
    pol["protected_paths"] = []
    write_yaml(devorch_dir(repo) / "policy.yaml", pol)
    rec_path = policy_guard.record_dir(repo) / policy_guard.RECORD
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    rec["files"] = policy_guard.current_hashes(repo)  # make the hashes match the edited files...
    rec_path.write_text(json.dumps(rec), encoding="utf-8")  # ...but the signature can't be recomputed without the key
    assert policy_guard.check(repo).status == "tampered"


@pytest.mark.parametrize("garbage", ["not json", "{}", '{"files": 1, "accepted_at": "x", "mac": "y"}'])
def test_a_damaged_record_is_not_trusted(repo, garbage):
    policy_guard.accept(repo)
    (policy_guard.record_dir(repo) / policy_guard.RECORD).write_text(garbage, encoding="utf-8")
    assert not policy_guard.check(repo).ok


def test_a_record_signed_with_another_key_is_rejected(repo, monkeypatch, tmp_path):
    policy_guard.accept(repo)
    where = policy_guard.record_dir(repo)
    monkeypatch.setenv("DEVORCH_KEY_FILE", str(tmp_path / "other.key"))  # a different key: the record's signature no longer verifies
    monkeypatch.setattr(policy_guard, "record_dir", lambda root: where)  # same record, found where it was written
    assert policy_guard.check(repo).status == "tampered"


def test_the_record_lives_outside_the_project_next_to_the_key(repo):
    policy_guard.accept(repo)
    d = policy_guard.record_dir(repo)
    assert repo not in d.parents and d.parent.parent == policy_guard.key_path().parent
    assert not any(".devorch" in str(x) and "record" in x.name for x in repo.rglob("*") if x.is_file())


# ---- scope -------------------------------------------------------------------------------------------------------------------------
def test_direct_construction_is_unchecked_and_a_running_server_keeps_what_it_loaded(repo):
    reg = ProviderRegistry()
    reg.register(MockProvider("claude", ["[]"]))
    direct = ControlPlane(repo, Policy(), {}, reg, Executor(repo, Policy()))
    assert direct.policy_state is None and direct.analyze_task(TASK)["task_id"]  # tests/library users: not checked

    policy_guard.accept(repo)
    running = plane(repo)
    p = devorch_dir(repo) / "policy.yaml"
    p.write_bytes(p.read_bytes() + b"\n# edited after the server started\n")
    assert running.analyze_task(TASK)["task_id"]  # documented limit: noticed at the NEXT start, not mid-session
    assert plane(repo).policy_state.status == "changed"


def test_read_only_inspection_still_works_and_says_how_to_fix_it(repo):
    info = plane(repo).inspect_project()
    assert info["policy_accepted"] == "unaccepted" and policy_accept_command(repo) in info["policy_how_to_accept"]
    policy_guard.accept(repo)
    ok = plane(repo).inspect_project()
    assert ok["policy_accepted"] == "ok" and "policy_how_to_accept" not in ok


async def test_the_agent_has_no_tool_to_accept_and_is_told_not_to_edit_the_files(repo):
    from devorch.mcp_server import INSTRUCTIONS, build_server

    names = {t.name for t in await build_server(plane(repo)).list_tools()}
    assert len(names) == 8 and not any("policy" in n or "accept" in n for n in names)
    assert "policy" in INSTRUCTIONS and "never edit .devorch/policy.yaml" in INSTRUCTIONS


# ---- what the human sees ---------------------------------------------------------------------------------------------------------
def test_risky_changes_are_spelled_out_for_the_human(repo):
    policy_guard.accept(repo)
    pol = read_yaml(devorch_dir(repo) / "policy.yaml")
    pol["protected_paths"] = []
    pol["allowed_commands"].append(f"{PY} marker.py")
    pol["approval"]["require_human_approval_at"] = "critical"
    write_yaml(devorch_dir(repo) / "policy.yaml", pol)
    cfg = read_yaml(devorch_dir(repo) / "config.yaml")
    cfg["project"]["test_command"] = f"{PY} marker.py"
    write_yaml(devorch_dir(repo) / "config.yaml", cfg)
    text = "\n".join(policy_guard.risky_changes(repo))
    assert "허용 명령(allowed_commands)이 추가됨" in text and "marker.py" in text
    assert "보호 경로(protected_paths)가 제거됨" in text and "src/store.py" in text
    assert "승인 규칙(approval)이 바뀜" in text and "테스트 명령(test_command)이 바뀜" in text
    diff = policy_guard.describe_changes(repo)
    assert "policy.yaml (마지막 승인본)" in diff and "marker.py" in diff


def test_a_lowered_protected_level_is_called_out(repo):
    policy_guard.accept(repo)
    pol = read_yaml(devorch_dir(repo) / "policy.yaml")
    pol["protected_paths"] = [{"pattern": "src/store.py", "min_level": "medium"}]
    write_yaml(devorch_dir(repo) / "policy.yaml", pol)
    assert any("최소 수준이 낮아짐" in l for l in policy_guard.risky_changes(repo))


def test_cli_status_and_accept(repo, monkeypatch):
    run = CliRunner().invoke
    st = run(cli_main.app, ["policy", "status", "-p", str(repo)])
    assert st.exit_code == 1 and "승인하지 않았" in st.output

    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)
    refused = run(cli_main.app, ["policy", "accept", "-p", str(repo)])
    assert refused.exit_code == 2 and policy_guard.check(repo).status == "unaccepted"

    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    no = run(cli_main.app, ["policy", "accept", "-p", str(repo)], input="n\n")
    assert no.exit_code == 1 and policy_guard.check(repo).status == "unaccepted"
    yes = run(cli_main.app, ["policy", "accept", "-p", str(repo)], input="y\n")
    assert yes.exit_code == 0 and policy_guard.check(repo).ok
    assert run(cli_main.app, ["policy", "status", "-p", str(repo)]).exit_code == 0
    again = run(cli_main.app, ["policy", "accept", "-p", str(repo)])
    assert again.exit_code == 0 and "이미 승인된" in again.output


def test_cli_accept_shows_the_dangerous_change_before_asking(repo, monkeypatch):
    policy_guard.accept(repo)
    pol = read_yaml(devorch_dir(repo) / "policy.yaml")
    pol["protected_paths"] = []
    write_yaml(devorch_dir(repo) / "policy.yaml", pol)
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)
    out = CliRunner().invoke(cli_main.app, ["policy", "accept", "-p", str(repo)], input="n\n")
    assert "보호 경로(protected_paths)가 제거됨" in out.output and out.exit_code == 1
    assert policy_guard.check(repo).status == "changed"  # declined: still blocked


def test_doctor_reports_each_state(repo):
    c = by(run_checks(repo, env={}), "정책 승인")
    assert c.status == "warn" and "policy accept" in c.hint
    policy_guard.accept(repo)
    assert by(run_checks(repo, env={}), "정책 승인").status == "ok"
    p = devorch_dir(repo) / "policy.yaml"
    p.write_bytes(p.read_bytes() + b"\n# x\n")
    changed = by(run_checks(repo, env={}), "정책 승인")
    assert changed.status == "fail" and "policy.yaml" in changed.detail
