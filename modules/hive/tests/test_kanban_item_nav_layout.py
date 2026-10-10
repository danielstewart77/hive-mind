"""The item page's back-to-backlog nav is a normal, non-overlapping element.

A bare <nav> tag matches a leftover global style.css rule that pins it
fixed across the full viewport at z-index 100, covering the page's own
content. The nav here must carry its own class so that rule never applies.
"""
from starlette.testclient import TestClient

import auth
import main as main_mod
import kanban_data as store


def test_the_back_to_backlog_nav_does_not_use_the_bare_fixed_nav_style(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    epic = store.create_epic("Desktop")
    user = auth.create_user("navlayout", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    html = client.get(f"/kanban/item/{epic['id']}").text
    assert "<nav><a" not in html
    assert '<nav class="item-back-nav">' in html
