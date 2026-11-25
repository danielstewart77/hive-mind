"""The shared account suite, run through the terminal: both stores.

The `terminal` fixture runs each test twice — once on the built-in hive.db
store, once signed in through the real auth-service — and the two must
agree. The console runs the same checks through its own app; this is what
keeps the two copies of the store honest against each other.
"""

import re

import pytest

from hive_site import testing as accounts
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

import main
import route_access
import terminal_access


def _socket(client, path):
    """A websocket from this client. Its cookies are passed by hand: they
    are Secure, and the test client's jar withholds Secure cookies from a
    `wss://testserver` URL that a browser would send them to."""
    cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    return client.websocket_connect(path, headers={"cookie": cookie})


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}:]+(?::[^}]+)?\}", "x", path)


def _authenticated_routes():
    """(method, path, template) for every route a signed-in user needs (not
    the public ones, the password page, or a route only a bearer reaches)."""
    for route in main.app.routes:
        if isinstance(route, Mount):
            continue
        for method in sorted(getattr(route, "methods", None) or {"WS"}):
            if method == "HEAD":
                continue
            rule = terminal_access.TABLE.rule_for(method, route.path)
            if rule.kind in (route_access.PUBLIC, route_access.SELF, route_access.BEARER):
                continue
            yield method, _concrete(route.path), route.path


def test_a_must_change_user_is_sent_to_change_password_on_every_terminal_route_and_the_old_password_stops_working(terminal):
    terminal.create_user("sloan", "temporary-pass-1")

    client, signed_in = terminal.sign_in("sloan", "temporary-pass-1")
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/password"

    walked = set()
    for method, path, template in _authenticated_routes():
        walked.add((method, template))
        if method == "WS":
            with pytest.raises(WebSocketDisconnect) as closed, _socket(client, path) as ws:
                ws.receive_bytes()
            assert closed.value.code == route_access.CLOSE_MUST_CHANGE_PASSWORD, template
            continue
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303, (method, template, response.status_code)
        assert response.headers["location"] == "/password", (method, template)
    assert ("WS", "/api/terminal/attach/{session_id}") in walked
    assert ("GET", "/terminal") in walked

    assert client.get("/password").status_code == 200
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
    assert client.get("/terminal", follow_redirects=False).status_code == 200

    _, old = terminal.sign_in("sloan", "temporary-pass-1")
    assert old.status_code == 401
    _, new = terminal.sign_in("sloan", "sloans-own-pass")
    assert new.status_code == 303
    assert new.headers["location"] == "/terminal"


def test_a_disabled_user_is_refused_at_terminal_sign_in_and_turned_away_mid_session(terminal):
    sloan = terminal.settled_user("sloan", "sloans-own-pass")
    assert sloan.get("/terminal", follow_redirects=False).status_code == 200

    accounts.run(terminal.store().update_user("sloan", disabled=True))

    page = sloan.get("/terminal", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")
    assert sloan.get("/api/minds").status_code == 401
    with pytest.raises(WebSocketDisconnect) as closed, _socket(
        sloan, "/api/terminal/attach/s-arnold"
    ) as ws:
        ws.receive_bytes()
    assert closed.value.code == route_access.CLOSE_UNAUTHENTICATED

    _, again = terminal.sign_in("sloan", "sloans-own-pass")
    assert again.status_code == 403
    assert "disabled" in again.text
