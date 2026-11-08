# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
"""A NUL character is stored, not refused.

SQLite stored one; a Postgres `text` column refuses it, and the refusal rolls
the whole unit back. Conversation text and inter-mind messages carry raw tool
output, which can contain one, so every comms text write turns it into the
replacement character rather than failing.
"""
from __future__ import annotations

import asyncio

import pytest

from comms import broker
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_store,
    fetchval,
    scratch_dsn,
)
from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    execute,
    make_manager,
    seed_session,
)


@pytest.mark.asyncio
async def test_a_message_carrying_a_nul_is_stored(broker_store) -> None:
    stored = await broker.insert_message(
        message_id="nul-1", conversation_id="conv-nul", from_mind="ada",
        to_mind="bob", content="before\x00after", rolling_summary="",
        metadata=None, status="pending")

    assert stored["existing"] is False
    assert await fetchval(
        "SELECT content FROM core.messages WHERE id = 'nul-1'"
    ) == "before�after"


def test_a_terminal_turn_carrying_a_nul_lands_in_the_ledger(comms_db) -> None:
    async def scenario():
        mgr = await make_manager()
        try:
            await seed_session(
                "sess-nul", owner_type="web", owner_ref="terminal", model="opus",
                claude_sid="conv-nul", status="running", mind_id="ada",
                summary="x")
            await execute(
                "INSERT INTO core.active_sessions (client_type, client_ref, session_id) "
                "VALUES ('web', 'tile-1', 'sess-nul')")
            answer = await mgr.record_turn("web", "tile-1", "user", "ls\x00out")
            turns = await mgr.get_session_turns("sess-nul")
        finally:
            await close_manager(mgr)
        return answer, turns

    answer, turns = asyncio.run(scenario())
    assert answer.get("ok") is not False
    assert [t["content"] for t in turns] == ["ls�out"]
