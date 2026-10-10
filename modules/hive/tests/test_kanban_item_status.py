"""The item detail page shows and sets a story's status; other kinds show none."""
import re

from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("status-editor", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_a_storys_item_page_carries_its_current_status(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = store.create_epic("Epic")
    story = store.create_story("A story", epic_id=epic["id"], status="in_progress")
    html = client.get(f"/kanban/item/{story['id']}").text
    assert 'data-item-status="in_progress"' in html


def test_setting_a_storys_status_through_the_item_page_api_call_saves_it(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = store.create_epic("Epic")
    story = store.create_story("A story", epic_id=epic["id"], status="backlog")
    response = client.patch(f"/api/kanban/stories/{story['id']}", json={"status": "done"})
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert store.find_item(story["id"])["status"] == "done"


def test_an_epics_item_page_carries_no_status(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = store.create_epic("Epic")
    html = client.get(f"/kanban/item/{epic['id']}").text
    assert 'data-item-status=""' in html
