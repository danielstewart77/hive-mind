"""Route tests for the design-session API.

The access test enumerates the design routes off the live router rather than
naming them, so a route added later is covered without anyone remembering to
add it here. That is the failure this is really guarding: not a route that
refuses wrongly today, but one that ships next month with no dependency on it
at all.
"""

import os
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import design_data
import main as main_mod
import accounts

PUSH_TOKEN = "design-push-token"  # secret-guard: allow


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DESIGN_PUSH_TOKEN", PUSH_TOKEN)
    # The house tokens are removed, not left to the invoking shell. Every
    # mind in the hive holds both, so a fallback to either is the whole
    # threat this credential exists to close — and on Daniel's own machine
    # they are exported, which would let that fallback pass unnoticed.
    monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)


@pytest.fixture()
def anon(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    return TestClient(main_mod.app)


@pytest.fixture()
def daniel(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    user = accounts.make_user("daniel", "secret-pass", is_admin=True,
                              replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME,
                       accounts.session_token(user))
    return client


@pytest.fixture()
def mind(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    client = TestClient(main_mod.app)
    client.headers.update({"Authorization": f"Bearer {PUSH_TOKEN}"})
    return client


def _design_routes():
    return [
        route
        for route in main_mod.app.routes
        if getattr(route, "path", "").startswith(main_mod.DESIGN_PREFIX)
    ]


def _exercise(client, route):
    method = next(iter(sorted(route.methods - {"HEAD", "OPTIONS"})))
    path = (
        route.path.replace("{design_id}", "any-id")
        .replace("{page}", "requirements")
    )
    query = "?mind=skippy&path=x.py&name=thing" if "symbol" in path else ""
    return client.request(method, path + query, json={})


def test_every_design_route_refuses_an_unauthenticated_caller(anon):
    routes = _design_routes()
    assert routes, "no design routes found on the router"

    for route in routes:
        response = _exercise(anon, route)
        assert response.status_code == 401, f"{route.path} answered {response.status_code}"


def test_every_design_route_refuses_a_wrong_bearer(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    client = TestClient(main_mod.app)
    client.headers.update({"Authorization": f"Bearer {PUSH_TOKEN}x"})

    for route in _design_routes():
        response = _exercise(client, route)
        assert response.status_code == 401, f"{route.path} answered {response.status_code}"


def test_a_bearer_holding_a_non_ascii_credential_is_refused_not_crashed(
    tmp_path, monkeypatch
):
    _env(tmp_path, monkeypatch)
    client = TestClient(main_mod.app)
    # Sent as latin-1 bytes, which is what a header actually carries on the
    # wire: Starlette decodes it back to a str holding a non-ASCII character,
    # and `compare_digest` raises TypeError on exactly that. The 500 it would
    # produce reads to a mind as a console that is down, not a refusal.
    client.headers.update({"Authorization": "Bearer tökén".encode("latin-1")})

    response = client.get(f"{main_mod.DESIGN_PREFIX}/sessions")

    assert response.status_code == 401


def test_the_mind_bearer_is_accepted(mind):
    response = mind.get(f"{main_mod.DESIGN_PREFIX}/sessions")

    assert response.status_code == 200
    assert response.json() == {"sessions": []}


def test_daniels_browser_login_is_accepted(daniel):
    response = daniel.get(f"{main_mod.DESIGN_PREFIX}/sessions")

    assert response.status_code == 200


def test_the_push_routes_refuse_when_no_token_is_configured(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.delenv("DESIGN_PUSH_TOKEN", raising=False)
    client = TestClient(main_mod.app)
    client.headers.update({"Authorization": "Bearer anything"})

    response = client.get(f"{main_mod.DESIGN_PREFIX}/sessions")

    assert response.status_code == 503


def test_a_session_created_by_the_mind_is_listed_for_daniel(mind, tmp_path, monkeypatch):
    created = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions",
        json={"title": "Design sessions", "conversation_id": "conv-1"},
    )
    assert created.status_code == 200

    listed = mind.get(f"{main_mod.DESIGN_PREFIX}/sessions").json()["sessions"]

    assert [row["title"] for row in listed] == ["Design sessions"]
    assert listed[0]["conversation_id"] == "conv-1"


def test_publishing_a_page_and_reading_it_back_over_http(mind):
    created = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions", json={"title": "Design"}
    ).json()

    published = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions/{created['id']}/pages/requirements/publish",
        json={
            "revision": created["revision"],
            "items": {"r1": {"order": 1, "text": "Sessions are listed"}},
        },
    )
    assert published.status_code == 200

    fetched = mind.get(
        f"{main_mod.DESIGN_PREFIX}/sessions/{created['id']}"
    ).json()
    assert [item["text"] for item in fetched["pages"]["requirements"]] == [
        "Sessions are listed"
    ]


def test_a_stale_revision_is_refused_with_a_conflict(mind):
    created = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions", json={"title": "Design"}
    ).json()
    mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions/{created['id']}/pages/requirements/publish",
        json={
            "revision": created["revision"],
            "items": {"r1": {"order": 1, "text": "Sessions are listed"}},
        },
    )

    second = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions/{created['id']}/pages/requirements/publish",
        json={
            "revision": created["revision"],
            "items": {"r1": {"order": 1, "text": "Something else"}},
        },
    )

    assert second.status_code == 409


