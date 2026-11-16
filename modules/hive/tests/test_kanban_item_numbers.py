"""Every item has a permanent whole-number ID (story 3.7.7).

A new item of any type gets the next whole number from one count shared by
every type, and an item's ID never changes when it is moved. Every test here
starts on an emptied board whose count is reset, so the first item is 1.
"""
import pytest
from starlette.testclient import TestClient

import accounts
import auth
import kanban_data
import main as main_mod
from hive_site.testing import run

_KINDS = ("epics", "features", "stories", "tasks")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("ids", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


def test_new_items_of_every_type_take_consecutive_numbers_from_one_count_starting_at_1(client):
    epic = client.post("/api/kanban/epics", json={"title": "Epic"}).json()
    feature = client.post(
        "/api/kanban/features", json={"epic_id": epic["id"], "title": "Feature"}).json()
    story = client.post(
        "/api/kanban/stories", json={"title": "Story", "feature_id": feature["id"]}).json()
    bug = run(kanban_data.create_story("Bug", epic_id=epic["id"], subtype="bug"))
    task = client.post("/api/kanban/tasks", json={"title": "Task on its own"}).json()
    second_epic = client.post("/api/kanban/epics", json={"title": "Epic two"}).json()

    assert [epic["item_number"], feature["item_number"], story["item_number"],
            bug["item_number"], task["item_number"],
            second_epic["item_number"]] == [1, 2, 3, 4, 5, 6]

    # Both board payloads the pages draw from carry the same numbers.
    expected = {epic["id"]: 1, feature["id"]: 2, story["id"]: 3,
                bug["id"]: 4, task["id"]: 5, second_epic["id"]: 6}
    for route in ("/api/kanban/data", "/api/kanban/board"):
        data = client.get(route).json()
        on_board = {r["id"]: r["item_number"]
                    for kind in _KINDS for r in data[kind]}
        assert on_board == expected, route
    assert run(
        kanban_data.find_item(story["id"]))["item_number"] == 3


def test_a_deleted_items_number_is_not_given_to_the_next_item():
    kept = run(kanban_data.create_epic("Kept"))
    gone = run(kanban_data.create_epic("Gone"))
    run(kanban_data.delete_epic(gone["id"]))
    after = run(kanban_data.create_epic("After"))
    assert (kept["item_number"], gone["item_number"], after["item_number"]) == (1, 2, 3)


def test_moving_reordering_reparenting_and_retyping_never_change_an_items_number():
    epic = run(kanban_data.create_epic("Epic"))
    other = run(kanban_data.create_epic("Other"))
    feature = run(kanban_data.create_feature(epic["id"], "Feature"))
    story = run(kanban_data.create_story("Story", feature_id=feature["id"]))
    sibling = run(kanban_data.create_story("Sibling", feature_id=feature["id"]))
    task = run(kanban_data.create_task("Task", parent_id=story["id"]))
    numbers = {r["id"]: r["item_number"]
               for r in (epic, other, feature, story, sibling, task)}

    def unchanged(moved):
        # What the move answered with, and the board read afterwards.
        assert moved["item_number"] == numbers[moved["id"]]
        data = run(kanban_data.get_data())
        now = {r["id"]: r["item_number"] for kind in _KINDS for r in data[kind]}
        assert now == numbers

    unchanged(run(kanban_data.update_epic(other["id"], before_id=epic["id"])))
    unchanged(run(kanban_data.place_before(sibling["id"], story["id"])))
    unchanged(run(kanban_data.update_feature(feature["id"], epic_id=other["id"])))
    unchanged(run(kanban_data.set_parent(story["id"], epic["id"])))
    unchanged(run(kanban_data.set_parent(task["id"], None)))
    unchanged(run(kanban_data.change_type(story["id"], "feature", parent_id=epic["id"])))
    unchanged(run(kanban_data.change_type(story["id"], "bug", parent_id=other["id"])))
    unchanged(run(kanban_data.change_type(task["id"], "story", parent_id=feature["id"])))
