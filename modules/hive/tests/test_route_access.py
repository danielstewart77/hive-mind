"""Who may reach which console route: the one table in route_access.

Minds and their owners come from a comms stand-in over real HTTP (see
multiuser_support): Arnold is sloan's, zack is aiden's, skippy is daniel's,
and `orphan` registered before owners existed.
"""

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
from hive_site.testing import run

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
    # Recorded through the real operation, on the suite's loop, which the
    # store's pool and every request share.
    run(console_access.record_operation("op-sloan", "sloan"))
    run(console_access.record_operation("op-aiden", "aiden"))
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
        # A mind is refused as though it were not there; a session or an
        # operation, which the asker reached by an id of their own, is not.
        names_a_mind = console_access.TABLE.rule_for(method, template).param in (
            "name", "mind_id")
        assert refused.status_code == (404 if names_a_mind else 403), (
            method, template, refused.status_code)
        assert refused.headers.get(REFUSED) == "owner", (method, template)

        allowed = sloan.request(method, own, json={})
        assert REFUSED not in allowed.headers, (method, template, allowed.text)
        as_admin = admin.request(method, other, json={})
        assert REFUSED not in as_admin.headers, (method, template, as_admin.text)


def test_a_mind_with_no_owner_or_no_registration_is_refused_to_a_non_admin(people):
    admin, sloan = people

    for name in ("orphan", "nobody-registered-this"):
        refused = sloan.get(f"/api/minds/{name}/settings")
        assert refused.status_code == 404
        assert refused.headers.get(REFUSED) == "owner"
    assert REFUSED not in admin.get("/api/minds/orphan/settings").headers


def test_somebody_who_neither_owns_a_mind_nor_is_an_admin_is_answered_as_though_it_were_not_there(
    people,
):
    """Daniel, 2026-11-17: "If this mind is not owned by me, I can't see the
    mind at all, much less change its settings." Aiden's mind, asked for by
    sloan, and a mind nobody ever registered: the same answer, word for word,
    for the settings and for the page."""
    admin, sloan = people

    for path in ("/api/minds/{}/settings", "/minds/{}"):
        anothers = sloan.get(path.format("zack"))
        nobodys = sloan.get(path.format("nobody-registered-this"))
        assert anothers.status_code == 404, path
        assert (nobodys.status_code, nobodys.text, nobodys.headers.get(REFUSED)) == (
            anothers.status_code, anothers.text, anothers.headers.get(REFUSED)), path
    assert sloan.get("/api/minds/zack/settings").json() == {"detail": "No such mind."}
    assert REFUSED not in admin.get("/api/minds/zack/settings").headers


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
    assert refused.status_code == 404
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
        if console_access.CUTOVER in rule.bearers
    }

    assert carrying == {
        "GET /api/kanban/data",
        "GET /api/memory/tables",
        "GET /api/memory/rows",
    }


def test_the_board_bearer_is_attached_to_the_board_and_no_others():
    """Read off the table itself, for the reason the cutover test gives: a
    bearer in the wrong rule is a sessionless way into whatever that rule
    covers, and the rule next door carries every per-mind write on the hive.

    The delete is on it: the chief of staff has full create, read, update
    and delete on the board (Daniel, 2026-11-19).
    """
    carrying = {
        f"{method} {path}"
        for (method, path), rule in console_access.TABLE.rules.items()
        if console_access.BOARD in rule.bearers
    }

    assert carrying == {
        "GET /api/kanban/data",
        "GET /api/kanban/numbers",
        "GET /api/kanban/relations/{record_id}",
        "GET /api/kanban/{kind}/{record_id}/descendants",
        "POST /api/kanban/epics",
        "POST /api/kanban/features",
        "POST /api/kanban/stories",
        "POST /api/kanban/tasks",
        "PATCH /api/kanban/{kind}/{record_id}",
        "DELETE /api/kanban/{kind}/{record_id}",
        "GET /api/kanban/items/{record_id}/attachments",
        "POST /api/kanban/items/{record_id}/attachments",
        "GET /api/kanban/items/{record_id}/attachments/{attachment_id}",
    }


