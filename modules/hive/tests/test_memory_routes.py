"""Route tests for the memory (lucent DB browser) page."""

import os
import sqlite3
import sys

from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod


def _seed_lucent(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, body TEXT, tier TEXT)")
    conn.execute("INSERT INTO memories (body, tier) VALUES (?, ?)", ("hello world", "contextual"))
    conn.execute("INSERT INTO memories (body, tier) VALUES (?, ?)", ("a long memory body", "standing"))
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO entities (name) VALUES (?)", ("Daniel",))
    conn.commit()
    conn.close()


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    lucent_path = tmp_path / "lucent.db"
    _seed_lucent(lucent_path)
    monkeypatch.setenv("LUCENT_DB_PATH", str(lucent_path))
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_memory_page_redirects_when_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    response = client.get("/memory", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/memory")


def test_memory_page_renders_for_authed_user(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/memory")

    assert response.status_code == 200
    assert "memory-page" in response.text or "memory" in response.text.lower()


def test_memory_tables_endpoint_lists_tables(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/memory/tables")

    assert response.status_code == 200
    payload = response.json()
    names = {t["name"] for t in payload["tables"]}
    assert "memories" in names
    assert "entities" in names
    memories = next(t for t in payload["tables"] if t["name"] == "memories")
    assert memories["row_count"] == 2


def test_memory_rows_endpoint_returns_columns_and_rows(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/memory/rows?table=memories&limit=10&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert payload["columns"] == ["id", "body", "tier"]
    assert len(payload["rows"]) == 2
    assert payload["total"] == 2
    assert payload["rows"][0]["body"] == "hello world"


def test_memory_rows_rejects_unknown_table(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/memory/rows?table=nonexistent")

    assert response.status_code == 404


def test_memory_rows_rejects_sql_injection_in_table_name(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/memory/rows?table=memories;DROP+TABLE+entities")

    assert response.status_code == 404


def test_memory_rows_renders_blob_columns_as_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    lucent_path = tmp_path / "lucent.db"
    conn = sqlite3.connect(lucent_path)
    conn.execute("CREATE TABLE vec_rows (id INTEGER PRIMARY KEY, body TEXT, embedding BLOB)")
    conn.execute(
        "INSERT INTO vec_rows (body, embedding) VALUES (?, ?)",
        ("hello", bytes([0xBA, 0xAD, 0xF0, 0x0D, 0x01, 0x02, 0x03])),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("LUCENT_DB_PATH", str(lucent_path))
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))

    response = client.get("/api/memory/rows?table=vec_rows")

    assert response.status_code == 200
    payload = response.json()
    assert payload["rows"][0]["body"] == "hello"
    assert payload["rows"][0]["embedding"] == "<BLOB 7 bytes>"


def test_memory_endpoints_require_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    tables_resp = client.get("/api/memory/tables")
    rows_resp = client.get("/api/memory/rows?table=memories")

    assert tables_resp.status_code in (401, 403)
    assert rows_resp.status_code in (401, 403)
