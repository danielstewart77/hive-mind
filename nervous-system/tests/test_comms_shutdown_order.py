"""The gateway's teardown keeps the database open until sessions are killed.

The session manager's shutdown authenticates each kill with the mind's
session token, which it reads off the broker row. Closing the pool first
makes every kill fail, so the harness processes survive a gateway that
reported a clean stop.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_session_shutdown_runs_before_the_database_is_closed(monkeypatch):
    """lifespan kills live sessions while the pool is still open."""
    from comms import server as comms_server

    order: list[str] = []

    # The pool is the transport boundary: stood in for at `hive_data`, so what
    # is asserted is the order the lifespan itself calls things in.
    async def fake_connect(*_a, **_kw):
        order.append("hive_data.connect")

    async def fake_close(*_a, **_kw):
        order.append("hive_data.close")

    async def fake_recover():
        return []

    async def fake_shutdown():
        order.append("session_mgr.shutdown")

    monkeypatch.setattr(comms_server.hive_data, "connect", fake_connect)
    monkeypatch.setattr(comms_server.hive_data, "close", fake_close)
    monkeypatch.setattr(comms_server.broker, "recover_stranded_messages", fake_recover)
    monkeypatch.setattr(comms_server.session_mgr, "start", AsyncMock())
    monkeypatch.setattr(comms_server.session_mgr, "shutdown", fake_shutdown)
    monkeypatch.setattr(
        comms_server.session_mgr, "resume_pending_resends", AsyncMock()
    )

    app = MagicMock()
    async with comms_server.lifespan(app):
        pass
    await asyncio.sleep(0)

    assert order == ["hive_data.connect", "session_mgr.shutdown", "hive_data.close"]