def test_every_bearer_the_table_declares_is_used_by_some_rule():
    """A bearer nobody names is a credential the table reads as configured
    and no route ever admits — which looks like a working guard and is a
    dead one."""
    named = {name for rule in console_access.TABLE.rules.values()
             for name in rule.bearers}

    assert named == set(console_access.TABLE.bearers)


def test_a_mind_reads_and_writes_the_board_with_the_board_bearer_and_no_session(
        builtin_console, monkeypatch):
    """The reach this whole credential exists to grant, exercised end to end
    rather than read off the table.

    A mind working the backlog runs outside a browser and has no session to
    present. Without a door of its own the things it reaches for instead are
    raw SQL against `work_items` — which skips the ordering, the numbering
    and the write lock the board module holds — or `docker exec` into this
    container. So this asserts the four operations it was given the token
    for: list the board, create under it, update a row, and read a row's
    relations. A wrong value is refused on each, so the token is the guard
    and not merely the thing that happened to be in the header.
    """
    monkeypatch.setenv("BOARD_PUSH_TOKEN", "a-board-token")
    mind = builtin_console.client()
    good = {"Authorization": "Bearer a-board-token"}
    wrong = {"Authorization": "Bearer not-the-token"}

    listing = mind.get("/api/kanban/data", headers=good)
    assert listing.status_code == 200, listing.text
    assert REFUSED not in listing.headers

    created = mind.post("/api/kanban/epics",
                        json={"title": "An epic the chief of staff opened"},
                        headers=good)
    assert created.status_code == 200, created.text
    epic_id = created.json()["id"]

    renamed = mind.patch(f"/api/kanban/epics/{epic_id}",
                         json={"title": "Renamed by the same bearer"},
                         headers=good)
    assert renamed.status_code == 200, renamed.text

    # Stored, not merely reported: the point of the write is that the board
    # the operator opens in a browser now says this.
    after = mind.get("/api/kanban/data", headers=good).json()
    titles = [row["title"] for row in after["epics"]]
    assert "Renamed by the same bearer" in titles, after

    relations = mind.get(f"/api/kanban/relations/{epic_id}", headers=good)
    assert relations.status_code == 200, relations.text

    for method, path, body in (
        ("get", "/api/kanban/data", None),
        ("get", "/api/kanban/numbers", None),
        ("get", f"/api/kanban/relations/{epic_id}", None),
        ("post", "/api/kanban/epics", {"title": "refused"}),
        ("patch", f"/api/kanban/epics/{epic_id}", {"title": "refused"}),
    ):
        call = getattr(mind, method)
        refused = call(path, headers=wrong, **({"json": body} if body else {}))
        assert refused.status_code == 401, (path, refused.text)


def test_a_mind_deletes_a_row_and_what_is_under_it_with_the_board_bearer(
        builtin_console, monkeypatch):
    """The chief of staff has full create, read, update and delete on the
    board (Daniel, 2026-11-19), so the delete answers the board bearer with
    no session, takes the row and everything under it, and says what went.
    """
    monkeypatch.setenv("BOARD_PUSH_TOKEN", "a-board-token")
    mind = builtin_console.client()
    good = {"Authorization": "Bearer a-board-token"}

    epic_id = mind.post("/api/kanban/epics", json={"title": "Going"},
                        headers=good).json()["id"]
    feature_id = mind.post(
        "/api/kanban/features", json={"title": "Under it", "epic_id": epic_id},
        headers=good).json()["id"]
    kept_id = mind.post("/api/kanban/epics", json={"title": "Staying"},
                        headers=good).json()["id"]

    removed = mind.delete(f"/api/kanban/epics/{epic_id}", headers=good,
                          follow_redirects=False)

    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"]["features"] == 1
    after = mind.get("/api/kanban/data", headers=good).json()
    assert {row["id"] for row in after["epics"]} == {kept_id}, after
    assert feature_id not in {row["id"] for row in after["features"]}, after


