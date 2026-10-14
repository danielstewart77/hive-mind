"""Who may reach which terminal route: the one table in terminal_access.

Minds, sessions and their owners come from a comms stand-in over real HTTP
(see multiuser_support): Arnold and its session s-arnold are sloan's, zack
and s-zack are aiden's, skippy and s-skippy are daniel's, and `orphan`
registered before owners existed.
"""

import re

import pytest
from fastapi import Depends, WebSocket
from starlette.routing import Mount
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import auth
import main
import route_access
import terminal_access

REFUSED = route_access.REFUSAL_HEADER

# What each kind of route parameter is filled with: (sloan's, aiden's).
_OWN_AND_OTHER = {"session_id": ("s-arnold", "s-zack")}


def _socket(client, path):
    """A websocket from this client. Its cookies are passed by hand: they
    are Secure, and the test client's jar withholds Secure cookies from a
    `wss://testserver` URL that a browser would send them to."""
    cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    return client.websocket_connect(path, headers={"cookie": cookie})


def _fill(template: str, which: int) -> str:
    def value(match):
        name = match.group(1)
        return _OWN_AND_OTHER[name][which] if name in _OWN_AND_OTHER else "x"

    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}", value, template)


def _owner_scoped_routes():
    for route in main.app.routes:
        if isinstance(route, Mount):
            continue
        for method in sorted(getattr(route, "methods", None) or {"WS"}):
            if method == "HEAD":
                continue
            if terminal_access.TABLE.rule_for(method, route.path).kind == route_access.OWNER:
                yield method, route.path


@pytest.fixture
def people(builtin_terminal):
    admin = builtin_terminal.admin()
    sloan = builtin_terminal.settled_user("sloan", "sloans-own-pass")
    builtin_terminal.create_user("aiden", "aiden-temp-pass")
    return admin, sloan


def test_each_terminal_listing_shows_a_non_admin_only_their_own_minds_and_the_admin_all(people):
    admin, sloan = people

    def names(rows):
        return sorted(str(row.get("name")) for row in rows)

    def mind_ids(rows):
        return sorted(str(row.get("mind_id")) for row in rows)

    assert names(sloan.get("/api/minds").json()) == ["Arnold"]
    assert mind_ids(sloan.get("/api/terminal/sessions").json()) == ["m-arnold"]
    assert sorted(sloan.get("/api/terminal/labels").json()) == ["s-arnold"]

    assert names(admin.get("/api/minds").json()) == ["Arnold", "orphan", "skippy", "zack"]
    everything = ["m-arnold", "m-skippy", "m-zack"]
    assert mind_ids(admin.get("/api/terminal/sessions").json()) == everything
    assert sorted(admin.get("/api/terminal/labels").json()) == ["s-arnold", "s-skippy", "s-zack"]


def test_a_non_admin_is_refused_another_users_session_on_every_session_route_and_the_attach_socket(people):
    admin, sloan = people
    routes = list(_owner_scoped_routes())
    assert ("WS", "/api/terminal/attach/{session_id}") in routes
    assert len(routes) >= 7

    for method, template in routes:
        own, other = _fill(template, 0), _fill(template, 1)
        if method == "WS":
            with pytest.raises(WebSocketDisconnect) as closed, _socket(sloan, other) as ws:
                ws.receive_bytes()
            assert closed.value.code == route_access.CLOSE_REFUSED, template
            with _socket(sloan, own) as ws:
                assert ws.receive_bytes() == b"attached s-arnold"
            with _socket(admin, other) as ws:
                assert ws.receive_bytes() == b"attached s-zack"
            continue
        refused = sloan.request(method, other, json={})
        assert refused.status_code == 403, (method, template, refused.status_code)
        assert refused.headers.get(REFUSED) == "owner", (method, template)

        allowed = sloan.request(method, own, json={})
        assert REFUSED not in allowed.headers, (method, template, allowed.text)
        as_admin = admin.request(method, other, json={})
        assert REFUSED not in as_admin.headers, (method, template, as_admin.text)


