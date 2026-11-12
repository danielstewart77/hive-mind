"""The setup page: one HTML page and a small JSON API.

Listens on every address on a random free port, so it can be opened from the
operator's own machine. Every route — the page included, and any path that
matches none — refuses a request without this run's code: a fresh one made
when the process starts, never written to disk, compared in constant time.
The code rides in the link as `?code=` and the page sends it back on every
call as `X-Setup-Code`.

The page holds no state of its own; it reads `/api/state`, so reopening it
after an SSH drop shows wherever the run has got to. It serves no websocket.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE = Path(__file__).with_name("page.html")
CODE_HEADER = "X-Setup-Code"


def new_code() -> str:
    return secrets.token_urlsafe(18)


class SetupServer:
    """The page for one run. `ROUTES` is every (method, path) it serves."""

    ROUTES = {
        ("GET", "/"): "page",
        ("GET", "/api/plan"): "get_plan",
        ("POST", "/api/plan"): "post_plan",
        ("GET", "/api/state"): "get_state",
        ("POST", "/api/start"): "post_start",
        ("POST", "/api/answer"): "post_answer",
        ("POST", "/api/restart"): "post_restart",
        ("POST", "/api/close"): "post_close",
    }

    def __init__(self, installer, host: str = "0.0.0.0", port: int = 0):
        self.installer = installer
        self.code = new_code()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # the installer log has what matters
                pass

            def do_GET(self):
                server._handle(self, "GET")

            def do_POST(self):
                server._handle(self, "POST")

            def do_PUT(self):
                server._handle(self, "PUT")

            def do_DELETE(self):
                server._handle(self, "DELETE")

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def link(self, address: str) -> str:
        return f"http://{address}:{self.port}/?code={self.code}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="setup-page",
                                        daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- dispatch ---------------------------------------------------------
    def _authorised(self, req: BaseHTTPRequestHandler) -> bool:
        presented = req.headers.get(CODE_HEADER) or ""
        if not presented:
            query = parse_qs(urlparse(req.path).query)
            presented = (query.get("code") or [""])[0]
        return bool(presented) and hmac.compare_digest(presented.encode(), self.code.encode())

    def _send(self, req, status: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req.send_response(status)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(data)))
        req.send_header("Cache-Control", "no-store")
        req.send_header("Referrer-Policy", "no-referrer")
        req.end_headers()
        req.wfile.write(data)

    def _handle(self, req: BaseHTTPRequestHandler, method: str) -> None:
        if not self._authorised(req):
            self._send(req, 403, {"error": "this page needs the code printed when the "
                                           "installer started"})
            return
        path = urlparse(req.path).path
        name = self.ROUTES.get((method, path))
        if name is None:
            self._send(req, 404, {"error": "no such route"})
            return
        body: dict = {}
        if method == "POST":
            length = int(req.headers.get("Content-Length") or 0)
            try:
                body = json.loads(req.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                self._send(req, 400, {"error": "the body is not JSON"})
                return
            if not isinstance(body, dict):
                self._send(req, 400, {"error": "the body is not an object"})
                return
        status, out, ctype = getattr(self, name)(body)
        self._send(req, status, out, ctype)

    # ---- routes -----------------------------------------------------------
    def page(self, _):
        return 200, PAGE.read_bytes(), "text/html; charset=utf-8"

    def get_plan(self, _):
        return 200, self.installer.plan_view(), "application/json"

    def post_plan(self, body):
        """The plan and questions for a selection not yet started."""
        view = self.installer.plan_view(body)
        return (400 if "error" in view else 200), view, "application/json"

    def get_state(self, _):
        return 200, self.installer.state_view(), "application/json"

    def post_start(self, body):
        problems = self.installer.start(body)
        return (400, {"problems": problems}, "application/json") if problems else \
            (200, {"started": True}, "application/json")

    def post_answer(self, body):
        problems = self.installer.answer(str(body.get("key") or ""), str(body.get("value") or ""))
        return (400, {"problems": problems}, "application/json") if problems else \
            (200, {"ok": True}, "application/json")

    def post_restart(self, _):
        problems = self.installer.restart()
        return (409, {"problems": problems}, "application/json") if problems else \
            (200, {"restarted": True}, "application/json")

    def post_close(self, _):
        if not self.installer.close():
            return 409, {"error": "the install is still running"}, "application/json"
        threading.Thread(target=self.stop, daemon=True).start()
        return 200, {"closing": True}, "application/json"
