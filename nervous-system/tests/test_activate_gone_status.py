"""A conversation with nowhere to go says which kind of nowhere, over HTTP.

A chat rotation retires the row it came from and opens a successor carrying
the carry-forward. Every client holding the old id then asks for something
that cannot be given — and asking used to produce a bare 500, because the
manager raised a plain `ValueError` and the route caught nothing. A 500 reads
as a broken gateway, so the health app retried a dead id for a month against
a conversation that was alive under a new one, reporting a coach who said
nothing and replies that would not send.

404 for an id with no row, 410 for one that has been retired.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from comms import sessions as sessions_module
from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    if mgr._dashboard_sweep_task:
        mgr._dashboard_sweep_task.cancel()
        mgr._dashboard_sweep_task = None
    return mgr


async def _seed(mgr: SessionManager, session_id: str, status: str) -> None:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions
           (id, owner_type, owner_ref, model, claude_sid, summary, created_at, last_active, status, mind_id)
           VALUES (?, 'health', 'daniel', 'opus', ?, 'New session', ?, ?, ?, 'skippy-uuid')""",
        (session_id, f"conv-{session_id}", now - 60, now, status),
    )
    await mgr._db.commit()


def test_activating_a_retired_session_raises_the_closed_type():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                await _seed(mgr, "rotated-away", "closed")
                with pytest.raises(sessions_module.SessionClosed):
                    await mgr.activate_session("rotated-away", "health", "daniel")
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_activating_an_unknown_session_raises_the_not_found_type():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            try:
                with pytest.raises(sessions_module.SessionNotFound):
                    await mgr.activate_session("never-existed", "health", "daniel")
            finally:
                await mgr.shutdown()

    _run(scenario())


def test_both_types_stay_value_errors_for_callers_that_catch_one():
    # Every existing `except ValueError` around activation keeps working.
    assert issubclass(sessions_module.SessionClosed, ValueError)
    assert issubclass(sessions_module.SessionNotFound, ValueError)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as http:
        yield http, server_module


def _activate(http, server, exc):
    with patch.object(server.session_mgr, "activate_session", AsyncMock(side_effect=exc)):
        return http.post(
            "/sessions/sess-1/activate",
            json={"client_type": "health", "client_ref": "health",
                  "owner_type": "health", "owner_ref": "daniel"},
        )


def test_a_retired_session_answers_410(client):
    http, server = client
    response = _activate(http, server, sessions_module.SessionClosed("Session sess-1 is closed"))
    assert response.status_code == 410
    assert "closed" in response.json()["error"]


def test_an_unknown_session_answers_404(client):
    http, server = client
    response = _activate(http, server, sessions_module.SessionNotFound("Session not found: sess-1"))
    assert response.status_code == 404


def test_a_refused_release_still_answers_502(client):
    # A mind that would not let go is a statement about a machine, not about
    # this row — the remedy for one is wasted on the other.
    http, server = client
    response = _activate(http, server, sessions_module.MindCallFailed("mind unreachable"))
    assert response.status_code == 502


def test_any_other_activation_refusal_answers_409_not_500(client):
    http, server = client
    response = _activate(http, server, ValueError("something else entirely"))
    assert response.status_code == 409


def test_reading_an_unknown_session_answers_404(client):
    """The route returned `(body, 404)` — a tuple FastAPI served as a 200 array."""
    http, server = client
    with patch.object(server.session_mgr, "get_session", AsyncMock(return_value=None)):
        response = http.get("/sessions/never-existed")
    assert response.status_code == 404
    assert response.json() == {"error": "Session not found"}