def test_a_refused_revision_names_the_item_it_would_not_change(mind):
    created = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions", json={"title": "Design"}
    ).json()
    published = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions/{created['id']}/pages/requirements/publish",
        json={
            "revision": created["revision"],
            "items": {
                "r1": {"order": 1, "text": "One"},
                "r2": {"order": 2, "text": "Two"},
            },
        },
    ).json()

    refused = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions/{created['id']}/pages/requirements/publish",
        json={
            "revision": published["revision"],
            "items": {
                "r1": {"order": 1, "text": "One changed"},
                "r2": {"order": 2, "text": "Two changed"},
            },
        },
    )

    assert refused.status_code == 409
    assert "r1" in refused.json()["detail"]
    assert "r2" in refused.json()["detail"]


def test_the_detail_page_404s_for_a_design_session_that_does_not_exist(daniel):
    assert daniel.get("/design/not-a-real-id").status_code == 404


def test_the_design_pages_require_a_login(anon):
    assert anon.get("/design", follow_redirects=False).status_code in (303, 401)


def test_the_symbol_route_refuses_to_guess_which_repository(mind):
    response = mind.get(
        f"{main_mod.DESIGN_PREFIX}/symbols",
        params={"mind": "skippy", "path": "src/design_data.py", "name": "publish_page"},
    )

    assert response.status_code == 400
    assert "repository" in response.json()["detail"].lower()


def test_the_symbol_route_takes_the_repository_from_a_single_target_session(mind):
    created = mind.post(
        f"{main_mod.DESIGN_PREFIX}/sessions",
        json={"title": "Design", "repos": ["/only/one"]},
    ).json()

    response = mind.get(
        f"{main_mod.DESIGN_PREFIX}/symbols",
        params={
            "mind": "skippy",
            "path": "src/design_data.py",
            "name": "publish_page",
            "design_id": created["id"],
        },
    )

    # Past the "name the repository" refusal and on to the Mind lookup,
    # which fails here because no such Mind is registered. Asserting only
    # "not 400" would pass on any later failure in the route just as happily.
    assert response.status_code == 404
    assert "repository" not in response.json()["detail"].lower()


def test_the_house_tokens_do_not_open_the_design_routes(tmp_path, monkeypatch):
    """The stated threat is a kid's Windows box publishing to these pages.
    Both house tokens are set here, and neither may be accepted."""
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "house-admin-token")
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "house-service-token")
    client = TestClient(main_mod.app)

    for house in ("house-admin-token", "house-service-token"):
        client.headers.update({"Authorization": f"Bearer {house}"})
        assert client.get(f"{main_mod.DESIGN_PREFIX}/sessions").status_code == 401


async def test_a_mind_with_no_symbols_route_is_reported_as_out_of_date(tmp_path, monkeypatch):
    """Starlette answers a bare 404 for a path matching no route. Passed
    through as a 404 that reads as 'no such function', it would send Daniel
    hunting a typo instead of redeploying the mind."""
    import httpx

    import mind_symbols

    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")

    async def route_missing(url, headers=None, params=None):
        return httpx.Response(404, json={"detail": "Not Found"})

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            return await route_missing(url, headers, params)

    monkeypatch.setattr(mind_symbols.httpx, "AsyncClient", lambda **kw: _Client())

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refusal:
        await mind_symbols.resolve("http://mind:8421", "/repo", "x.py", "thing")

    assert refusal.value.status_code == 501


async def test_a_mind_refusing_a_repository_is_passed_through_as_a_404(monkeypatch):
    import httpx
    from fastapi import HTTPException

    import mind_symbols

    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            return httpx.Response(
                404, json={"error": "Repository is not one this mind will read"}
            )

    monkeypatch.setattr(mind_symbols.httpx, "AsyncClient", lambda **kw: _Client())

    with pytest.raises(HTTPException) as refusal:
        await mind_symbols.resolve("http://mind:8421", "/repo", "x.py", "thing")

    assert refusal.value.status_code == 404
    assert "will read" in refusal.value.detail
