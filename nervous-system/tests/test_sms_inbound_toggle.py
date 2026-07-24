"""Tests for the SMS inbound toggle (broker settings + endpoint behavior).

The toggle is a single boolean keyed `sms_inbound_enabled` in the broker
`settings` table. Default = off (no row -> "false"). When off, `/sms/inbound`
returns 200 `{"status": "disabled"}` and skips HMAC verification, broker
insertion, and dispatch entirely. When on, the route behaves as before.

These tests exercise the broker helpers directly and the FastAPI route via
TestClient. They don't spin up a real session manager — they just verify the
short-circuit path skips broker work when the flag is off.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from comms import broker


def _run(coro):
    return asyncio.run(coro)


# --- broker.get_setting / set_setting --------------------------------------


def test_get_setting_returns_default_when_missing() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                assert await broker.get_setting(db, "missing") is None
                assert (
                    await broker.get_setting(db, "missing", default="x") == "x"
                )
            finally:
                await db.close()

    _run(scenario())


def test_set_setting_upserts() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = await broker.init_db(os.path.join(tmp, "broker.db"))
            try:
                await broker.set_setting(db, "k", "v1")
                assert await broker.get_setting(db, "k") == "v1"
                await broker.set_setting(db, "k", "v2")
                assert await broker.get_setting(db, "k") == "v2"
            finally:
                await db.close()

    _run(scenario())


# --- /sms/inbound/enabled endpoints + /sms/inbound short-circuit ------------


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    """Boot the comms FastAPI app with an isolated broker DB.

    We monkey-patch BROKER_DB_PATH before importing the app, disable bearer
    auth by leaving the token unset, and skip the session-manager loop hooks
    that aren't relevant here.
    """
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("SMS_INBOUND_HMAC_SECRET", "test-secret")

    # Import the app fresh so lifespan picks up the env vars above.
    import importlib

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield client, server_module


def test_get_enabled_defaults_false(app_client) -> None:
    client, _ = app_client
    r = client.get("/sms/inbound/enabled")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_put_enabled_round_trip(app_client) -> None:
    client, _ = app_client
    r = client.put("/sms/inbound/enabled", json={"enabled": True})
    assert r.status_code == 200
    assert r.json() == {"enabled": True}

    r = client.get("/sms/inbound/enabled")
    assert r.json() == {"enabled": True}

    r = client.put("/sms/inbound/enabled", json={"enabled": False})
    assert r.json() == {"enabled": False}


def test_put_enabled_rejects_bad_body(app_client) -> None:
    client, _ = app_client
    r = client.put("/sms/inbound/enabled", json={})
    assert r.status_code == 400

    r = client.put("/sms/inbound/enabled", json={"enabled": "yes"})
    assert r.status_code == 400


def test_inbound_route_short_circuits_when_disabled(app_client) -> None:
    """When the flag is off (default), /sms/inbound returns 200 disabled
    without verifying HMAC or hitting the broker."""
    client, _ = app_client

    # No signature headers, no HMAC body — would normally 401. Should pass
    # because the route short-circuits before signature verification.
    r = client.post("/sms/inbound", content=b"{}")
    assert r.status_code == 200
    assert r.json() == {"status": "disabled"}


def test_inbound_route_runs_when_enabled_but_no_secret(app_client, monkeypatch) -> None:
    """When the flag is on, the inbound route resumes its normal flow —
    the next gate is HMAC. With a missing/invalid signature, expect 401."""
    client, server_module = app_client

    client.put("/sms/inbound/enabled", json={"enabled": True})

    r = client.post("/sms/inbound", content=b"{}")
    # No HMAC headers -> 401 from the verification step.
    assert r.status_code == 401


def _sign(body: bytes, timestamp: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(body)
    mac.update(timestamp.encode())
    return mac.hexdigest()


def test_inbound_route_dispatches_when_enabled_with_valid_signature(
    app_client,
) -> None:
    """Sanity: with the flag on, a properly-signed body proceeds past
    HMAC verification. We expect it to reach the 'ada not registered'
    branch since we haven't seeded a mind row, returning 200 with
    ack-no-recipient — proving the short-circuit is gone."""
    client, _ = app_client

    client.put("/sms/inbound/enabled", json={"enabled": True})

    body = json.dumps({
        "event": "sms:received",
        "payload": {
            "phoneNumber": "+15551234567",
            "message": "hi",
            "messageId": "abc-123",
        },
    }).encode()
    ts = "1700000000"
    sig = _sign(body, ts, "test-secret")

    r = client.post(
        "/sms/inbound",
        content=body,
        headers={
            "x-signature": sig,
            "x-timestamp": ts,
            "content-type": "application/json",
        },
    )
    assert r.status_code == 200
    # No ada in the test broker -> ack-no-recipient.
    assert r.json() == {"status": "ack-no-recipient"}
