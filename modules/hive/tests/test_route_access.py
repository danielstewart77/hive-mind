"""Who may reach which console route: the one table in route_access.

Minds and their owners come from a comms stand-in over real HTTP (see
multiuser_support): Arnold is sloan's, zack is aiden's, skippy is daniel's,
and `orphan` registered before owners existed.
"""

import asyncio
import json
import re

import pytest
from fastapi import Depends, WebSocket
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

import auth
import console_access
import main
import route_access

REFUSED = route_access.REFUSAL_HEADER

# What each kind of route parameter is filled with: (sloan's, aiden's).
_OWN_AND_OTHER = {
    "name": ("arnold", "zack"),
    "mind_id": ("m-arnold", "m-zack"),
    "session_id": ("s-arnold", "s-zack"),
    "request_id": ("op-sloan", "op-aiden"),
}

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
            rule = console_access.TABLE.rule_for(method, route.path)
            if rule.kind == route_access.OWNER:
                yield method, route.path


@pytest.fixture
def people(builtin_console):
    admin = builtin_console.admin()
    sloan = builtin_console.settled_user(admin, "sloan", "sloans-own-pass")
    builtin_console.create_user(admin, "aiden", "aiden-temp-pass")
    # Recorded through the real operation, on a loop of its own: the store
    # is one SQLite connection on one worker thread, so a write made here is
    # a write the next request sees.
    asyncio.run(console_access.record_operation("op-sloan", "sloan"))
    asyncio.run(console_access.record_operation("op-aiden", "aiden"))
    return admin, sloan


def test_each_listing_shows_a_non_admin_only_their_own_minds_and_the_admin_all(people):
    admin, sloan = people

    def names(rows):
        return sorted(str(row.get("name")) for row in rows)

    def mind_ids(rows):
        return sorted(str(row.get("mind_id")) for row in rows)

    overview = sloan.get("/api/overview").json()
    assert names(overview["minds"]) == ["Arnold"]
    assert overview["counts"]["minds"] == 1
    assert mind_ids(overview["sessions"]) == ["m-arnold"]
    assert names(sloan.get("/api/settings/minds").json()["minds"]) == ["Arnold"]
    sessions = sloan.get("/api/settings/hive-comms/sessions").json()["sessions"]
    assert mind_ids(sessions) == ["m-arnold"]

    everything = ["Arnold", "orphan", "skippy", "zack"]
    assert names(admin.get("/api/overview").json()["minds"]) == everything
    assert names(admin.get("/api/settings/minds").json()["minds"]) == everything
    all_sessions = admin.get("/api/settings/hive-comms/sessions").json()["sessions"]
    assert mind_ids(all_sessions) == ["m-arnold", "m-skippy", "m-zack"]


def test_a_non_admin_is_refused_another_users_mind_on_every_owner_scoped_route(people):
    admin, sloan = people
    routes = list(_owner_scoped_routes())
    assert len(routes) > 20

    for method, template in routes:
        own, other = _fill(template, 0), _fill(template, 1)
        if method == "WS":
            with pytest.raises(WebSocketDisconnect) as closed, _socket(sloan, other) as ws:
                ws.receive_text()
            assert closed.value.code == route_access.CLOSE_REFUSED, template
            continue
        refused = sloan.request(method, other, json={})
        assert refused.status_code == 403, (method, template, refused.status_code)
        assert refused.headers.get(REFUSED) == "owner", (method, template)

        allowed = sloan.request(method, own, json={})
        assert REFUSED not in allowed.headers, (method, template, allowed.text)
        as_admin = admin.request(method, other, json={})
        assert REFUSED not in as_admin.headers, (method, template, as_admin.text)


def test_a_mind_with_no_owner_or_no_registration_is_refused_to_a_non_admin(people):
    admin, sloan = people

    for name in ("orphan", "nobody-registered-this"):
        refused = sloan.get(f"/api/minds/{name}/settings")
        assert refused.status_code == 403
        assert refused.headers.get(REFUSED) == "owner"
    assert REFUSED not in admin.get("/api/minds/orphan/settings").headers


@pytest.mark.parametrize(
    "page, api",
    [
        ("/dashboard", "/api/dashboard"),
        ("/settings/credentials", "/api/settings/credentials"),
        ("/sentinel", "/api/sentinel/review"),
        ("/settings/users", "/api/admin/users"),
    ],
)
def test_a_non_admin_is_refused_each_house_wide_page_and_its_api(people, page, api):
    admin, sloan = people

    for path in (page, api):
        refused = sloan.get(path, follow_redirects=False)
        assert refused.status_code == 403, path
        assert refused.headers.get(REFUSED) == "admin", path
    assert admin.get(page).status_code == 200


