"""A feature whose parent is a feature stays under that parent; nothing else changes."""
from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store
import accounts


async def test_children_keep_their_parent_when_their_parent_changes_type(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    epic = await store.create_epic("Desktop")
    parent = await store.create_feature(epic["id"], "Parent")
    child = await store.create_feature(epic["id"], "Child")
    await store.change_type(child["id"], "feature", parent["id"])
    await store.change_type(parent["id"], "feature", epic["id"])
    grandchild = await store.create_feature(epic["id"], "Grandchild")
    await store.change_type(grandchild["id"], "feature", child["id"])
    user = await auth.create_user("nest", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    board = client.get("/api/kanban/data").json()
    by_id = {f["id"]: f for f in board["features"]}
    assert by_id[child["id"]]["parent_id"] == parent["id"]
    assert by_id[grandchild["id"]]["parent_id"] == child["id"]
