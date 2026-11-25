"""The shared account suite: every test here runs against both stores.

The `console` fixture runs each test twice — once with the console's own
hive.db store, once signed in through the real auth-service — and the two
must agree. Every step goes through the console's own HTTP surface, so what
is exercised is the store, the route table in front of the routes, and the
pages, together.
"""

import re

import pytest
from multiuser_support import SEED_PASSWORD, SEED_USERNAME
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

import console_access
import main
import route_access


def _socket(client, path):
    """A websocket from this client. Its cookies are passed by hand: they
    are Secure, and the test client's jar withholds Secure cookies from a
    `wss://testserver` URL that a browser would send them to."""
    cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    return client.websocket_connect(path, headers={"cookie": cookie})



def _concrete(path: str) -> str:
    """A route template with every parameter filled by a placeholder."""
    return re.sub(r"\{[^}:]+(?::[^}]+)?\}", "x", path)


def _authenticated_routes():
    """(method, path, template) for every route a signed-in user needs (not
    the public ones, the password page, or a route only a bearer reaches)."""
    for route in main.app.routes:
        if isinstance(route, Mount):
            continue
        template = route.path
        methods = sorted(getattr(route, "methods", None) or {"WS"})
        for method in methods:
            if method == "HEAD":
                continue
            rule = console_access.TABLE.rule_for(method, template)
            if rule.kind in (route_access.PUBLIC, route_access.SELF, route_access.BEARER):
                continue
            yield method, _concrete(template), template


def test_a_must_change_user_is_sent_to_change_password_on_every_route_and_the_old_password_stops_working(console):
    admin = console.admin()
    console.create_user(admin, "sloan", "temporary-pass-1")

    client, signed_in = console.sign_in("sloan", "temporary-pass-1")
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/password"

    walked = 0
    for method, path, template in _authenticated_routes():
        walked += 1
        if method == "WS":
            with pytest.raises(WebSocketDisconnect) as closed, _socket(client, path) as ws:
                ws.receive_json()
            assert closed.value.code == route_access.CLOSE_MUST_CHANGE_PASSWORD, template
            continue
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303, (method, template, response.status_code)
        assert response.headers["location"] == "/password", (method, template)
    assert walked > 100

    page = client.get("/password")
    assert page.status_code == 200
    changed = client.post(
        "/password",
        data={
            "current_password": "temporary-pass-1",
            "new_password": "sloans-own-pass",
            "confirm_password": "sloans-own-pass",
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert client.get("/overview", follow_redirects=False).status_code == 200

    _, old = console.sign_in("sloan", "temporary-pass-1")
    assert old.status_code == 401
    _, new = console.sign_in("sloan", "sloans-own-pass")
    assert new.status_code == 303
    assert new.headers["location"] == "/overview"


def test_a_disabled_user_is_refused_at_sign_in_and_turned_away_mid_session(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloans-own-pass")
    assert sloan.get("/overview", follow_redirects=False).status_code == 200

    disabled = admin.patch("/api/admin/users/sloan", json={"disabled": True})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["disabled"] is True

    page = sloan.get("/overview", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")
    assert sloan.get("/api/settings/minds").status_code == 401

    _, again = console.sign_in("sloan", "sloans-own-pass")
    assert again.status_code == 403
    assert "disabled" in again.text


def test_an_admin_reset_issues_a_temporary_password_that_must_be_changed_again(console):
    admin = console.admin()
    console.settled_user(admin, "sloan", "sloans-own-pass")

    reset = admin.post(
        "/api/admin/users/sloan/reset", json={"temp_password": "reset-temp-pass"}
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["must_change"] is True

    _, old = console.sign_in("sloan", "sloans-own-pass")
    assert old.status_code == 401
    client, temp = console.sign_in("sloan", "reset-temp-pass")
    assert temp.status_code == 303
    assert temp.headers["location"] == "/password"
    held = client.get("/overview", follow_redirects=False)
    assert held.headers["location"] == "/password"


def test_a_password_reset_ends_the_users_other_live_session(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloans-own-pass")
    assert sloan.get("/overview", follow_redirects=False).status_code == 200

    admin.post("/api/admin/users/sloan/reset", json={"temp_password": "reset-temp-pass"})

    ended = sloan.get("/overview", follow_redirects=False)
    assert ended.status_code == 303
    assert ended.headers["location"].startswith("/login")


def _user_and_group_actions():
    return (
        ("GET", "/api/admin/users", None),
        ("POST", "/api/admin/users", {"username": "zed", "temp_password": "zed-temp-pass"}),
        ("PATCH", "/api/admin/users/aiden", {"email": "aiden@example.com"}),
        ("POST", "/api/admin/users/aiden/reset", {"temp_password": "aiden-reset-pass"}),
        ("GET", "/api/admin/groups", None),
        ("POST", "/api/admin/groups", {"name": "kids"}),
        ("PUT", "/api/admin/groups/kids/members/aiden", None),
        ("DELETE", "/api/admin/groups/kids/members/aiden", None),
        ("PATCH", "/api/admin/users/aiden", {"disabled": True}),
    )


def test_a_non_admin_is_refused_every_user_and_group_action_and_an_admin_succeeds(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloans-own-pass")
    console.create_user(admin, "aiden", "aiden-temp-pass")

    for method, path, body in _user_and_group_actions():
        refused = sloan.request(method, path, json=body)
        assert refused.status_code == 403, (method, path, refused.status_code)

    assert sloan.get("/settings/users", follow_redirects=False).status_code == 403

    for method, path, body in _user_and_group_actions():
        done = admin.request(method, path, json=body)
        assert 200 <= done.status_code < 300, (method, path, done.status_code, done.text)
    assert admin.get("/settings/users").status_code == 200


def test_membership_of_admins_grants_and_revokes_admin_on_the_next_request(console):
    admin = console.admin()
    sloan = console.settled_user(admin, "sloan", "sloans-own-pass")
    assert sloan.get("/api/admin/users").status_code == 403

    added = admin.put("/api/admin/groups/admins/members/sloan")
    assert added.status_code == 204, added.text
    assert sloan.get("/api/admin/users").status_code == 200

    removed = admin.delete("/api/admin/groups/admins/members/sloan")
    assert removed.status_code == 204, removed.text
    assert sloan.get("/api/admin/users").status_code == 403


def test_the_last_enabled_admin_cannot_be_disabled_or_removed_from_admins(console):
    admin = console.admin()

    assert admin.patch(f"/api/admin/users/{SEED_USERNAME}", json={"disabled": True}).status_code == 409
    assert admin.delete(f"/api/admin/groups/admins/members/{SEED_USERNAME}").status_code == 409

    # A second admin who is disabled does not count.
    console.create_user(admin, "aiden", "aiden-temp-pass")
    assert admin.put("/api/admin/groups/admins/members/aiden").status_code == 204
    assert admin.patch("/api/admin/users/aiden", json={"disabled": True}).status_code == 200

    assert admin.patch(f"/api/admin/users/{SEED_USERNAME}", json={"disabled": True}).status_code == 409
    assert admin.delete(f"/api/admin/groups/admins/members/{SEED_USERNAME}").status_code == 409

    # The seeded admin still works: the refusals changed nothing.
    _, again = console.sign_in(SEED_USERNAME, SEED_PASSWORD)
    assert again.status_code == 303
