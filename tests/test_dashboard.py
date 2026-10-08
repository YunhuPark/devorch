import http.client
import json
import threading

import pytest

from devorch.dashboard.server import make_server
from tests.test_mcp import HIGH_TASK, LOW_TASK, SEC_FINDING, edit, make_plane, repo  # noqa: F401


@pytest.fixture
def served(repo):
    plane = make_plane(repo, openai=[SEC_FINDING])
    srv = make_server(repo, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield plane, srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path, method="GET", host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    return r.status, dict(r.getheaders()), body


async def test_lists_task_with_gates_and_detail(served, repo):
    plane, srv, port = served
    tid = plane.analyze_task(HIGH_TASK)["task_id"]
    edit(repo)
    await plane.request_review(tid)
    code, _, body = get(port, "/api/tasks")
    rows = json.loads(body)
    assert code == 200 and rows[0]["task_id"] == tid
    assert rows[0]["gates"]["evaluation"] == "NOT_READY" and rows[0]["gates"]["approval"] == "pending"
    code, _, body = get(port, f"/api/tasks/{tid}")
    d = json.loads(body)
    assert code == 200 and d["evaluation"]["blocking"][0]["title"] == "권한 검증 누락" and "return a + b" in d["diff"]
    assert d["plan"] and d["risk_factors"]


def test_empty_state_and_bad_ids(served):
    _, _, port = served
    assert json.loads(get(port, "/api/tasks")[2]) == []
    assert json.loads(get(port, "/api/audit")[2]) == []
    assert json.loads(get(port, "/api/bench")[2]) == []
    assert get(port, "/api/tasks/not-an-id")[0] == 404
    assert get(port, "/api/tasks/..%2f..%2fetc")[0] == 404
    assert get(port, "/api/tasks/deadbeef")[0] == 404
    assert get(port, "/api/audit?limit=abc")[0] == 400
    assert get(port, "/nope")[0] == 404


def test_page_headers_and_no_remote_origins(served):
    _, _, port = served
    code, h, body = get(port, "/")
    assert code == 200 and b"DevOrch" in body
    csp = h["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "http" not in csp
    assert h["X-Content-Type-Options"] == "nosniff" and "Access-Control-Allow-Origin" not in h
    assert b"innerHTML" not in body  # untrusted LLM text is only ever inserted as text


def test_read_only_and_host_check(served):
    _, _, port = served
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        assert get(port, "/api/tasks", method=m)[0] == 405
    assert get(port, "/api/tasks", host="evil.example.com")[0] == 403  # DNS rebinding
    assert get(port, "/api/tasks", host=f"evil.example.com:{port}")[0] == 403
    assert get(port, "/api/tasks", host=f"localhost:{port}")[0] == 200


def test_no_approval_or_write_endpoint_exists(served):
    _, srv, port = served
    for p in ("/api/approve", "/api/tasks/deadbeef/approve", "/approve"):
        assert get(port, p)[0] == 404


async def test_audit_filter_limit_and_corrupt_line(served, repo):
    plane, _, port = served
    a = plane.analyze_task(LOW_TASK)["task_id"]
    b = plane.analyze_task(LOW_TASK)["task_id"]
    with (repo / ".devorch" / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    plane.audit.log(a, "x_event", {"k": 1})
    allev = json.loads(get(port, "/api/audit")[2])
    assert allev[0]["event"] == "x_event"  # newest first, corrupt line skipped
    only_b = json.loads(get(port, f"/api/audit?task={b}")[2])
    assert only_b and all(e["run_id"] == b for e in only_b)
    assert len(json.loads(get(port, "/api/audit?limit=1")[2])) == 1


def test_audit_secrets_are_redacted_at_write_time(served, repo):
    plane, _, port = served
    plane.audit.log("deadbeef", "leak", {"key": "sk-ant-api03-" + "A" * 40})
    body = get(port, "/api/audit")[2].decode()
    assert "AAAAAAAAAAAA" not in body


def test_bench_results_listed_with_mock_flag(served, repo):
    _, _, port = served
    d = repo / ".devorch" / "bench" / "20260101-000000"
    d.mkdir(parents=True)
    (d / "report.md").write_text("# r")
    (d / "results.json").write_text(json.dumps({"mock": True, "modes": ["single"], "summaries": {}}))
    (repo / ".devorch" / "bench" / "broken").mkdir()
    runs = json.loads(get(port, "/api/bench")[2])
    assert len(runs) == 1 and runs[0]["mock"] is True and runs[0]["report"] == "# r"


def test_unreadable_task_file_does_not_break_list(served, repo):
    _, _, port = served
    (repo / ".devorch" / "tasks").mkdir(parents=True, exist_ok=True)
    (repo / ".devorch" / "tasks" / "abcdef01.json").write_text("{broken")
    rows = json.loads(get(port, "/api/tasks")[2])
    assert rows[0]["task_id"] == "abcdef01" and "error" in rows[0]