def test_every_console_route_is_classified_in_the_table():
    assert console_access.TABLE.unlisted(main.app) == []


def test_a_route_added_with_no_authorization_code_is_refused_to_a_non_admin(people):
    admin, sloan = people

    async def plain(user: dict = Depends(auth.require_auth)):
        return {"ok": user["username"]}

    async def by_name(name: str, user: dict = Depends(auth.require_auth)):
        return {"ok": name}

    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    main.app.add_api_route("/api/brand-new-page", plain, methods=["GET"])
    main.app.add_api_route("/api/minds/{name}/brand-new", by_name, methods=["GET"])
    main.app.add_api_websocket_route("/ws/brand-new", socket)
    added = main.app.router.routes[-3:]
    try:
        for path in ("/api/brand-new-page", "/api/minds/zack/brand-new", "/api/minds/arnold/brand-new"):
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


def test_only_an_admin_moves_a_mind_to_another_owner_and_comms_hears_it_with_the_admin_token(
    people, fake_comms, monkeypatch
):
    admin, sloan = people
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
    fake_comms.received.clear()

    refused = sloan.put("/api/minds/arnold/owner", json={"owner": "sloan"})
    assert refused.status_code == 403
    assert fake_comms.received == []

    assert admin.put("/api/minds/zack/owner", json={"owner": "Sloan"}).status_code == 200
    assert admin.put("/api/minds/zack/owner", json={"owner": "nobody-here"}).status_code == 400
    (sent,) = fake_comms.received
    assert (sent["method"], sent["path"]) == ("PUT", "/broker/minds/m-zack/owner")
    assert sent["authorization"] == "Bearer admin-token"
    assert json.loads(sent["body"]) == {"owner": "sloan"}


# ── bearers, long-lived connections, refusals a browser can read ───────


def _anonymous():
    from starlette.testclient import TestClient

    return TestClient(main.app, base_url="https://testserver")


_BEARER_ROUTES = [
    ("GET", "/canvas/render.svg", None),
    ("GET", "/api/design/sessions", None),
]


@pytest.mark.parametrize("method, path, body", _BEARER_ROUTES)
@pytest.mark.parametrize(
    "authorization",
    [
        [("Authorization", "bearer not-the-token")],
        [("Authorization", "BEARER not-the-token")],
        [("Authorization", "Basic c2xvYW46eA=="), ("Authorization", "Bearer not-the-token")],
        [("Authorization", "Bearer not-the-token"), ("Authorization", "Basic c2xvYW46eA==")],
    ],
    ids=["lowercase", "uppercase", "basic-then-bearer", "bearer-then-basic"],
)
def test_a_bearer_route_refuses_a_wrong_or_doubled_authorization_to_a_non_admin_and_to_anyone(
    people, monkeypatch, method, path, body, authorization
):
    _, sloan = people
    monkeypatch.setenv("CANVAS_PUSH_TOKEN", "the-canvas-token")
    monkeypatch.setenv("DESIGN_PUSH_TOKEN", "the-design-token")

    for who, client in (("sloan", sloan), ("anonymous", _anonymous())):
        response = client.request(method, path, json=body, headers=authorization)
        assert response.status_code in (400, 401), (who, response.status_code, response.text)


def test_a_bearer_is_admitted_whatever_the_case_of_its_scheme(people, monkeypatch):
    monkeypatch.setenv("CANVAS_PUSH_TOKEN", "the-canvas-token")
    monkeypatch.setenv("DESIGN_PUSH_TOKEN", "the-design-token")
    client = _anonymous()

    svg = client.get(
        "/canvas/render.svg",
        headers={"Authorization": "bearer the-canvas-token"},  # secret-guard: allow
    )
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    listed = client.get(
        "/api/design/sessions",
        headers={"authorization": "BEARER the-design-token"},  # secret-guard: allow
    )
    assert listed.status_code == 200, listed.text


def test_a_refused_websocket_is_accepted_then_closed_with_its_reason(people):
    """Closed before it is accepted, a socket reaches a browser as a bare
    1006: the code is only readable on one the server accepted first."""
    _, sloan = people
    with _socket(sloan, "/ws/canvas") as ws, pytest.raises(
        WebSocketDisconnect
    ) as closed:  # accepted: entering did not raise
        ws.receive_text()
    assert closed.value.code == route_access.CLOSE_REFUSED


def _closed_within(ws, seconds: float):
    """The close code of `ws` if the server closes it within `seconds`, else
    None. The receive runs on a thread, since a socket nobody closes would
    otherwise hold the test forever."""
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