def test_a_planning_mind_reads_and_writes_the_board_with_the_planner_bearer(
        builtin_console, monkeypatch):
    """A mind that plans the backlog and is not the chief of staff holds a
    second board credential (Daniel, 2026-11-20). It lists the board, creates
    under it and updates a row with no session, and what it wrote is what the
    board then says.
    """
    monkeypatch.setenv("BOARD_PUSH_TOKEN", "a-board-token")
    monkeypatch.setenv("BOARD_PLANNER_TOKEN", "a-planner-token")
    mind = builtin_console.client()
    planner = {"Authorization": "Bearer a-planner-token"}

    created = mind.post("/api/kanban/epics",
                        json={"title": "An epic the planner opened"},
                        headers=planner)
    assert created.status_code == 200, created.text
    epic_id = created.json()["id"]

    renamed = mind.patch(f"/api/kanban/epics/{epic_id}",
                         json={"title": "Renamed by the planner"},
                         headers=planner)
    assert renamed.status_code == 200, renamed.text

    listing = mind.get("/api/kanban/data", headers=planner)
    assert listing.status_code == 200, listing.text
    assert [row["title"] for row in listing.json()["epics"]] == [
        "Renamed by the planner"]


def test_the_planner_bearer_is_refused_the_delete_and_the_row_stays(
        builtin_console, monkeypatch):
    """The one route the two board credentials differ by. The planner's
    delete is refused, the row it named is still on the board, and the same
    delete with the chief of staff's token takes it.
    """
    monkeypatch.setenv("BOARD_PUSH_TOKEN", "a-board-token")
    monkeypatch.setenv("BOARD_PLANNER_TOKEN", "a-planner-token")
    mind = builtin_console.client()
    planner = {"Authorization": "Bearer a-planner-token"}
    full = {"Authorization": "Bearer a-board-token"}
    epic_id = mind.post("/api/kanban/epics", json={"title": "Staying"},
                        headers=planner).json()["id"]

    refused = mind.delete(f"/api/kanban/epics/{epic_id}", headers=planner,
                          follow_redirects=False)

    assert refused.status_code == 401, refused.text
    after = mind.get("/api/kanban/data", headers=planner).json()
    assert [row["id"] for row in after["epics"]] == [epic_id]

    removed = mind.delete(f"/api/kanban/epics/{epic_id}", headers=full)
    assert removed.status_code == 200, removed.text
    assert mind.get("/api/kanban/data", headers=full).json()["epics"] == []


def test_the_board_bearer_is_not_a_way_into_the_console(
        builtin_console, monkeypatch):
    """The token is not a session: it admits the board and nothing else on
    the console, so it cannot be used to read the memory store or edit
    another Mind.
    """
    monkeypatch.setenv("BOARD_PUSH_TOKEN", "a-board-token")
    mind = builtin_console.client()
    good = {"Authorization": "Bearer a-board-token"}

    elsewhere = mind.get("/api/memory/tables", headers=good,
                         follow_redirects=False)
    assert elsewhere.status_code in (303, 401, 403), elsewhere.text


