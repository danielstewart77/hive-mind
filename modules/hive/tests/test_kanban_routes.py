"""Route contract: GET /api/kanban/data; POST collection; PATCH/DELETE /{kind}/{id}."""
import pytest
from starlette.testclient import TestClient

import auth
import main as main_mod
import accounts


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


def test_item_detail_page_loads_and_saves_metadata(client):
    epic, _, _ = _board(client)
    page = client.get(f"/kanban/item/{epic['id']}")
    assert page.status_code == 200
    assert "Description" in page.text
    saved = client.patch(
        f"/api/kanban/epics/{epic['id']}",
        json={"description": "Purpose", "requirements": "Need", "acceptance_criteria": "Works", "qa_tests": "Check it"},
    )
    assert saved.status_code == 200
    assert client.get("/api/kanban/data").json()["epics"][0]["qa_tests"] == "Check it"
    assert client.get("/kanban/item/no-such-item").status_code == 404


def test_data_api_migrates_an_existing_board_database(tmp_path, monkeypatch):
    """A board file predating the prose columns is served, not refused.

    The store brings the file forward as it opens, so the legacy rows come
    back through the route with the new columns empty — rather than the page
    failing on a column the read names and the file does not have.
    """
    import asyncio
    import sqlite3

    import store as data_store

    legacy = tmp_path / "legacy.db"
    database = sqlite3.connect(legacy)
    database.execute(
        "CREATE TABLE work_items (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
        "parent_id TEXT REFERENCES work_items(id), title TEXT NOT NULL, "
        "status TEXT, position INTEGER NOT NULL)")
    database.execute(
        "INSERT INTO work_items VALUES ('e-existing', 'epic', NULL, "
        "'Existing', NULL, 1)")
    database.commit()
    database.close()

    asyncio.run(data_store.close())
    asyncio.run(data_store.open(legacy))

    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("kanban-legacy", "secret-pass", is_admin=True,
                              replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))

    response = client.get("/api/kanban/data")
    assert response.status_code == 200
    assert response.json()["epics"][0]["title"] == "Existing"
    assert response.json()["epics"][0]["description"] == ""


def test_a_new_board_is_served_with_every_lane_present_and_empty(client):
    assert client.get("/api/kanban/data").json() == {
        "epics": [],
        "features": [],
        "stories": [],
    }


def test_each_kind_can_be_created_and_a_story_starts_in_the_backlog(client):
    epic, feature, story = _board(client)
    assert story["status"] == "backlog"
    data = client.get("/api/kanban/data").json()
    assert [e["id"] for e in data["epics"]] == [epic["id"]]
    assert [f["id"] for f in data["features"]] == [feature["id"]]
    assert [s["id"] for s in data["stories"]] == [story["id"]]


def test_a_story_is_served_under_the_key_naming_the_parent_it_has(client):
    epic, feature, _ = _board(client)
    direct = client.post(
        "/api/kanban/stories", json={"title": "Direct", "epic_id": epic["id"]}
    ).json()

    served = {s["id"]: s for s in client.get("/api/kanban/data").json()["stories"]}
    assert served[direct["id"]]["epic_id"] == epic["id"]
    assert "feature_id" not in served[direct["id"]]
    assert "parent_id" not in served[direct["id"]]
    assert client.get("/api/kanban/data").json()["features"][0]["epic_id"] == epic["id"]


def test_a_rename_is_stored_and_not_merely_reported(client):
    epic, _, _ = _board(client)
    assert client.patch(
        f"/api/kanban/epics/{epic['id']}", json={"title": "Renamed"}
    ).status_code == 200
    stored = client.get("/api/kanban/data").json()["epics"][0]
    assert stored["title"] == "Renamed"


def test_a_story_can_be_reordered_to_a_drop_position_through_the_api(client):
    epic, feature, original_story = _board(client)
    client.delete(f"/api/kanban/stories/{original_story['id']}")
    second = client.post("/api/kanban/stories", json={"title": "Second", "feature_id": feature["id"]}).json()
    first = client.post("/api/kanban/stories", json={"title": "First", "feature_id": feature["id"]}).json()

    response = client.patch(f"/api/kanban/stories/{second['id']}", json={"feature_id": feature["id"], "before_id": first["id"]})
    assert response.status_code == 200
    data = client.get("/api/kanban/data").json()
    assert [item["id"] for item in data["stories"] if item.get("feature_id") == feature["id"]] == [second["id"], first["id"]]


def test_a_status_change_is_stored_and_not_merely_reported(client):
    _, _, story = _board(client)
    assert client.patch(
        f"/api/kanban/stories/{story['id']}", json={"status": "done"}
    ).json()["status"] == "done"
    stored = client.get("/api/kanban/data").json()["stories"][0]
    assert stored["status"] == "done"


def test_deleting_an_epic_clears_the_whole_board_and_reports_what_it_took(client):
    epic, _, _ = _board(client)
    client.post("/api/kanban/stories", json={"title": "Direct", "epic_id": epic["id"]})

    removed = client.delete(f"/api/kanban/epics/{epic['id']}")
    assert removed.status_code == 200
    assert removed.json()["removed"] == {"features": 1, "stories": 2}
    assert client.get("/api/kanban/data").json() == {
        "epics": [],
        "features": [],
        "stories": [],
    }


def test_the_descendant_count_is_read_from_the_board_not_from_the_caller(client):
    epic, feature, _ = _board(client)
    client.post("/api/kanban/stories", json={"title": "Direct", "epic_id": epic["id"]})

    assert client.get(f"/api/kanban/epics/{epic['id']}/descendants").json() == {
        "features": 1,
        "stories": 2,
    }
    assert client.get(f"/api/kanban/features/{feature['id']}/descendants").json() == {
        "features": 0,
        "stories": 1,
    }


