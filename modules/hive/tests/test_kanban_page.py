"""The kanban page itself: who may open it, and that it is the page served."""
import pytest
from starlette.testclient import TestClient

import auth
import main as main_mod


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("kanban-page", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def test_kanban_view_routes_require_authentication(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)
    for path in ("/kanban", "/kanban/board", "/kanban/backlog"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/login?next={path}"


def test_backlog_uses_collapsible_items_and_drag_drop_hooks(signed_in):
    body = signed_in.get("/kanban/backlog")
    assert body.status_code == 200
    assert "kanban-toggle" in body.text
    assert "/kanban/item/" in body.text
    assert "application/json" in body.text
    assert "parent_id" in body.text
    assert "enableDrop" in body.text
    assert 'id="collapse-level"' in body.text
    assert 'id="expand-level"' in body.text
    assert "const target=[3,2,1][level]" in body.text
    assert "const target=[1,2,3][level]" in body.text
    assert '<nav class="kanban-toolbar"' not in body.text
    assert body.text.count('href="/kanban/backlog"') == 1
    assert body.text.count('href="/kanban/board"') == 1
    assert 'class="console-nav-flyout"' in body.text
    assert 'class="console-nav-submenu"' in body.text
    assert 'href="/kanban/backlog" class="console-nav-sublink is-active"' in body.text
    assert 'href="/kanban/board" class="console-nav-sublink "' in body.text
    assert "board-toggle" not in body.text
    assert "backlog-toggle" not in body.text
    assert "Move work forward on the board" not in body.text


def test_board_and_backlog_routes_render_only_their_selected_view(signed_in):
    board = signed_in.get("/kanban/board")
    backlog = signed_in.get("/kanban/backlog")
    assert board.status_code == backlog.status_code == 200
    assert 'id="board-view" aria-label="Kanban board"' in board.text
    assert 'id="backlog-view" aria-label="Kanban backlog" hidden' in board.text
    assert 'id="backlog-view" aria-label="Kanban backlog"' in backlog.text
    assert 'id="board-view" aria-label="Kanban board" hidden' in backlog.text


def test_kanban_navigation_links_point_to_the_distinct_view_routes(signed_in):
    body = signed_in.get("/kanban/backlog").text
    assert 'href="/kanban/backlog"' in body
    assert 'href="/kanban/board"' in body


def test_a_legacy_kanban_url_redirects_to_the_board_route(signed_in):
    response = signed_in.get("/kanban?filter=active", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/kanban/board?filter=active"


def test_a_signed_in_visitor_is_served_the_board_and_not_another_page(signed_in):
    """A 200 alone does not say which template the route rendered.

    The board draws itself in the browser into two mount points that no
    other console page has, so naming them is how this distinguishes the
    kanban page from any other page the route could have been pointed at.
    """
    body = signed_in.get("/kanban/board")
    assert body.status_code == 200
    assert "board-mount" in body.text and 'id="backlog-view" aria-label="Kanban backlog" hidden' in body.text


def test_kanban_entry_point_sends_a_signed_in_browser_to_the_board_keeping_its_query(signed_in):
    response = signed_in.get("/kanban?epic=e1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/kanban/board?epic=e1"


def test_sidebar_marks_kanban_current_on_every_page_of_the_section(signed_in):
    epic = signed_in.post("/api/kanban/epics", json={"title": "Nav epic"}).json()
    for path in ("/kanban/board", "/kanban/backlog", f"/kanban/item/{epic['id']}"):
        body = signed_in.get(path)
        assert body.status_code == 200, path
        assert '<a href="/kanban" class="console-nav-link is-active">' in body.text, path


def test_done_cards_on_the_board_carry_a_trash_control(signed_in):
    body = signed_in.get("/kanban/board").text
    assert "kanban-trash" in body
    assert "trashButton(s)" in body
    assert "status==='done'" in body


def test_board_card_markup_includes_a_selectable_item_id(signed_in):
    body = signed_in.get("/kanban/board").text
    assert "kanban-card-id" in body
    assert "el('span','kanban-card-id',s.id)" in body


def test_board_cards_link_to_the_item_details_page(signed_in):
    body = signed_in.get("/kanban/board").text
    assert "const heading=el('strong','');heading.append(safeTitle(s))" in body