def test_a_delete_asked_to_spare_a_row_with_rows_under_it_removes_nothing(
        builtin_console, monkeypatch):
    """The board tool deletes with `only_if_empty` unless the cascade was
    asked for, so a row that has something under it stays, with what is
    under it, and the answer says what that is. The same row with nothing
    under it goes.
    """
    monkeypatch.setenv("BOARD_PUSH_TOKEN", "a-board-token")
    mind = builtin_console.client()
    good = {"Authorization": "Bearer a-board-token"}
    epic_id = mind.post("/api/kanban/epics", json={"title": "Has one"},
                        headers=good).json()["id"]
    feature_id = mind.post(
        "/api/kanban/features", json={"title": "Under it", "epic_id": epic_id},
        headers=good).json()["id"]

    spared = mind.delete(f"/api/kanban/epics/{epic_id}?only_if_empty=true",
                         headers=good)

    assert spared.status_code == 400, spared.text
    assert "1 feature" in spared.json()["detail"]
    after = mind.get("/api/kanban/data", headers=good).json()
    assert [row["id"] for row in after["epics"]] == [epic_id]
    assert [row["id"] for row in after["features"]] == [feature_id]

    gone = mind.delete(
        f"/api/kanban/features/{feature_id}?only_if_empty=true", headers=good)
    assert gone.status_code == 200, gone.text
    assert mind.get("/api/kanban/data", headers=good).json()["features"] == []


def test_an_install_that_issued_no_board_token_admits_no_bearer_to_the_board(
        builtin_console, monkeypatch):
    """Where an install with no mind on the backlog sits: an unset token is
    not an open door, and the board answers to a session and nothing else."""
    monkeypatch.delenv("BOARD_PUSH_TOKEN", raising=False)
    monkeypatch.delenv("HIVE_SITE_ADMIN_TOKEN", raising=False)
    anonymous = builtin_console.client()

    for path in ("/api/kanban/data", "/api/kanban/numbers"):
        refused = anonymous.get(path, headers={"Authorization": "Bearer anything"})
        assert refused.status_code == 401, (path, refused.text)


def test_the_board_and_the_cutover_bearer_each_admit_the_one_route_they_share(
        builtin_console, monkeypatch):
    """`GET /api/kanban/data` is named by two unrelated credentials, which is
    the only reason a rule may name more than one.

    Each has to be admitted on its own, and neither may be admitted by the
    other's value — a route that accepted any configured bearer would make
    every token on the table a key to every route that names one.
    """
    monkeypatch.setenv("BOARD_PUSH_TOKEN", "a-board-token")
    monkeypatch.setenv("HIVE_SITE_ADMIN_TOKEN", "a-cutover-token")
    anonymous = builtin_console.client()

    for token in ("a-board-token", "a-cutover-token"):
        admitted = anonymous.get("/api/kanban/data",
                                 headers={"Authorization": f"Bearer {token}"})
        assert admitted.status_code == 200, (token, admitted.text)

    # The board token reaches the board; it does not become the cutover
    # check's credential by sharing one route with it.
    memory = anonymous.get("/api/memory/tables",
                           headers={"Authorization": "Bearer a-board-token"},
                           follow_redirects=False)
    assert memory.status_code in (303, 401, 403), memory.text

    # And the cutover token does not become a board writer by the same share.
    write = anonymous.post("/api/kanban/epics", json={"title": "no"},
                           headers={"Authorization": "Bearer a-cutover-token"},
                           follow_redirects=False)
    assert write.status_code in (303, 401, 403), write.text


# ── the operator's roles bearer (feature 856, story 860) ───────────────

_ROLES_GOOD = {"Authorization": "Bearer an-operator-token"}
_ROLES_WRONG = {"Authorization": "Bearer not-the-token"}


def _roles_in_the_listing(client, headers):
    listing = client.get("/api/roles", headers=headers)
    assert listing.status_code == 200, listing.text
    return {row["slug"]: row for row in listing.json()["roles"] if row["id"]}


