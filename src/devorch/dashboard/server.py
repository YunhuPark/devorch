"""Local, read-only HTTP server (stdlib only).

Hardening: binds to loopback by default, GET/HEAD only, Host header must be loopback
(blocks DNS-rebinding), no CORS headers, CSP without remote origins, no state-changing
endpoint exists — approval stays in the terminal (`devorch approve`).
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from devorch.dashboard.data import Data
from devorch.dashboard.page import PAGE
from devorch.service import ControlPlaneError
from devorch.store import StoreError

CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'"
_TASK = re.compile(r"^/api/tasks/([^/]+)$")


def _host_ok(host_header: str | None, port: int) -> bool:
    if not host_header:
        return False
    return host_header in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    data = Data(root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "DevOrchDashboard"

        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not _host_ok(self.headers.get("Host"), self.server.server_address[1]):
                return self._json(403, {"error": "허용되지 않은 Host"})
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if url.path == "/api/tasks":
                    return self._json(200, data.tasks())
                m = _TASK.match(url.path)
                if m:
                    return self._json(200, data.task(m.group(1)))
                if url.path == "/api/audit":
                    q = parse_qs(url.query)
                    try:
                        limit = int(q.get("limit", ["200"])[0])
                    except ValueError:
                        return self._json(400, {"error": "limit 는 정수여야 합니다"})
                    return self._json(200, data.audit(q.get("task", [None])[0], limit))
                if url.path == "/api/bench":
                    return self._json(200, data.bench_runs())
            except (StoreError, ControlPlaneError) as e:
                return self._json(404, {"error": str(e)})
            except Exception as e:  # never leak a traceback
                return self._json(500, {"error": f"내부 오류: {type(e).__name__}"})
            return self._json(404, {"error": "not found"})

        def _reject(self):
            self._json(405, {"error": "읽기 전용 대시보드입니다 (GET만 지원)"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _reject

    return ThreadingHTTPServer((host, port), Handler)
