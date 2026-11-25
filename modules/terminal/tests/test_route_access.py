"""Who may reach which terminal route: the one table in terminal_access.

Minds, sessions and their owners come from a comms stand-in over real HTTP
(see multiuser_support): Arnold and its session s-arnold are sloan's, zack
and s-zack are aiden's, skippy and s-skippy are daniel's, and `orphan`
registered before owners existed.
"""

import re

import pytest

from hive_site import testing as accounts
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
    account_store = builtin_terminal.store()
    builtin_terminal.create_user("sloan", "sloan-temp-pass")
    sloan = builtin_terminal.client()
    sloan.cookies.set(
        auth.SSO_COOKIE_NAME,
        accounts.run(auth.issue_sso_token(account_store, "sloan", "microsoft")))

    with _socket(sloan, "/api/terminal/attach/s-arnold") as ws:
        assert ws.receive_bytes() == b"attached s-arnold"
    with pytest.raises(WebSocketDisconnect) as closed, _socket(sloan, "/api/terminal/attach/s-zack") as ws:
        ws.receive_bytes()
    assert closed.value.code == route_access.CLOSE_REFUSED

    accounts.run(account_store.update_user("sloan", disabled=True))
    assert sloan.get("/api/terminal/sessions").status_code == 401


# ── bearers, long-lived connections, refusals a browser can read ───────


def _anonymous():
    return TestClient(main.app, base_url="https://testserver")


@pytest.mark.parametrize(
    "authorization",
    [
        [("Authorization", "Basic c2xvYW46eA=="), ("Authorization", "Bearer the-real-label-token")],  # secret-guard: allow
        [("Authorization", "Bearer the-real-label-token"), ("Authorization", "Basic c2xvYW46eA==")],  # secret-guard: allow
    ],
    ids=["basic-then-bearer", "bearer-then-basic"],
)
def test_a_label_call_with_a_doubled_authorization_is_refused_to_a_non_admin_and_to_anyone(
    people, fake_comms, monkeypatch, authorization
):
    _, sloan = people
    monkeypatch.setenv("TERMINAL_LABELS_TOKEN", "the-real-label-token")
    fake_comms.received.clear()

    for client in (sloan, _anonymous()):
        refused = client.put(
            "/api/terminal/labels/s-zack", json={"name": "mine now"}, headers=authorization
        )
        assert refused.status_code in (400, 401), refused.text
        listed = client.get("/api/terminal/labels", headers=authorization)
        assert listed.status_code in (400, 401), listed.text
    assert fake_comms.received == []


def test_a_refused_attach_is_accepted_then_closed_with_its_reason(people):
    """Closed before it is accepted, a socket reaches a browser as a bare
    1006, and the tile reattaches forever: the code is only readable on a
    socket the server accepted first."""
    _, sloan = people
    with _socket(sloan, "/api/terminal/attach/s-zack") as ws, pytest.raises(
        WebSocketDisconnect
    ) as closed:  # accepted
        ws.receive_bytes()
    assert closed.value.code == route_access.CLOSE_REFUSED


def _closed_within(ws, seconds: float):
    """The close code of `ws` if the server closes it within `seconds`, else
    None. Received on a thread: a socket nobody closes would otherwise hold
    the test forever."""
    import threading

    outcome: dict = {}

    def drain():
        while not outcome.get("stop"):
            message = ws.receive()
            if message["type"] == "websocket.close":
                outcome["code"] = message.get("code")
                return

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    thread.join(seconds)
    outcome["stop"] = True
    return outcome.get("code")


@pytest.mark.parametrize(
    "change, code",
    [("disabled", route_access.CLOSE_UNAUTHENTICATED), ("owner moved", route_access.CLOSE_REFUSED)],
)
def test_an_open_attach_is_closed_once_its_user_may_no_longer_hold_it(
    people, builtin_terminal, monkeypatch, change, code
):
    from multiuser_support import MINDS

    _, sloan = people
    monkeypatch.setattr(route_access, "RECHECK_SECONDS", 0.2)

    with _socket(sloan, "/api/terminal/attach/s-arnold") as ws:
        assert ws.receive_bytes() == b"attached s-arnold"
        if change == "disabled":
            accounts.run(
                builtin_terminal.store().update_user("sloan", disabled=True))
        else:
            arnold = next(m for m in MINDS if m["id"] == "m-arnold")
            monkeypatch.setitem(arnold, "owner", "aiden")
        assert _closed_within(ws, 5) == code


def test_an_open_session_stream_ends_once_its_user_is_disabled(
    people, builtin_terminal, fake_comms, monkeypatch
):
    import threading

    _, sloan = people
    monkeypatch.setattr(route_access, "RECHECK_SECONDS", 0.2)
    monkeypatch.setitem(fake_comms.stream, "seconds", 30.0)
    finished = threading.Event()

    def read_stream():
        sloan.get("/api/console/s-arnold/stream")
        finished.set()

    threading.Thread(target=read_stream, daemon=True).start()
    assert not finished.wait(1.0)  # streaming, as it should while she may
    accounts.run(builtin_terminal.store().update_user("sloan", disabled=True))
    assert finished.wait(5.0)


def test_a_non_admin_speaks_only_in_the_voices_of_their_own_minds(people, fake_comms, monkeypatch):
    admin, sloan = people
    monkeypatch.setenv("VOICE_API_URL", fake_comms.url)

    for voice in ("zack", "skippy", "skippy-clip.wav", "default"):
        refused = sloan.post("/api/terminal/tts", json={"text": "hello", "voice_id": voice})
        assert refused.status_code == 403, voice
    for voice in ("Arnold", "arnold-clip.wav"):
        assert sloan.post("/api/terminal/tts", json={"text": "hi", "voice_id": voice}).status_code == 200
    assert admin.post("/api/terminal/tts", json={"text": "hi", "voice_id": "zack"}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/login"])
def test_a_public_route_asks_the_sign_in_service_without_blocking_the_event_loop(
    monkeypatch, fake_comms, path
):
    """A blocking call to auth-service stalls every terminal the site serves.

    It used to be kept off the loop by hand, with a synchronous `httpx`
    client pushed through `run_in_threadpool`. The shared store awaits an
    `httpx.AsyncClient` instead, so what this guards now is that the request
    is awaited on the loop rather than issued by a blocking client that
    would hold it — the transport records where it was called from, and a
    sign-in store put back on a synchronous client fails here.
    """
    import asyncio

    import httpx

    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "auth-service")
    monkeypatch.setenv("AUTH_SERVICE_URL", "http://auth-service.invalid")
    asked_from: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        try:
            asyncio.get_running_loop()
            asked_from.append("the event loop, awaited")
        except RuntimeError:
            asked_from.append("a blocking client")
        return httpx.Response(401, json={"detail": "expired"})

    account_store = auth.get_store()
    monkeypatch.setattr(
        account_store, "_loop_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    client = _anonymous()
    client.cookies.set("auth_session", "a-token")

    client.get(path, follow_redirects=False)

    assert asked_from
    assert set(asked_from) == {"the event loop, awaited"}
