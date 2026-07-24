"""Server-side terminal session labels (name + color).

Labels were localStorage-only, so a session renamed on the desktop showed
up unnamed on the phone. They now live in the auth database and sync via
/api/terminal/labels, with the browser cache as warm-start only.
"""

import os
import sys

from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_labels_roundtrip(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    assert client.get("/api/terminal/labels").json() == {}

    put = client.put(
        "/api/terminal/labels/sess-1", json={"name": "deploy bot", "color": "#4ee8fc"}
    )
    assert put.status_code == 200

    labels = client.get("/api/terminal/labels").json()
    assert labels == {"sess-1": {"name": "deploy bot", "color": "#4ee8fc"}}


def test_label_update_overwrites(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    client.put("/api/terminal/labels/sess-1", json={"name": "old", "color": "#c9a84c"})
    client.put("/api/terminal/labels/sess-1", json={"name": "new", "color": ""})

    labels = client.get("/api/terminal/labels").json()
    assert labels["sess-1"] == {"name": "new", "color": ""}


def test_empty_label_deletes_row(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    client.put("/api/terminal/labels/sess-1", json={"name": "x", "color": ""})
    client.put("/api/terminal/labels/sess-1", json={"name": "", "color": ""})

    assert client.get("/api/terminal/labels").json() == {}


def test_rejects_non_hex_color(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    resp = client.put(
        "/api/terminal/labels/sess-1",
        json={"name": "x", "color": "javascript:alert(1)"},
    )
    assert resp.status_code == 400


def test_labels_require_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "stb.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)
    assert client.get("/api/terminal/labels").status_code == 401
    assert client.put("/api/terminal/labels/s", json={"name": "x"}).status_code == 401


def test_terminal_page_wires_label_sync():
    template = os.path.join(
        os.path.dirname(__file__), "..", "src", "templates", "terminal.html"
    )
    with open(template, encoding="utf-8") as fh:
        body = fh.read()
    assert "/api/terminal/labels" in body
    assert "syncLabelsFromServer" in body
