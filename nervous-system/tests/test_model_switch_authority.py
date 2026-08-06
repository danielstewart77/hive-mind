"""Which models exist is the proxy's business, asked through the mind.

The gateway used to hold a table mapping three short aliases to a provider,
with everything unrecognised falling through to Ollama — so a real deployment
name was classified as a local model, and a name nothing serves was accepted
outright. It now holds no such table: a mind reports what its own proxy key
can address, and a switch to anything else is refused rather than spawned.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest

from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


async def _seed(mgr: SessionManager, model: str) -> str:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 created_at, last_active, status, mind_id)
           VALUES ('sess-1', 'telegram', '123', ?, 'conv-1', ?, ?, 'running', 'ada')""",
        (model, now, now),
    )
    await mgr._db.commit()
    return "sess-1"


def test_a_model_the_mind_does_not_offer_is_refused():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            with patch.object(
                mgr, "mind_models",
                new=AsyncMock(return_value=[{"name": "claude-opus-5"}]),
            ), patch.object(mgr, "_kill_process", new=AsyncMock()) as killed, \
                    patch.object(mgr, "_spawn", new=AsyncMock()) as spawned:
                with pytest.raises(ValueError):
                    await mgr.switch_model("sess-1", "gpt-5.4")
            # Refused before anything was torn down: the conversation the user
            # is in must survive a mistyped model name.
            assert killed.await_count == 0
            assert spawned.await_count == 0
            row = await mgr._get_row("sess-1")
            assert row["model"] == "claude-opus-5"
            await mgr.shutdown()

    _run(scenario())


def test_a_model_the_mind_offers_is_switched_to():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            with patch.object(
                mgr, "mind_models",
                new=AsyncMock(return_value=[
                    {"name": "claude-opus-5"}, {"name": "claude-sonnet-5"},
                ]),
            ), patch.object(mgr, "_kill_process", new=AsyncMock()), \
                    patch.object(mgr, "_spawn", new=AsyncMock()), \
                    patch.object(mgr, "_routing_for", new=AsyncMock(return_value={})):
                await mgr.switch_model("sess-1", "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            assert row["model"] == "claude-sonnet-5"
            await mgr.shutdown()

    _run(scenario())


def test_an_unreachable_mind_refuses_rather_than_approving_everything():
    """An empty listing must not become a blanket yes."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, "claude-opus-5")
            with patch.object(mgr, "mind_models", new=AsyncMock(return_value=[])), \
                    patch.object(mgr, "_kill_process", new=AsyncMock()), \
                    patch.object(mgr, "_spawn", new=AsyncMock()):
                with pytest.raises(ValueError):
                    await mgr.switch_model("sess-1", "claude-sonnet-5")
            await mgr.shutdown()

    _run(scenario())
