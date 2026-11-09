"""The item detail page lets the title be edited and saved, and puts Save at the top."""
import re

from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store
import accounts
from hive_site.testing import run


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = run(auth.create_user("editor", "secret-pass", is_admin=True, replace=True))
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, run(auth.create_session_token(user)))
    return client


def test_the_item_page_renders_an_editable_title_field(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = run(store.create_epic("Original name"))
    html = client.get(f"/kanban/item/{epic['id']}").text
    assert re.search(r'name="title"[^>]*value="Original name"', html)


def test_renaming_through_the_item_page_api_call_saves_the_new_title(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = run(store.create_epic("Original name"))
    response = client.patch(f"/api/kanban/epics/{epic['id']}", json={"title": "Renamed"})
    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"
    assert (run(store.find_item(epic["id"])))["title"] == "Renamed"


def test_the_save_button_is_the_first_control_rendered_in_the_form(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path)
    epic = run(store.create_epic("Original name"))
    html = client.get(f"/kanban/item/{epic['id']}").text
    form_start = html.index('id="item-form"')
    save_index = html.index('type="submit"', form_start)
    title_field_index = html.index('name="title"', form_start)
    assert save_index < title_field_index
