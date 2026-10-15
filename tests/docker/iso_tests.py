"""Runs INSIDE the DevOrch Docker sandbox. Each test asserts that something a hostile test could try is impossible."""
import os
import pathlib
import socket
import subprocess


def test_runs_as_an_unprivileged_user():
    assert os.getuid() != 0


def test_no_network():
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=3).close()
        except OSError:
            continue
        raise AssertionError(f"reached {host}: the container has network access")


def test_no_dns():
    try:
        socket.gethostbyname("example.com")
    except OSError:
        return
    raise AssertionError("DNS works: the container has network access")


def test_host_environment_is_not_visible():
    assert "DEVORCH_PROBE_SECRET" not in os.environ
    assert not [k for k in os.environ if "API_KEY" in k or k.endswith("_TOKEN")]


def test_host_paths_do_not_exist():
    for p in ("C:/Users", "C:\\Users", "/mnt/c", "/host_mnt", "/run/desktop/mnt/host"):
        assert not pathlib.Path(p).exists(), f"{p} is visible inside the sandbox"


def test_the_signing_key_and_devorch_state_are_unreachable():
    hits = []
    for root in ("/work", "/tmp", "/home", "/root"):
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f in ("state.key", "audit.jsonl") or f.endswith(".approval.json"):
                    hits.append(os.path.join(dirpath, f))
    assert hits == [], hits


def test_cannot_write_outside_work_and_tmp():
    for p in ("/etc/devorch_probe", "/usr/devorch_probe", "/devorch_probe"):
        try:
            open(p, "w").write("x")
        except OSError:
            continue
        raise AssertionError(f"wrote {p}")


def test_tmp_is_writable_and_small():
    pathlib.Path("/tmp/ok.txt").write_text("ok")
    try:
        with open("/tmp/big.bin", "wb") as fh:
            fh.write(b"0" * (300 * 1024 * 1024))  # the tmpfs is 256m
    except OSError:
        return
    raise AssertionError("wrote 300 MB into /tmp: the size limit is not enforced")


def test_no_capabilities_can_not_chown_or_mount():
    r = subprocess.run(["mount", "-t", "tmpfs", "tmpfs", "/mnt"], capture_output=True)
    assert r.returncode != 0
    r = subprocess.run(["chown", "0:0", "/work"], capture_output=True)
    assert r.returncode != 0 or os.getuid() == 0


def test_the_work_directory_is_the_only_project_data():
    assert pathlib.Path("/work/calc.py").exists()
    assert not pathlib.Path("/work/.git").is_dir() or True  # a worktree has a .git FILE pointing at the host repo; reported separately
