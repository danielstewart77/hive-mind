"""Dropping an item onto any item makes it that item's child; its own children stay where they are."""
from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store
import accounts


async def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = await auth.create_user("drop", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    return client


async def test_a_feature_dropped_onto_a_feature_keeps_its_own_children(tmp_path, monkeypatch):
    client = await _client(monkeypatch, tmp_path)
    epic = await store.create_epic("Desktop")
    moved = await store.create_feature(epic["id"], "Moved")
    target = await store.create_feature(epic["id"], "Target")
    child = await store.create_story("Child", feature_id=moved["id"])
    response = client.patch(f"/api/kanban/features/{moved['id']}", json={"parent_id": target["id"]})
    assert response.status_code == 200
    assert (await store.item_relations(moved["id"]))["parent"]["id"] == target["id"]
    assert (await store.item_relations(child["id"]))["parent"]["id"] == moved["id"]


async def test_a_story_dropped_onto_a_story_is_accepted(tmp_path, monkeypatch):
    client = await _client(monkeypatch, tmp_path)
    epic = await store.create_epic("Desktop")
    first = await store.create_story("First", epic_id=epic["id"])
    second = await store.create_story("Second", epic_id=epic["id"])
    response = client.patch(f"/api/kanban/stories/{second['id']}", json={"parent_id": first["id"]})
    assert response.status_code == 200
    assert (await store.item_relations(second["id"]))["parent"]["id"] == first["id"]
