"""The item page knows which kind it is, so its Save goes to a real API URL."""
import re

from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store
import accounts


async def test_the_item_page_renders_the_kind_its_save_url_is_built_from(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    epic = await store.create_epic("Desktop")
    user = await auth.create_user("kind", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    html = client.get(f"/kanban/item/{epic['id']}").text
    assert re.search(r'data-item-kind="epic"', html)


async def test_item_detail_displays_the_full_backlog_number(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    epic = await store.create_epic("Desktop")
    feature = await store.create_feature(epic["id"], "Feature")
    story = await store.create_story("Story", feature_id=feature["id"])
    user = await auth.create_user("number-detail", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    html = client.get(f"/kanban/item/{story['id']}").text
    assert 'id="item-number" aria-label="Backlog item number"' in html
    numbers = client.get("/api/kanban/numbers").json()
    assert numbers[story["id"]] == "1.1.1"




async def test_a_save_to_the_kind_the_page_renders_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    epic = await store.create_epic("Desktop")
    user = await auth.create_user("kind2", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, await auth.create_session_token(user))
    assert client.patch(f"/api/kanban/epics/{epic['id']}", json={"description": "saved"}).status_code == 200
