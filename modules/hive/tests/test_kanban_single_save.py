"""One save on the item page carries the type, the parent and the fields together."""
from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("single", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_type_parent_and_fields_save_in_one_request(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = store.create_epic("Desktop")
    feature = store.create_feature(epic["id"], "Restart")
    other = store.create_epic("Other")
    response = client.patch(
        f"/api/kanban/features/{feature['id']}",
        json={"type": "story", "parent_id": other["id"], "description": "one save"},
    )
    assert response.status_code == 200
    saved = response.json()
    assert saved["description"] == "one save"
    assert store.item_relations(feature["id"])["parent"]["id"] == other["id"]


def test_clearing_the_parent_actually_removes_it(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = store.create_epic("Desktop")
    feature = store.create_feature(epic["id"], "Restart")
    response = client.patch(f"/api/kanban/features/{feature['id']}", json={"parent_id": None})
    assert response.status_code == 200
    assert store.item_relations(feature["id"])["parent"] is None
