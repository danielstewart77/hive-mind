"""Observer event-stream heartbeat.

Sessions can sit silent for minutes between turns; proxies in front of
web surfaces (Cloudflare idles out around 100s) sever quiet SSE
connections, and every event published before the observer reconnects
is lost. stream_session_events must emit a ping when idle so the
connection stays warm.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

from comms.models import ModelRegistry, Provider
from comms.sessions import SessionManager


def _registry() -> ModelRegistry:
    return ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    return mgr


async def _seed_session(mgr: SessionManager, status: str = "running") -> str:
    session_id = "sess-hb"
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active,
                                 status, mind_id)
           VALUES (?, 'telegram', '123', 'opus', ?, ?, ?, 'ada')""",
        (session_id, now, now, status),
    )
    await mgr._db.commit()
    return session_id


def test_idle_stream_emits_ping() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            mgr.EVENT_HEARTBEAT_SECONDS = 0.05
            try:
                sid = await _seed_session(mgr)
                stream = mgr.stream_session_events(sid)
                event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
                assert event == {"type": "ping", "session_id": sid}
                await stream.aclose()
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())


def test_published_event_still_delivered_between_pings() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            mgr.EVENT_HEARTBEAT_SECONDS = 30.0
            try:
                sid = await _seed_session(mgr)
                stream = mgr.stream_session_events(sid)

                async def publish_soon() -> None:
                    # Give the stream a beat to subscribe first.
                    await asyncio.sleep(0.05)
                    await mgr._publish_session_event(
                        sid, {"type": "assistant", "text": "hello"}
                    )

                task = asyncio.ensure_future(publish_soon())
                event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
                await task
                assert event["type"] == "assistant"
                await stream.aclose()
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())


def test_closed_session_yields_session_closed_immediately() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            try:
                sid = await _seed_session(mgr, status="closed")
                stream = mgr.stream_session_events(sid)
                event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
                assert event == {"type": "session_closed", "session_id": sid}
            finally:
                await mgr.shutdown()

    asyncio.run(scenario())