def test_an_open_canvas_socket_is_closed_once_its_user_stops_being_an_admin(
    builtin_console, monkeypatch
):
    monkeypatch.setattr(route_access, "RECHECK_SECONDS", 0.2)
    admin = builtin_console.admin()
    second = builtin_console.settled_user(admin, "ada", "adas-own-pass")
    assert admin.put("/api/admin/groups/admins/members/ada").status_code == 204

    with _socket(second, "/ws/canvas") as ws:
        assert ws.receive_json()["type"] == "state"
        assert admin.delete("/api/admin/groups/admins/members/ada").status_code == 204
        assert _closed_within(ws, 5) == route_access.CLOSE_REFUSED


def test_an_owner_route_refuses_a_non_admin_while_comms_cannot_say_who_owns_the_mind(
    people, monkeypatch
):
    admin, sloan = people
    monkeypatch.setenv("GATEWAY_API_URL", "http://127.0.0.1:9")

    refused = sloan.get("/api/minds/arnold/settings")
    assert refused.status_code == 403
    assert refused.headers.get(REFUSED) == "owner"
    assert REFUSED not in admin.get("/api/minds/arnold/settings").headers


def test_the_canvas_review_tells_the_mind_to_fetch_the_snapshot_with_its_bearer(
    builtin_console, fake_comms
):
    admin = builtin_console.admin()
    fake_comms.received.clear()

    assert admin.post("/canvas/submit", json={}).status_code == 200
    (sent,) = [r for r in fake_comms.received if r["path"] == "/broker/messages"]
    content = json.loads(sent["body"])["content"]
    assert "https://testserver/canvas/render.svg" in content
    assert "Authorization: Bearer $CANVAS_PUSH_TOKEN" in content


def test_the_cutover_check_reaches_the_board_and_the_memory_pages_with_its_own_bearer(
        builtin_console, monkeypatch):
    """The one-database check runs outside a browser, so it has no session to
    present — and an unchecked app is reported as unchecked rather than
    counted as working. The three reads it makes take a bearer this install
    issued for the purpose; nothing else does, and a wrong one is refused."""
    monkeypatch.setenv("HIVE_SITE_ADMIN_TOKEN", "a-cutover-token")
    anonymous = builtin_console.client()

    for path in ("/api/kanban/data", "/api/memory/tables",
                 "/api/memory/rows?table=memories"):
        with_token = anonymous.get(
            path, headers={"Authorization": "Bearer a-cutover-token"})
        assert with_token.status_code != 401, (path, with_token.text)
        assert REFUSED not in with_token.headers, (path, with_token.text)

        wrong = anonymous.get(
            path, headers={"Authorization": "Bearer not-the-token"})
        assert wrong.status_code == 401, (path, wrong.text)

        without = anonymous.get(path, follow_redirects=False)
        assert without.status_code in (303, 401), (path, without.text)

    # The bearer admits those reads and nothing else: it is not a way into
    # the console at large.
    elsewhere = anonymous.get(
        "/api/admin/users",
        headers={"Authorization": "Bearer a-cutover-token"})
    assert elsewhere.status_code in (401, 403), elsewhere.text


def test_an_install_that_issued_no_cutover_token_admits_no_bearer(
        builtin_console, monkeypatch):
    """Where every install that is not mid-cutover sits: the routes answer to
    a session and to nothing else, so an unset token is not an open door."""
    monkeypatch.delenv("HIVE_SITE_ADMIN_TOKEN", raising=False)
    anonymous = builtin_console.client()

    refused = anonymous.get(
        "/api/kanban/data", headers={"Authorization": "Bearer anything"})
    assert refused.status_code == 401, refused.text


def test_the_cutover_bearer_is_attached_to_those_three_routes_and_no_others():
    """Read off the table itself, because a bearer in the wrong rule is a
    sessionless way into whatever that rule covers.

    `Rule(OWNER, param="name")` carries about twenty-five per-mind routes
    including the writes to a Mind's hooks, its skills, its soul and its bot
    token. The cutover check needs three reads; a bearer that reached those
    writes would be a credential in one `.env` that edits every Mind on the
    hive without a session. Asserting the three by looping over them cannot
    see that, which is why this asserts the whole set.
    """
    carrying = {
        f"{method} {path}"
        for (method, path), rule in console_access.TABLE.rules.items()
        if rule.bearer == console_access.CUTOVER
    }

    assert carrying == {
        "GET /api/kanban/data",
        "GET /api/memory/tables",
        "GET /api/memory/rows",
    }


def test_every_bearer_the_table_declares_is_used_by_some_rule():
    """A bearer nobody names is a credential the table reads as configured
    and no route ever admits — which looks like a working guard and is a
    dead one."""
    named = {rule.bearer for rule in console_access.TABLE.rules.values()
             if rule.bearer}

    assert named == set(console_access.TABLE.bearers)