def test_a_non_admin_cannot_open_a_session_on_another_users_mind(people, fake_comms):
    admin, sloan = people
    fake_comms.received.clear()

    for mind in ("m-zack", "zack", "m-orphan", "never-registered"):
        refused = sloan.post("/api/terminal/sessions", json={"mind_id": mind})
        assert refused.status_code == 403, mind
        assert refused.headers.get(REFUSED) == "owner", mind
    assert [r for r in fake_comms.received if r["path"] == "/sessions"] == []

    assert sloan.post("/api/terminal/sessions", json={"mind_id": "m-arnold"}).status_code == 200
    assert admin.post("/api/terminal/sessions", json={"mind_id": "m-zack"}).status_code == 200
    opened = [r for r in fake_comms.received if r["path"] == "/sessions"]
    assert len(opened) == 2


def test_a_wrong_label_bearer_does_not_fall_back_to_the_cookie(people, fake_comms, monkeypatch):
    """The table hands any bearer request straight to the label route, so a
    bearer the route does not recognise must refuse, not drop to the cookie
    — which would rename another user's conversation with no owner check."""
    _, sloan = people
    monkeypatch.setenv("TERMINAL_LABELS_TOKEN", "the-real-label-token")
    fake_comms.received.clear()

    for path in ("/api/terminal/labels/s-zack", "/api/terminal/labels/s-arnold"):
        refused = sloan.put(
            path, json={"name": "mine now"}, headers={"Authorization": "Bearer wrong"}
        )
        assert refused.status_code == 401, path
    assert fake_comms.received == []

    # The mind renaming over Telegram: the right bearer and no cookie at all.
    service = TestClient(main.app, base_url="https://testserver")
    written = service.put(
        "/api/terminal/labels/s-zack",
        json={"name": "renamed"},
        headers={"Authorization": "Bearer the-real-label-token"},  # secret-guard: allow
    )
    assert written.status_code == 200
    assert [r["path"] for r in fake_comms.received] == ["/sessions/s-zack/name"]


def test_every_terminal_route_is_classified_in_the_table():
    assert terminal_access.TABLE.unlisted(main.app) == []


def test_a_terminal_route_added_with_no_authorization_code_is_refused_to_a_non_admin(people):
    admin, sloan = people

    async def plain(user: dict = Depends(auth.require_auth)):
        return {"ok": user["username"]}

    async def by_session(session_id: str, user: dict = Depends(auth.require_auth)):
        return {"ok": session_id}

    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    main.app.add_api_route("/api/brand-new-page", plain, methods=["GET"])
    main.app.add_api_route("/api/terminal/sessions/{session_id}/brand-new", by_session, methods=["GET"])
    main.app.add_api_websocket_route("/ws/brand-new", socket)
    added = main.app.router.routes[-3:]
    try:
        for path in (
            "/api/brand-new-page",
            "/api/terminal/sessions/s-zack/brand-new",
            "/api/terminal/sessions/s-arnold/brand-new",
        ):
            refused = sloan.get(path)
            assert refused.status_code == 403, path
            assert admin.get(path).status_code == 200, path
        with pytest.raises(WebSocketDisconnect) as closed, _socket(sloan, "/ws/brand-new") as ws:
            ws.receive_text()
        assert closed.value.code == route_access.CLOSE_REFUSED
        with _socket(admin, "/ws/brand-new") as ws:
            assert ws.receive_text() == "hello"
    finally:
        for route in added:
            main.app.router.routes.remove(route)


def test_a_microsoft_or_windows_session_from_the_console_opens_the_owners_terminal_and_no_one_elses(builtin_terminal):
    store = builtin_terminal.store()
    builtin_terminal.create_user("sloan", "sloan-temp-pass")
    sloan = builtin_terminal.client()
    sloan.cookies.set(auth.SSO_COOKIE_NAME, auth.issue_sso_token(store, "sloan", "microsoft"))

    with _socket(sloan, "/api/terminal/attach/s-arnold") as ws:
        assert ws.receive_bytes() == b"attached s-arnold"
    with pytest.raises(WebSocketDisconnect) as closed, _socket(sloan, "/api/terminal/attach/s-zack") as ws:
        ws.receive_bytes()
    assert closed.value.code == route_access.CLOSE_REFUSED

    store.update_user("sloan", disabled=True)
    assert sloan.get("/api/terminal/sessions").status_code == 401
