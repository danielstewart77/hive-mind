"""Route contract: GET /api/kanban/data; POST collection; PATCH/DELETE /{kind}/{id}."""
import pytest
from starlette.testclient import TestClient

import auth
import main as main_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("kanban", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
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

    page = anon.get("/kanban", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login?next=/kanban")