def test_the_roles_bearer_is_attached_to_the_roles_page_routes_and_no_others():
    """Read off the table itself, for the reason the cutover test gives: a
    bearer in the wrong rule is a sessionless way into whatever that rule
    covers. The operator may do anything to a role and nothing else on the
    console, so a new route carrying this bearer has to be a decision here.
    """
    carrying = {
        f"{method} {path}"
        for (method, path), rule in console_access.TABLE.rules.items()
        if console_access.ROLES in rule.bearers
    }

    assert carrying == {
        "GET /api/roles",
        "GET /api/roles/catalog",
        "POST /api/roles",
        "PUT /api/roles/{role_id}",
        "DELETE /api/roles/{role_id}",
        "POST /api/roles/{role_id}/skills",
        "DELETE /api/roles/{role_id}/skills/{skill}",
        "POST /api/roles/{role_id}/keep",
        "POST /api/roles/{role_id}/promote",
        "POST /api/roles/repo/{slug}/take",
    }


def test_the_operator_creates_changes_and_deletes_a_role_with_the_roles_bearer_and_no_session(
        builtin_console, monkeypatch):
    """Everything a person does on the Roles page, with no session: make a
    role, change its name, model, skills, tools and hooks, add and remove one
    skill, and delete it — and the listing the page reads shows each, so the
    change is stored and not merely reported."""
    monkeypatch.setenv("ROLES_PUSH_TOKEN", "an-operator-token")
    operator = builtin_console.client()

    made = operator.post("/api/roles", headers=_ROLES_GOOD, json={
        "name": "Reviewer", "model": "a-model", "skills": ["read"],
        "tools": ["files"], "hooks": ["guard"]})
    assert made.status_code == 200, made.text
    rid = made.json()["id"]
    role = _roles_in_the_listing(operator, _ROLES_GOOD)["reviewer"]
    assert (role["name"], role["model"], role["skills"], role["tools"],
            role["hooks"]) == ("Reviewer", "a-model", ["read"], ["files"], ["guard"])

    changed = operator.put(f"/api/roles/{rid}", headers=_ROLES_GOOD, json={
        "name": "Chief Reviewer", "model": None, "skills": ["read", "write"],
        "tools": ["shell", "web"], "hooks": ["guard", "audit"]})
    assert changed.status_code == 200, changed.text
    role = _roles_in_the_listing(operator, _ROLES_GOOD)["reviewer"]
    assert (role["name"], role["model"], role["skills"], role["tools"],
            role["hooks"]) == ("Chief Reviewer", None, ["read", "write"],
                               ["shell", "web"], ["guard", "audit"])

    added = operator.post(f"/api/roles/{rid}/skills", headers=_ROLES_GOOD,
                          json={"skill": "plan"})
    assert added.status_code == 200, added.text
    assert added.json()["changed"] is True
    assert _roles_in_the_listing(operator, _ROLES_GOOD)["reviewer"]["skills"] == [
        "read", "write", "plan"]

    again = operator.post(f"/api/roles/{rid}/skills", headers=_ROLES_GOOD,
                          json={"skill": "plan"})
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False
    assert _roles_in_the_listing(operator, _ROLES_GOOD)["reviewer"]["skills"] == [
        "read", "write", "plan"]

    removed = operator.delete(f"/api/roles/{rid}/skills/write", headers=_ROLES_GOOD)
    assert removed.status_code == 200, removed.text
    assert removed.json()["changed"] is True
    role = _roles_in_the_listing(operator, _ROLES_GOOD)["reviewer"]
    assert role["skills"] == ["read", "plan"]
    # One skill moved; every other grant stayed where the whole-list edit put it.
    assert (role["name"], role["tools"], role["hooks"]) == (
        "Chief Reviewer", ["shell", "web"], ["guard", "audit"])

    absent = operator.delete(f"/api/roles/{rid}/skills/write", headers=_ROLES_GOOD)
    assert absent.status_code == 200 and absent.json()["changed"] is False

    gone = operator.delete(f"/api/roles/{rid}", headers=_ROLES_GOOD)
    assert gone.status_code == 200, gone.text
    assert "reviewer" not in _roles_in_the_listing(operator, _ROLES_GOOD)

    catalog = operator.get("/api/roles/catalog", headers=_ROLES_GOOD)
    assert catalog.status_code == 200, catalog.text


