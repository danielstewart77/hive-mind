"""Status on every type, and a task that stands alone (stories 3.7.1, 3.7.4).

Driven through the board's own routes, the ones the backlog page and a mind
holding the board bearer call.
"""

import pytest
from starlette.testclient import TestClient

import auth
import main as main_mod
from hive_site.testing import run


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = run(auth.create_user("board-editor", "secret-pass", is_admin=True, replace=True))
    test_client = TestClient(main_mod.app)
    test_client.cookies.set(auth.SESSION_COOKIE_NAME, run(auth.create_session_token(user)))
    return test_client


def _created(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_a_task_naming_no_parent_is_created_and_is_its_own_entry_on_the_board(client):
    epic = _created(client.post("/api/kanban/epics", json={"title": "An epic"}))

    task = _created(client.post("/api/kanban/tasks", json={"title": "Stand alone"}))

    board = client.get("/api/kanban/data").json()
    assert [t["id"] for t in board["tasks"]] == [task["id"]]
    assert "parent_id" not in board["tasks"][0]
    # Its own root on the board, numbered after the epic rather than under it.
    numbers = client.get("/api/kanban/numbers").json()
    assert numbers[epic["id"]] == "1"
    assert numbers[task["id"]] == "2"


def test_a_story_moved_under_a_task_numbers_beneath_it(client):
    task = _created(client.post("/api/kanban/tasks", json={"title": "Parent task"}))
    epic = _created(client.post("/api/kanban/epics", json={"title": "Epic"}))
    story = _created(client.post("/api/kanban/stories",
                                 json={"title": "Story", "epic_id": epic["id"]}))

    _created(client.patch(f"/api/kanban/stories/{story['id']}",
                          json={"parent_id": task["id"]}))

    numbers = client.get("/api/kanban/numbers").json()
    assert numbers[story["id"]] == f"{numbers[task['id']]}.1"


def test_a_done_item_changed_to_another_type_is_still_done(client):
    epic = _created(client.post("/api/kanban/epics", json={"title": "Epic"}))
    story = _created(client.post("/api/kanban/stories",
                                 json={"title": "Finished", "epic_id": epic["id"],
                                       "status": "done"}))

    _created(client.patch(f"/api/kanban/stories/{story['id']}", json={"type": "feature"}))

    board = client.get("/api/kanban/data").json()
    assert [(f["id"], f["status"]) for f in board["features"]] == [(story["id"], "done")]
