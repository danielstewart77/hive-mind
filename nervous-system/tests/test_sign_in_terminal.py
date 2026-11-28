"""An open terminal started with a sign-in key is closed when the key ends.

Feature 834, requirement 41. The real gateway application, the real attach
route and gate, over real rows on the scratch Postgres; only the mind's side
of the bridge (a fake socket, as in test_ws_attach.py) and the record's clock
are stood in for. The re-check interval is the gateway's own setting, set small
so the test does not wait a real half minute.
"""
from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from hive_data import access, core
from comms import user_access
from comms_pg_fixture import comms_db, execute, scratch_dsn  # noqa: F401 - fixtures

from tests.test_ws_attach import (  # noqa: F401 - fixtures and helpers
    GRILL_ADMIN, _FakeHttpSession, _FakeMindWS, _run, _seed_session_and_mind,
    app_client,
)

NOW = 1_800_000_000.0
KEY = "terminal-sign-in-key-0123456789"  # secret-guard: allow


def _record_sign_in(ends_at: float) -> None:
    async def record() -> None:
        # The person is the administrator `app_client` already made.
        await execute("DELETE FROM core.credentials WHERE holder = 'sign-in-t'")
        await execute(
            "INSERT INTO core.credentials (holder, username, role, token_sha, "
            "created_at, ends_at) VALUES ('sign-in-t', 'root-admin', 'user', $1, $2, $3)",
            access.digest(KEY), NOW, ends_at)
    _run(record())


def _open(app_client, monkeypatch, clock):
    client, server_module = app_client
    monkeypatch.setattr(core, "now", lambda: clock["t"])
    monkeypatch.setenv("COMMS_SIGN_IN_RECHECK_SECONDS", "0.05")
    _run(_seed_session_and_mind(server_module))
    _FakeHttpSession.ws_to_return = _FakeMindWS(incoming=[b"terminal up\r\n"])
    _FakeHttpSession.raise_on_connect = None
    monkeypatch.setattr(server_module.aiohttp, "ClientSession", _FakeHttpSession)
    return client


def test_a_terminal_opened_with_a_sign_in_key_is_closed_when_the_key_ends(
        app_client, monkeypatch):
    clock = {"t": NOW}
    client = _open(app_client, monkeypatch, clock)
    _record_sign_in(NOW + 12 * 3600)

    with client.websocket_connect(
            "/sessions/sess-attach/attach",
            headers={"authorization": f"Bearer {KEY}"}) as ws:
        assert ws.receive_bytes() == b"terminal up\r\n"
        clock["t"] = NOW + 12 * 3600  # the twelve hours run out
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_bytes()

    assert closed.value.code == user_access.WS_DENY_CODE


def test_a_chat_socket_opened_with_a_sign_in_key_is_closed_when_the_key_ends(
        app_client, monkeypatch):
    clock = {"t": NOW}
    client = _open(app_client, monkeypatch, clock)
    _record_sign_in(NOW + 12 * 3600)

    with client.websocket_connect(
            "/sessions/sess-attach/stream",
            headers={"authorization": f"Bearer {KEY}"}) as ws:
        clock["t"] = NOW + 12 * 3600
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == user_access.WS_DENY_CODE


def test_an_events_stream_opened_with_a_sign_in_key_ends_when_the_key_ends(
        app_client, monkeypatch):
    clock = {"t": NOW}
    client = _open(app_client, monkeypatch, clock)
    _record_sign_in(NOW + 12 * 3600)

    with client.stream("GET", "/sessions/sess-attach/events",
                       headers={"authorization": f"Bearer {KEY}"}) as response:
        assert response.status_code == 200
        clock["t"] = NOW + 12 * 3600
        # The stream finishes by itself: iterating returns instead of hanging.
        list(response.iter_lines())


def test_a_sign_in_key_cannot_make_or_cut_off_a_credential_but_the_long_lived_key_can(
        app_client, monkeypatch):
    clock = {"t": NOW}
    client = _open(app_client, monkeypatch, clock)
    _record_sign_in(NOW + 12 * 3600)
    sign_in = {"authorization": f"Bearer {KEY}"}
    body = {"username": "root-admin", "token": "a-key-that-never-ends-0123456789",  # secret-guard: allow
            "role": "user"}

    assert client.put("/credentials/root-admin", json=body, headers=sign_in).status_code == 403
    assert client.delete("/credentials/root-admin", headers=sign_in).status_code == 403
    assert client.get("/credentials", headers=sign_in).status_code == 200
    # The administrator's long-lived key (the client's default) still can.
    assert client.put("/credentials/other-person",
                      json={**body, "username": "other-person"}).status_code == 200