def test_a_skill_added_or_removed_on_a_role_that_does_not_exist_is_a_404(
        builtin_console, monkeypatch):
    monkeypatch.setenv("ROLES_PUSH_TOKEN", "an-operator-token")
    operator = builtin_console.client()

    added = operator.post("/api/roles/987654/skills", headers=_ROLES_GOOD,
                          json={"skill": "plan"})
    removed = operator.delete("/api/roles/987654/skills/plan", headers=_ROLES_GOOD)

    assert (added.status_code, removed.status_code) == (404, 404), (added.text, removed.text)


def test_a_wrong_or_absent_roles_bearer_changes_no_role_on_any_roles_route(
        builtin_console, monkeypatch):
    """A role changes what every mind holding it may use, so the credential
    is the guard and not merely the thing that happened to be in the header.
    Each route is refused without it and the role is as it was."""
    monkeypatch.setenv("ROLES_PUSH_TOKEN", "an-operator-token")
    operator = builtin_console.client()
    rid = operator.post("/api/roles", headers=_ROLES_GOOD,
                        json={"name": "Held", "skills": ["read"]}).json()["id"]
    anonymous = builtin_console.client()

    calls = (
        ("get", "/api/roles", None),
        ("get", "/api/roles/catalog", None),
        ("post", "/api/roles", {"name": "Intruder"}),
        ("put", f"/api/roles/{rid}", {"name": "Taken", "skills": []}),
        ("post", f"/api/roles/{rid}/skills", {"skill": "evil"}),
        ("delete", f"/api/roles/{rid}/skills/read", None),
        ("post", f"/api/roles/{rid}/keep", None),
        ("post", f"/api/roles/{rid}/promote", None),
        ("post", "/api/roles/repo/anything/take", None),
        ("delete", f"/api/roles/{rid}", None),
    )
    for headers in (_ROLES_WRONG, {}):
        for method, path, body in calls:
            refused = getattr(anonymous, method)(
                path, headers=headers, follow_redirects=False,
                **({"json": body} if body is not None else {}))
            assert refused.status_code in (303, 401, 403), (method, path, refused.text)

    survivors = _roles_in_the_listing(operator, _ROLES_GOOD)
    assert (survivors["held"]["name"], survivors["held"]["skills"]) == ("Held", ["read"])
    assert "intruder" not in survivors


def test_an_install_that_issued_no_roles_token_admits_no_bearer_to_the_roles_routes(
        builtin_console, monkeypatch):
    """An unset token is not an open door, and an empty one is not matched by
    an empty header value, nor by a house token."""
    monkeypatch.delenv("ROLES_PUSH_TOKEN", raising=False)
    monkeypatch.setenv("BOARD_PUSH_TOKEN", "a-board-token")
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "a-house-token")
    anonymous = builtin_console.client()

    for token in ("anything", "a-board-token", "a-house-token", ""):
        for method, path, body in (
            ("get", "/api/roles", None),
            ("post", "/api/roles", {"name": "Intruder"}),
            ("post", "/api/roles/1/skills", {"skill": "x"}),
        ):
            refused = getattr(anonymous, method)(
                path, headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
                **({"json": body} if body is not None else {}))
            assert refused.status_code in (303, 401, 403), (token, path, refused.text)


def test_the_roles_bearer_is_not_a_way_into_the_console_or_the_board(
        builtin_console, monkeypatch):
    monkeypatch.setenv("ROLES_PUSH_TOKEN", "an-operator-token")
    operator = builtin_console.client()

    for path in ("/api/memory/tables", "/api/kanban/data", "/api/admin/users"):
        refused = operator.get(path, headers=_ROLES_GOOD, follow_redirects=False)
        assert refused.status_code in (303, 401, 403), (path, refused.text)


