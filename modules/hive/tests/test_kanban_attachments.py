"""Story 3.7.2: a file attached to an item is kept and comes back with it.

Daniel's check is his Azure DevOps screenshot, a JPEG, attached to an item,
the item closed and reopened, and the same image on that item and no other.
The bytes below open the way a JPEG does - which is what decides it is shown
as an image - and carry every byte value, NUL included, because a file is not
text and a path that treated it as text would hand back something else.
"""
import accounts
import pytest
from starlette.testclient import TestClient

import auth
import kanban_data
import main as main_mod

A_SCREENSHOT = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(256)) * 4


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("kanban", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


def _board(client):
    epic = client.post("/api/kanban/epics", json={"title": "Epic"}).json()
    feature = client.post(
        "/api/kanban/features", json={"epic_id": epic["id"], "title": "Feature"}
    ).json()
    story = client.post(
        "/api/kanban/stories", json={"title": "Story", "feature_id": feature["id"]}
    ).json()
    return epic, feature, story


def _attach(client, item_id, name="kanban-pattern-azure-devops-backlog.jpg",
            data=A_SCREENSHOT):
    return client.post(
        f"/api/kanban/items/{item_id}/attachments",
        files={"file": (name, data, "application/octet-stream")},
    )


def test_an_attached_screenshot_comes_back_with_its_item_and_no_other(client):
    epic, feature, story = _board(client)

    attached = _attach(client, story["id"])
    assert attached.status_code == 200, attached.text

    # Reopened: the item carries it, and its own listing names it.
    reopened = client.get(f"/api/kanban/items/{story['id']}/attachments").json()
    assert [(a["filename"], a["size_bytes"], a["is_image"]) for a in reopened] == [
        ("kanban-pattern-azure-devops-backlog.jpg", len(A_SCREENSHOT), True)]
    served = client.get(
        f"/api/kanban/items/{story['id']}/attachments/{reopened[0]['id']}")
    assert served.status_code == 200
    assert served.content == A_SCREENSHOT
    # Decided by the file's own bytes, not by the type the browser sent.
    assert served.headers["content-type"] == "image/jpeg"

    # On no other item: not in its listing, and not through its id.
    assert client.get(f"/api/kanban/items/{epic['id']}/attachments").json() == []
    assert client.get(
        f"/api/kanban/items/{feature['id']}/attachments/{reopened[0]['id']}"
    ).status_code == 404

    # A second item's own image comes back as its own, with its own type.
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    assert _attach(client, epic["id"], "board.png", png).status_code == 200
    on_epic = client.get(f"/api/kanban/items/{epic['id']}/attachments").json()
    assert [a["filename"] for a in on_epic] == ["board.png"]
    served = client.get(f"/api/kanban/items/{epic['id']}/attachments/{on_epic[0]['id']}")
    assert (served.content, served.headers["content-type"]) == (png, "image/png")
    assert [a["filename"] for a in client.get(
        f"/api/kanban/items/{story['id']}/attachments").json()] == [
        "kanban-pattern-azure-devops-backlog.jpg"]


async def test_the_item_record_carries_every_file_attached_to_it_in_order():
    epic = await kanban_data.create_epic("Epic with two files")
    first = await kanban_data.add_attachment(epic["id"], "one.png",
                                             b"\x89PNG\r\n\x1a\nfirst")
    second = await kanban_data.add_attachment(epic["id"], "notes.txt",
                                              b"second file")
    elsewhere = await kanban_data.create_epic("Another epic")
    await kanban_data.add_attachment(elsewhere["id"], "not-yours.png",
                                     b"\x89PNG\r\n\x1a\nother")

    opened = await kanban_data.find_item(epic["id"])
    assert [(a["id"], a["filename"], a["is_image"])
            for a in opened["attachments"]] == [
        (first["id"], "one.png", True), (second["id"], "notes.txt", False)]


async def test_an_attachment_survives_every_edit_move_reorder_retype_and_status_change():
    epic = await kanban_data.create_epic("Epic")
    other_epic = await kanban_data.create_epic("Other epic")
    feature = await kanban_data.create_feature(epic["id"], "Feature")
    other = await kanban_data.create_feature(epic["id"], "Sibling")
    story = await kanban_data.create_story("Story", feature_id=feature["id"])
    sibling = await kanban_data.create_story("Sibling", feature_id=feature["id"])
    kept = await kanban_data.add_attachment(story["id"], "shot.jpg", A_SCREENSHOT)

    async def still_there(step):
        listed = await kanban_data.list_attachments(story["id"])
        assert [a["id"] for a in listed] == [kept["id"]], step
        got = await kanban_data.get_attachment(story["id"], kept["id"])
        assert got["content"] == A_SCREENSHOT, step

    await kanban_data.update_story(story["id"], title="Renamed", description="Edited")
    await still_there("edit")
    await kanban_data.update_story(story["id"], status="done")
    await still_there("status change")
    await kanban_data.place_before(story["id"], sibling["id"])
    await still_there("reorder")
    await kanban_data.update_story(story["id"], feature_id=other["id"])
    await still_there("move to another feature")
    await kanban_data.set_parent(story["id"], other_epic["id"])
    await still_there("new parent")
    await kanban_data.change_type(story["id"], "bug")
    await still_there("retype to bug")
    await kanban_data.change_type(story["id"], "epic")
    await still_there("retype to epic")


async def test_a_standalone_task_carries_a_file_and_it_comes_back_with_it():
    """Tasks too, now that a task can stand alone on the board."""
    task = await kanban_data.create_task("A task on its own")
    kept = await kanban_data.add_attachment(task["id"], "shot.jpg", A_SCREENSHOT)

    opened = await kanban_data.find_item(task["id"])
    assert [a["id"] for a in opened["attachments"]] == [kept["id"]]
    got = await kanban_data.get_attachment(task["id"], kept["id"])
    assert got["content"] == A_SCREENSHOT
