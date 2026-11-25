"""Route tests for the memory browser page, over the hive's `memory` schema."""

import os
import sys

from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod
import accounts


async def _seed_memories():
    from hive_data import memory

    await memory.store(mind_id="a-mind", content="hello world",
                       tier="contextual", created_at=1)
    await memory.store(mind_id="a-mind", content="a long memory body",
                       tier="standing", created_at=2)


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    accounts.run(_seed_memories())
    user = accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


def test_memory_page_redirects_when_unauthenticated(tmp_path, monkeypatch):
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
    assert {"memories", "nodes", "edges"} <= names
    memories = next(t for t in payload["tables"] if t["name"] == "memories")
    assert memories["row_count"] == 2


def test_memory_rows_endpoint_returns_columns_and_rows(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/memory/rows?table=memories&limit=10&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert {"id", "content", "tier"} <= set(payload["columns"])
    assert len(payload["rows"]) == 2
    assert payload["total"] == 2
    assert sorted(row["content"] for row in payload["rows"]) == [
        "a long memory body", "hello world"]


def test_memory_rows_rejects_unknown_table(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/memory/rows?table=nonexistent")

    assert response.status_code == 404


def test_memory_rows_rejects_sql_injection_in_table_name(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/memory/rows?table=memories;DROP+TABLE+nodes")

    assert response.status_code == 404


def test_memory_endpoints_require_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    tables_resp = client.get("/api/memory/tables")
    rows_resp = client.get("/api/memory/rows?table=memories")

    assert tables_resp.status_code in (401, 403)
    assert rows_resp.status_code in (401, 403)
