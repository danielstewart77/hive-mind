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
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from comms.sessions import MindRefusedCredential
from comms_pg_fixture import (  # noqa: F401 - fixtures
    comms_db,
    fetchval,
    scratch_dsn,
    seed_session,
)

MIND_A = "11111111-1111-4111-8111-111111111111"


#: These tests are about their own subject, not about authorization, so they
#: present the admin credential. They used to clear every token instead and
#: rely on the gateway serving open when unconfigured — a bypass that is gone,
#: because a gateway that cannot establish who is calling must refuse rather
#: than answer the LAN.
GRILL_ADMIN = "admin-bearer"  # secret-guard: allow


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def app_client(monkeypatch, comms_db):  # noqa: F811 - the imported fixture
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as client:
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
            "harness": "claude_cli", "owner": "daniel",
            "session_token": "alpha-token",  # secret-guard: allow
        },
    )
    assert response.status_code == 200

    stored = _run(
        fetchval("SELECT session_token FROM core.minds WHERE mind_id = $1", MIND_A)
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
            "harness": "claude_cli", "owner": "daniel",
            "session_token": "alpha-token",  # secret-guard: allow
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
            "harness": "claude_cli", "owner": "daniel",
            "session_token": "alpha-token",  # secret-guard: allow
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
    # On the app's own loop: the pool the lifespan opened lives there.
    client.portal.call(lambda: seed_session(
        "sess-sw", owner_type="telegram", owner_ref="123", model="sonnet",
        mind_id=MIND_A, summary="chat", claude_sid="conv-1",
    ))

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
