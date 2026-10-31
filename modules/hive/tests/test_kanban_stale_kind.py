"""A row is updated by the kind it is now, not the kind its URL was built with."""
from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store
import accounts


async def test_a_feature_changed_to_an_epic_still_saves_through_its_old_feature_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    epic = await store.create_epic("Desktop")
    feature = await store.create_feature(epic["id"], "Restart")
    user = await auth.create_user("stale", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    assert client.patch(f"/api/kanban/features/{feature['id']}", json={"type": "epic"}).status_code == 200
    saved = client.patch(f"/api/kanban/features/{feature['id']}", json={"description": "still here"})
    assert saved.status_code == 200
    assert saved.json()["description"] == "still here"
