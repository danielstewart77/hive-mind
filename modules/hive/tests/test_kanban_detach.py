"""A child can be detached from its parent from the item page."""
from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store
import accounts


async def test_a_feature_can_be_detached_from_its_epic_through_the_api(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    epic = await store.create_epic("Desktop")
    feature = await store.create_feature(epic["id"], "Restart")
    user = await auth.create_user("detach", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    response = client.patch(f"/api/kanban/features/{feature['id']}", json={"parent": "none"})
    assert response.status_code == 200
    assert (await store.item_relations(feature["id"]))["parent"] is None
