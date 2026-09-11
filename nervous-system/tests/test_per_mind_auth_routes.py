"""The gateway's own routes, where a refusal becomes a thing somebody reads.

The decisions underneath these are covered in `test_per_mind_gateway_auth.py`.
What is covered here is the layer that carries them outward: the one route by
which the gateway ever learns a mind's credential, and the three that decide
whether a refusal reaches a person as itself or as "Internal server error" and
"this mind offers nothing".
"""
from __future__ import annotations

import asyncio
import importlib
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from comms import broker
from comms.sessions import MindRefusedCredential

MIND_A = "11111111-1111-4111-8111-111111111111"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield client, server_module


# ---------------------------------------------------------------------------
# R4 — the only channel by which the gateway learns a credential
# ---------------------------------------------------------------------------
def test_the_registration_route_persists_the_token(app_client) -> None:
    """`broker.register_mind` storing it is asserted directly elsewhere. This
    is the route, which is the only way a mind ever reaches that function."""
    client, server_module = app_client
    response = client.post(
        "/broker/minds",
        headers={"Authorization": "Bearer admin-bearer"},
        json={
            "mind_id": MIND_A, "name": "alpha",
            "gateway_url": "http://alpha:8420", "model": "sonnet",
            "harness": "claude_cli", "session_token": "alpha-token",  # secret-guard: allow
        },
    )
    assert response.status_code == 200

    stored = _run(
        broker.get_mind_session_token(server_module.session_mgr.broker_db, MIND_A)
    )
    assert stored == "alpha-token"  # secret-guard: allow


def test_the_registration_route_does_not_echo_the_token_back(app_client) -> None:
    client, _ = app_client
    response = client.post(
        "/broker/minds",
        headers={"Authorization": "Bearer admin-bearer"},
        json={
            "mind_id": MIND_A, "name": "alpha",
            "gateway_url": "http://alpha:8420", "model": "sonnet",
            "harness": "claude_cli", "session_token": "alpha-token",  # secret-guard: allow
        },
    )
    assert "session_token" not in response.text


def test_the_minds_listing_does_not_carry_it(app_client) -> None:
    client, _ = app_client
    client.post(
        "/broker/minds",
        headers={"Authorization": "Bearer admin-bearer"},
        json={
            "mind_id": MIND_A, "name": "alpha",
            "gateway_url": "http://alpha:8420", "model": "sonnet",
            "harness": "claude_cli", "session_token": "alpha-token",  # secret-guard: allow
        },
    )
    listing = client.get("/broker/minds")
    assert listing.status_code == 200
    assert "alpha-token" not in listing.text  # secret-guard: allow


# ---------------------------------------------------------------------------
# R11 — the routes that carry a refusal outward
# ---------------------------------------------------------------------------
def _refusing(*_args, **_kwargs):
    raise MindRefusedCredential("mind alpha refused the gateway's credential")


def test_the_models_route_answers_502_rather_than_an_empty_catalog(app_client) -> None:
    """An empty list reaches the console as "this mind offers nothing", which
    is the distinction the picker was built to carry."""
    client, server_module = app_client
    with patch.object(server_module.session_mgr, "mind_models", _refusing):
        response = client.get("/models", params={"mind_id": MIND_A})
    assert response.status_code == 502
    assert "credential" in response.text


def test_the_suspend_route_answers_502_when_a_release_cannot_be_delivered(
    app_client,
) -> None:
    client, server_module = app_client
    with patch.object(server_module.session_mgr, "suspend_session", _refusing):
        response = client.post("/sessions/sess-1/suspend")
    assert response.status_code == 502
    assert "credential" in response.text


def test_a_command_names_the_refusal_rather_than_internal_server_error(
    app_client,
) -> None:
    """Telegram is where this is read. "Internal server error" is the least
    useful sentence available for the one failure designed to be legible."""
    client, server_module = app_client
    mgr = server_module.session_mgr
    now = time.time()
    _run(mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at,
                                 last_active, status, mind_id, summary, claude_sid)
           VALUES ('sess-sw', 'telegram', '123', 'sonnet', ?, ?, 'running', ?,
                   'chat', 'conv-1')""",
        (now, now, MIND_A),
    ))
    _run(mgr._db.commit())

    with patch.object(mgr, "activate_session", _refusing):
        response = client.post(
            "/command",
            json={
                "content": "/switch sess-sw", "owner_type": "telegram",
                "client_ref": "123", "owner_ref": "123", "mind_id": MIND_A,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert "credential" in body.get("error", "")
    assert "Internal server error" not in body.get("error", "")
