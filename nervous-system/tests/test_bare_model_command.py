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

MIND = "11111111-1111-1111-1111-111111111111"


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


def _seed_active(mgr, model: str) -> None:
    now = time.time()
    _run(mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id)
           VALUES ('sess-m', 'telegram', '123', ?, 'conv-1', ?, ?, 'running', ?)""",
        (model, now, now, MIND),
    ))
    _run(mgr._db.execute(
        """INSERT INTO active_sessions (client_type, client_ref, session_id)
           VALUES ('telegram', '123', 'sess-m')""",
    ))
    _run(mgr._db.commit())


def _ask(client):
    return client.post("/command", json={
        "content": "/model", "owner_type": "telegram",
        "client_ref": "123", "owner_ref": "123", "mind_id": MIND,
    })


def test_the_listing_carries_the_conversations_own_model(app_client) -> None:
    client, server_module = app_client
    mgr = server_module.session_mgr
    _seed_active(mgr, "claude-sonnet-5-5")

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
