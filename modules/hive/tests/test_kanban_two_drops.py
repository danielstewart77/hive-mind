"""A reorder drop lands before a sibling; a nest drop makes the dropped item a child. Children are left alone."""
from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("two", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_a_reorder_drop_places_the_item_before_its_sibling(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = store.create_epic("Desktop")
    first = store.create_feature(epic["id"], "First")
    second = store.create_feature(epic["id"], "Second")
    response = client.patch(f"/api/kanban/features/{second['id']}", json={"before_id": first["id"]})
    assert response.status_code == 200
    features = [f["id"] for f in store.list_features() if f.get("epic_id") == epic["id"]]
    assert features.index(second["id"]) < features.index(first["id"])


def test_a_nest_drop_makes_the_item_a_child_and_its_own_children_stay(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = store.create_epic("Desktop")
    moved = store.create_feature(epic["id"], "Moved")
    target = store.create_feature(epic["id"], "Target")
    child = store.create_story("Child", feature_id=moved["id"])
    response = client.patch(f"/api/kanban/features/{moved['id']}", json={"parent_id": target["id"]})
    assert response.status_code == 200
    assert store.item_relations(moved["id"])["parent"]["id"] == target["id"]
    assert store.item_relations(child["id"])["parent"]["id"] == moved["id"]
