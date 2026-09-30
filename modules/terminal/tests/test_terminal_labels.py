"""Conversation names, read and written through to the one row that holds them.

Names used to live in this site's own `terminal_labels` table, keyed to a
session id and knowing nothing about a session's lifecycle: a chat rotation
retires the row and mints a new id, so a name given weeks ago stayed behind on
a closed session every picker hides. They are columns on the gateway's session
row now. These routes survive the move because they have readers beyond this
page — `mini-boss` reads them with a login cookie — so what is tested here is
the pass-through: what it forwards, and whether a refusal arrives as one.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod


class _Gateway:
    """A stand-in for hive-comms: the input this route reads, not the subject.

    It records the request bodies it is handed, which is the only way to prove
    what the pass-through actually forwarded — a partial write that quietly
    sends both fields looks identical from the outside until it blanks a
    colour.
    """

    def __init__(self, names=None, status=200):
        self.names = dict(names or {})
        self.status = status
        self.puts = []
        self._server = None
        self._thread = None

    def __enter__(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):  # noqa: N802
                body = json.dumps(gateway.names).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                session_id = self.path.split("/")[2]
                gateway.puts.append((session_id, payload, dict(self.headers)))
                if gateway.status != 200:
                    self.send_response(gateway.status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                current = dict(gateway.names.get(session_id) or {})
                current.update(payload)
                gateway.names[session_id] = current
                body = json.dumps(current).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_names_come_from_the_gateway_not_from_a_local_table(tmp_path, monkeypatch):
    """R1: this site holds no names of its own.

    Breaks if a local store is reintroduced for this route to read — the read
    would then answer from it and this gateway's name would never appear.
    """
    client = _authed_client(tmp_path, monkeypatch)
    with _Gateway({"sess-1": {"name": "Fittimus Maximus", "color": "#3481cc"}}) as gw:
        monkeypatch.setenv("GATEWAY_API_URL", gw.url)

        labels = client.get("/api/terminal/labels").json()

    assert labels["sess-1"] == {"name": "Fittimus Maximus", "color": "#3481cc"}
    assert "sess-unnamed" not in labels


def test_renaming_forwards_only_the_field_that_changed(tmp_path, monkeypatch):
    """R2: a partial write, so a rename cannot blank a colour.

    Breaks if the route goes back to sending a whole record — which is how the
    health app blanked the colour picked at the tile on every rename, and how
    one tab working from a stale cache overwrote another tab's colour.
    """
    client = _authed_client(tmp_path, monkeypatch)
    with _Gateway({"sess-1": {"name": "old", "color": "#5c913b"}}) as gw:
        monkeypatch.setenv("GATEWAY_API_URL", gw.url)

        response = client.put("/api/terminal/labels/sess-1", json={"name": "new"})

    assert response.status_code == 200
    assert gw.puts[0][1] == {"name": "new"}
    assert gw.names["sess-1"]["color"] == "#5c913b"


@pytest.mark.parametrize("gateway_status", [400, 409])
def test_a_refused_rename_is_reported_as_refused(tmp_path, monkeypatch, gateway_status):
    """R7: a rename that did not land never answers 200.

    400 is a colour the gateway rejected; 409 is a conversation that has
    rotated away, whose row is still writable and read by nothing. Breaks if
    the route swallows the upstream status — the browser would keep showing a
    name the record does not hold, which is this whole change in miniature.
    """
    client = _authed_client(tmp_path, monkeypatch)
    with _Gateway(status=gateway_status) as gw:
        monkeypatch.setenv("GATEWAY_API_URL", gw.url)

        response = client.put("/api/terminal/labels/sess-1", json={"name": "Health"})

    assert response.status_code == gateway_status


def test_an_unreachable_gateway_is_reported_rather_than_answered_empty(
    tmp_path, monkeypatch
):
    """R7: down and "no names" must not read the same.

    This site used to hold the names itself, so it could always answer. Breaks
    if the read starts returning an empty object on failure, which draws every
    conversation unnamed and reads as the migration having lost the lot.
    """
    client = _authed_client(tmp_path, monkeypatch)
    monkeypatch.setenv("GATEWAY_API_URL", "http://127.0.0.1:9")

    response = client.get("/api/terminal/labels")

    assert response.status_code >= 500


def test_names_require_auth(tmp_path, monkeypatch):
    """The cookie path is what `mini-boss` reads with; it stays guarded."""
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)
    assert client.get("/api/terminal/labels").status_code == 401
    assert client.put("/api/terminal/labels/s", json={"name": "x"}).status_code == 401


def test_an_open_tab_picks_up_a_name_set_elsewhere(page, monkeypatch):
    """Names are edited on whichever device is to hand, and a tile stays open
    for days. The tab already open has to keep asking — before this it read
    them once at boot, so a name set on the phone never reached the laptop
    until a reload."""
    page, base = page

    with _Gateway() as gw:
        os.environ["GATEWAY_API_URL"] = gw.url
        try:
            page.route(
                "**/api/terminal/sessions*",
                lambda r: r.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps([{
                        "id": "sess-live", "mind_id": "mind-uuid",
                        "mind_name": "skippy", "status": "running",
                        "short_id": "sess-live",
                    }]),
                ),
            )
            page.goto(base + "/terminal")
            page.wait_for_selector(".term-card", timeout=15000)
            assert "renamed elsewhere" not in page.inner_text(".term-card")

            # Another client names it on the gateway. Same record, different
            # caller — no reload here, and nothing tells this tab it happened.
            gw.names["sess-live"] = {"name": "renamed elsewhere", "color": "#4ee88a"}

            page.wait_for_function(
                "() => document.querySelector('.term-card')"
                " && document.querySelector('.term-card').innerText"
                ".includes('renamed elsewhere')",
                timeout=45000,
            )
        finally:
            os.environ.pop("GATEWAY_API_URL", None)