def test_a_signed_in_admin_still_manages_roles_and_a_signed_in_person_still_only_reads_them(
        people, monkeypatch):
    """The bearer is an added door; the session's abilities are as they were."""
    monkeypatch.setenv("ROLES_PUSH_TOKEN", "an-operator-token")
    admin, sloan = people

    made = admin.post("/api/roles", json={"name": "By Hand", "skills": ["a"]})
    assert made.status_code == 200, made.text
    rid = made.json()["id"]
    assert admin.post(f"/api/roles/{rid}/skills", json={"skill": "b"}).status_code == 200
    assert _roles_in_the_listing(admin, {})["by-hand"]["skills"] == ["a", "b"]

    assert sloan.get("/api/roles").status_code == 200
    for method, path, body in (
        ("post", "/api/roles", {"name": "Nope"}),
        ("put", f"/api/roles/{rid}", {"skills": []}),
        ("post", f"/api/roles/{rid}/skills", {"skill": "c"}),
        ("delete", f"/api/roles/{rid}/skills/a", None),
        ("delete", f"/api/roles/{rid}", None),
    ):
        refused = getattr(sloan, method)(
            path, follow_redirects=False,
            **({"json": body} if body is not None else {}))
        assert refused.status_code in (303, 401, 403), (path, refused.text)
    assert _roles_in_the_listing(admin, {})["by-hand"]["skills"] == ["a", "b"]


def test_skills_added_to_one_role_at_once_all_survive(
        builtin_console, monkeypatch):
    """Without the role's lock the second add reads the list before the first
    wrote, and one skill is lost with both calls reporting success. Many adds
    released together make that loss all but certain."""
    import threading

    monkeypatch.setenv("ROLES_PUSH_TOKEN", "an-operator-token")
    operator = builtin_console.client()
    rid = operator.post("/api/roles", headers=_ROLES_GOOD,
                        json={"name": "Busy"}).json()["id"]
    names = [f"skill-{n}" for n in range(12)]
    gate = threading.Barrier(len(names))
    answers: dict[str, int] = {}

    def add(name):
        client = builtin_console.client()
        gate.wait()
        answers[name] = client.post(f"/api/roles/{rid}/skills", headers=_ROLES_GOOD,
                                    json={"skill": name}).status_code

    threads = [threading.Thread(target=add, args=(n,)) for n in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert answers == {n: 200 for n in names}
    held = _roles_in_the_listing(operator, _ROLES_GOOD)["busy"]["skills"]
    assert sorted(held) == sorted(names), held


def test_a_whole_list_replace_of_a_role_waits_for_whoever_holds_that_roles_lock(
        builtin_console, monkeypatch):
    """The PUT and the one-skill add take the same lock, so a replace cannot
    read between an add's read and its write and erase it. Another unit holds
    the role's lock for a while; the replace must not land before it lets go.
    """
    import threading
    import time

    import asyncio
    import main
    import store
    from hive_site import testing

    monkeypatch.setenv("ROLES_PUSH_TOKEN", "an-operator-token")
    operator = builtin_console.client()
    rid = operator.post("/api/roles", headers=_ROLES_GOOD,
                        json={"name": "Contended", "skills": ["a"]}).json()["id"]
    holding = threading.Event()
    released_at: list[float] = []

    async def hold_the_lock():
        async with store.transaction(hold=main._role_lock(rid)):
            holding.set()
            await asyncio.sleep(1.0)
            released_at.append(time.monotonic())

    holder = testing.suite_portal().start_task_soon(hold_the_lock)
    assert holding.wait(10)
    replaced = operator.put(f"/api/roles/{rid}", headers=_ROLES_GOOD,
                            json={"skills": ["b"]})
    landed_at = time.monotonic()
    holder.result(10)

    assert replaced.status_code == 200, replaced.text
    assert landed_at >= released_at[0]
    assert _roles_in_the_listing(operator, _ROLES_GOOD)["contended"]["skills"] == ["b"]
