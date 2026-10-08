"""Route contract: GET /api/kanban/data; POST collection; PATCH/DELETE /{kind}/{id}."""
import os
import sys

from starlette.testclient import TestClient
from fastapi.responses import HTMLResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import kanban_data
import main as main_mod


def client_for(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("kanban", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def setup_function():
    kanban_data.reset()


def teardown_function():
    kanban_data.reset()


def test_data_and_all_mutation_families(tmp_path, monkeypatch):
    c = client_for(tmp_path, monkeypatch)
    assert len(c.get("/api/kanban/data").json()["stories"]) == 14
    e = c.post("/api/kanban/epics", json={"title": "New epic"}).json()
    f = c.post("/api/kanban/features", json={"epic_id": e["id"], "title": "New feature"}).json()
    s = c.post("/api/kanban/stories", json={"title": "New story", "feature_id": f["id"]}).json()
    assert s["status"] == "backlog"
    assert c.patch(f"/api/kanban/epics/{e['id']}", json={"title": "Renamed"}).status_code == 200
    assert c.patch(f"/api/kanban/features/{f['id']}", json={"title": "Renamed feature"}).status_code == 200
    assert c.patch(f"/api/kanban/stories/{s['id']}", json={"status": "done"}).json()["status"] == "done"
    assert c.delete(f"/api/kanban/stories/{s['id']}").status_code == 200
    assert c.delete(f"/api/kanban/features/{f['id']}").status_code == 200
    assert c.delete(f"/api/kanban/epics/{e['id']}").status_code == 200


def test_validation_not_found_and_auth(tmp_path, monkeypatch):
    c = client_for(tmp_path, monkeypatch)
    assert c.post("/api/kanban/stories", json={"title": "x", "feature_id": "f1", "epic_id": "e1"}).status_code == 400
    assert c.post("/api/kanban/stories", json={"title": "x", "epic_id": "e1", "status": "bad"}).status_code == 400
    assert c.post("/api/kanban/epics", content="{").status_code == 400
    assert c.post("/api/kanban/epics", json=[]).status_code == 400
    assert c.post("/api/kanban/epics", json={"extra": 1}).status_code == 400
    # Store KeyError is the only missing-record signal and consistently maps to 404.
    assert c.patch("/api/kanban/stories/nope", json={"title": "x"}).status_code == 404
    assert c.delete("/api/kanban/epics/nope").status_code == 404
    assert c.post("/api/kanban/features", json={"title": "x", "epic_id": "nope"}).status_code == 404
    assert c.patch("/api/kanban/features/f1", json={"epic_id": "nope"}).status_code == 404
    assert c.patch("/api/kanban/stories/s1", json={"status": "unknown"}).status_code == 400
    assert c.post("/api/kanban/epics", json={}).status_code == 400
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
    ):
        assert getattr(anon, method)(path, **kwargs).status_code == 401
    page = anon.get("/kanban", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login?next=/kanban")
    # The page route delegates rendering to the shared console helper. Story 3
    # owns kanban.html; intercept the helper only for page auth/cache assertions.
    original_console_page = main_mod._console_page
    main_mod._console_page = lambda request, template_name: HTMLResponse(
        template_name, headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
    )
    try:
        page = c.get("/kanban")
        assert page.status_code == 200
        assert page.text == "kanban.html"
        assert page.headers["cache-control"] == "no-store, must-revalidate"
        assert page.headers["pragma"] == "no-cache"
    finally:
        main_mod._console_page = original_console_page


def test_reset_isolation(tmp_path, monkeypatch):
    c = client_for(tmp_path, monkeypatch)
    c.post("/api/kanban/epics", json={"title": "Temporary"})
    kanban_data.reset()
    assert len(c.get("/api/kanban/data").json()["epics"]) == 4
