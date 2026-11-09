"""Observer event-stream heartbeat.

Sessions can sit silent for minutes between turns; proxies in front of
web surfaces (Cloudflare idles out around 100s) sever quiet SSE
connections, and every event published before the observer reconnects
is lost. stream_session_events must emit a ping when idle so the
connection stays warm.
"""
from __future__ import annotations

import asyncio

from comms.sessions import SessionManager
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    make_manager,
    scratch_dsn,
    seed_session,
)



async def _make_manager() -> SessionManager:
    mgr = await make_manager()
    return mgr


async def _seed_session(mgr: SessionManager, status: str = "running") -> str:
    session_id = "sess-hb"
    await seed_session(
        session_id, surface="telegram", surface_id="123", model="opus",
        status=status, mind_id="ada",
    )
    return session_id


def test_idle_stream_emits_ping(comms_db) -> None:
    async def scenario() -> None:
        mgr = await _make_manager()
        mgr.EVENT_HEARTBEAT_SECONDS = 0.05
        try:
            sid = await _seed_session(mgr)
            stream = mgr.stream_session_events(sid)
            event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
            assert event == {"type": "ping", "session_id": sid}
            await stream.aclose()
        finally:
            await close_manager(mgr)

    asyncio.run(scenario())


def test_published_event_still_delivered_between_pings(comms_db) -> None:
    async def scenario() -> None:
        mgr = await _make_manager()
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
            await close_manager(mgr)

    asyncio.run(scenario())


def test_closed_session_yields_session_closed_immediately(comms_db) -> None:
    async def scenario() -> None:
        mgr = await _make_manager()
        try:
            sid = await _seed_session(mgr, status="closed")
            stream = mgr.stream_session_events(sid)
            event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
            assert event == {"type": "session_closed", "session_id": sid}
        finally:
            await close_manager(mgr)

    asyncio.run(scenario())