def test_a_malformed_or_unsupported_request_is_refused(client):
    epic, feature, _ = _board(client)
    assert client.post(
        "/api/kanban/stories",
        json={"title": "x", "feature_id": feature["id"], "epic_id": epic["id"]},
    ).status_code == 400
    assert client.post(
        "/api/kanban/stories",
        json={"title": "x", "epic_id": epic["id"], "status": "bad"},
    ).status_code == 400
    assert client.post("/api/kanban/epics", content="{").status_code == 400
    assert client.post("/api/kanban/epics", json=[]).status_code == 400
    assert client.post("/api/kanban/epics", json={"extra": 1}).status_code == 400
    assert client.post("/api/kanban/epics", json={}).status_code == 400
    assert client.patch(f"/api/kanban/stories/{_board(client)[2]['id']}", json={}).status_code == 400


def test_a_row_that_is_not_there_is_a_404_and_not_a_500(client):
    epic, feature, _ = _board(client)
    assert client.patch("/api/kanban/stories/nope", json={"title": "x"}).status_code == 404
    assert client.delete("/api/kanban/epics/nope").status_code == 404
    assert client.post(
        "/api/kanban/features", json={"title": "x", "epic_id": "nope"}
    ).status_code == 404
    assert client.patch(
        f"/api/kanban/features/{feature['id']}", json={"epic_id": "nope"}
    ).status_code == 404
    assert client.get("/api/kanban/epics/nope/descendants").status_code == 404


def test_a_parent_of_the_wrong_kind_is_refused_over_the_wire(client):
    epic, feature, story = _board(client)
    assert client.post(
        "/api/kanban/features", json={"title": "x", "epic_id": story["id"]}
    ).status_code == 404
    assert client.post(
        "/api/kanban/stories", json={"title": "x", "feature_id": epic["id"]}
    ).status_code == 404


def test_every_kanban_route_refuses_a_caller_who_is_not_signed_in():
    anon = TestClient(main_mod.app)
    assert anon.get("/api/kanban/data").status_code == 401
    for method, path, kwargs in (
        ("post", "/api/kanban/epics", {"json": {"title": "x"}}),
        ("post", "/api/kanban/features", {"json": {"title": "x", "epic_id": "e1"}}),
        ("post", "/api/kanban/stories", {"json": {"title": "x", "epic_id": "e1"}}),
        ("patch", "/api/kanban/epics/e1", {"json": {"title": "x"}}),
        ("patch", "/api/kanban/features/f1", {"json": {"title": "x"}}),
        ("patch", "/api/kanban/stories/s1", {"json": {"status": "done"}}),
        ("delete", "/api/kanban/epics/e1", {}),
        ("delete", "/api/kanban/features/f1", {}),
        ("delete", "/api/kanban/stories/s1", {}),
        ("get", "/api/kanban/epics/e1/descendants", {}),
    ):
        assert getattr(anon, method)(path, **kwargs).status_code == 401, path

    for path in ("/kanban", "/kanban/board", "/kanban/backlog"):
        page = anon.get(path, follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login?next=")


def test_a_status_the_board_has_no_lane_for_is_refused_on_update(client):
    _, _, story = _board(client)
    assert client.patch(
        f"/api/kanban/stories/{story['id']}", json={"status": "archived"}
    ).status_code == 400
    assert client.get("/api/kanban/data").json()["stories"][0]["status"] == "backlog"


def test_a_field_the_route_does_not_know_is_refused_rather_than_crashing(client):
    assert client.post(
        "/api/kanban/epics", json={"title": "x", "extra": 1}
    ).status_code == 400
    epic, _, _ = _board(client)
    assert client.patch(
        f"/api/kanban/epics/{epic['id']}", json={"title": "x", "extra": 1}
    ).status_code == 400


def test_a_collection_the_board_does_not_have_is_a_404_rather_than_a_500(client):
    assert client.patch("/api/kanban/widgets/x", json={"title": "y"}).status_code == 404
    assert client.delete("/api/kanban/widgets/x").status_code == 404
    assert client.get("/api/kanban/widgets/x/descendants").status_code == 404


def test_the_descendant_count_follows_the_row_not_the_kind_in_the_url(client):
    epic, feature, story = _board(client)
    assert client.get(f"/api/kanban/stories/{epic['id']}/descendants").status_code == 200
    assert client.get(f"/api/kanban/epics/{feature['id']}/descendants").status_code == 200
    assert client.get(f"/api/kanban/stories/{story['id']}/descendants").json() == {
        "features": 0,
        "stories": 0,
    }


def test_item_relations_endpoint_lists_parent_children_and_choices(client):
    epic, feature, story = _board(client)
    relations = client.get(f"/api/kanban/relations/{story['id']}").json()
    assert relations["parent"]["id"] == feature["id"]
    assert relations["children"] == []
    assert {c["id"] for c in relations["choices"]} >= {epic["id"], feature["id"]}


def test_changing_an_item_type_through_the_api_follows_the_parent_rules(client):
    epic, feature, story = _board(client)
    changed = client.patch(f"/api/kanban/stories/{story['id']}", json={"type": "bug"})
    assert changed.status_code == 200
    assert changed.json()["subtype"] == "bug"
    made_bug = client.patch(f"/api/kanban/features/{feature['id']}", json={"type": "bug"})
    assert made_bug.status_code == 200
    assert made_bug.json()["subtype"] == "bug"
