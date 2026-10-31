"""A story with no parent is saved and read back without an invented parent key."""
import auth
import main as main_mod
import kanban_data as store
from starlette.testclient import TestClient
import accounts


async def test_an_unparented_story_saves_its_description_and_has_no_parent_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    epic = await store.create_epic("Desktop")
    story = await store.create_story("Story", epic_id=epic["id"])
    await store.update_story(story["id"], parent="none")
    user = await auth.create_user("loose", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    response = client.patch(f"/api/kanban/stories/{story['id']}", json={"description": "added on item page"})
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "added on item page"
    assert not any(key.startswith("None") for key in body)
    assert (await store.find_item(story["id"]))["description"] == "added on item page"
