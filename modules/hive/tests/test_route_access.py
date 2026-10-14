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
    console_access.record_operation("op-sloan", "sloan")
    console_access.record_operation("op-aiden", "aiden")
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
