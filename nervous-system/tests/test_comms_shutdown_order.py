"""The gateway's teardown keeps the broker DB open until sessions are killed.

The session manager's shutdown authenticates each kill with the mind's
session token, which it reads off the broker row. Closing the broker
connection first makes every kill raise "no active connection", so the
harness processes survive a gateway that reported a clean stop.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_session_shutdown_runs_before_broker_db_is_closed(monkeypatch, tmp_path):
    """lifespan kills live sessions while the broker connection is still usable."""
    from comms import server as comms_server

    order: list[str] = []

    broker_db = MagicMock()
    broker_db.close = AsyncMock(side_effect=lambda: order.append("broker_db.close"))
    # The lifespan creates the credentials table on the way up, so the stand-in
    # has to be awaitable where a real aiosqlite connection is. A bare
    # MagicMock returns a MagicMock, which `await` rejects — and the failure
    # reads as a shutdown-ordering bug rather than a mock that is the wrong
    # shape.
    broker_db.executescript = AsyncMock()
    broker_db.commit = AsyncMock()
    # The migration asks whether the predecessor table is there. `fetchone`
    # answering None is "it is not", which is what a fresh broker looks like.
    _no_rows = MagicMock()
    _no_rows.fetchone = AsyncMock(return_value=None)
    broker_db.execute = AsyncMock(return_value=_no_rows)

    async def fake_init_db(path):
        return broker_db

    async def fake_recover(db):
        return []

    monkeypatch.setattr(comms_server.broker, "init_db", fake_init_db)
    monkeypatch.setattr(comms_server.broker, "recover_stranded_messages", fake_recover)
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))

    async def fake_shutdown():
        # What the real shutdown does: read each mind's credential off the
        # broker row, then call the mind. A closed connection fails here.
        broker_db.execute("SELECT session_token FROM minds WHERE mind_id = ?")
        order.append("session_mgr.shutdown")

    monkeypatch.setattr(comms_server.session_mgr, "start", AsyncMock())
    monkeypatch.setattr(comms_server.session_mgr, "shutdown", fake_shutdown)
    monkeypatch.setattr(
        comms_server.session_mgr, "resume_pending_resends", AsyncMock()
    )

    app = MagicMock()
    async with comms_server.lifespan(app):
        pass
    await asyncio.sleep(0)

    assert order == ["session_mgr.shutdown", "broker_db.close"]
