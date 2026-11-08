"""A bare `/model` lists what the mind runs and names where this conversation is.

The listing alone is not enough for a surface drawing a picker: it has no
other way to mark the model the conversation is already on. The mind's
configured default is a different fact and on an edge install it is a
pre-proxy alias (`opus`) that matches no deployment name in the catalog
(`claude-opus-5`), so a picker marking that marked nothing at all.
"""
from __future__ import annotations

import asyncio
import importlib
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from comms_pg_fixture import comms_db, execute, scratch_dsn  # noqa: F401

MIND = "11111111-1111-1111-1111-111111111111"


#: These tests are about their own subject, not about authorization, so they
#: present the admin credential. They used to clear every token instead and
#: rely on the gateway serving open when unconfigured — a bypass that is gone,
#: because a gateway that cannot establish who is calling must refuse rather
#: than answer the LAN.
GRILL_ADMIN = "admin-bearer"  # secret-guard: allow


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def app_client(comms_db, monkeypatch):  # noqa: F811 - the imported fixture
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as client:
        yield client, server_module


def _seed_active(model: str) -> None:
    """Written on a plain connection: the gateway's pool lives on the test
    client's own loop, not on this one."""
    now = time.time()

    async def seed() -> None:
        await execute(
            "INSERT INTO core.minds (mind_id, name, gateway_url, model, harness, "
            "registered_at, last_seen) VALUES ($1, 'bare-model-mind', "
            "'http://mind.invalid:8420', 'opus', 'claude_cli', $2, $2) "
            "ON CONFLICT (mind_id) DO NOTHING", MIND, now,
        )
        await execute(
            "INSERT INTO core.sessions (id, surface, surface_id, model, "
            "conversation_id, created_at, last_active, status, mind_id) "
            "VALUES ('sess-m', 'telegram', '123', $1, 'conv-1', $2, $2, "
            "'running', $3)", model, now, MIND,
        )
        await execute(
            "INSERT INTO core.active_sessions (client_type, client_ref, session_id) "
            "VALUES ('telegram', '123', 'sess-m')"
        )

    _run(seed())


def _ask(client):
    return client.post("/command", json={
        "content": "/model", "owner_type": "telegram",
        "client_ref": "123", "owner_ref": "123", "mind_id": MIND,
    })


def test_the_listing_carries_the_conversations_own_model(app_client) -> None:
    client, server_module = app_client
    mgr = server_module.session_mgr
    _seed_active("claude-sonnet-5-5")

    with patch.object(mgr, "mind_models", new=AsyncMock(return_value=[
        {"name": "claude-opus-5-5"}, {"name": "claude-sonnet-5-5"},
    ])):
        body = _ask(client).json()

    assert [row["name"] for row in body["models"]] == [
        "claude-opus-5-5", "claude-sonnet-5-5",
    ]
    assert body["current"] == "claude-sonnet-5-5"


def test_a_chat_holding_no_conversation_is_told_so_rather_than_listed_at(app_client) -> None:
    """The remedy is `/new`, not "the proxy is down"."""
    client, server_module = app_client

    with patch.object(server_module.session_mgr, "mind_models",
                      new=AsyncMock(return_value=[{"name": "claude-opus-5-5"}])):
        body = _ask(client).json()

    assert "models" not in body
    assert "No active session" in body["error"]
