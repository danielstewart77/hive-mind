"""Deleting a mind says why it cannot, and takes its grants with it when it can.

`core.sessions.mind_id` is ON DELETE RESTRICT: a conversation names the mind
it ran on, and ten of the hive's eleven minds have some. Against SQLite the
delete simply went through and orphaned them; against Postgres it is refused,
and the console has to be told "this mind still has conversations" rather
than handed an integrity error as a 500.

Secret scopes are keyed by the mind's name with nothing cascading to them, so
a mind deleted without clearing them leaves its grants for the next mind
registered under the same name.
"""
from __future__ import annotations

import time

import httpx
import pytest
import pytest_asyncio

from comms import broker
from comms_broker_pg import (  # noqa: F401 - fixtures
    broker_store,
    execute,
    fetchval,
    put_mind,
    scratch_dsn,
)

pytestmark = pytest.mark.asyncio

MIND = "5e1f0000-0000-4000-8000-00000000de1e"
OTHER = "5e1f0000-0000-4000-8000-0000000000aa"
ADMIN = "admin-credential-for-tests"  # secret-guard: allow


async def _with_conversations(mind_id: str, count: int = 2) -> None:
    now = time.time()
    for n in range(count):
        await execute(
            "INSERT INTO core.sessions "
            "(id, surface, surface_id, mind_id, status, created_at, last_active) "
            "VALUES ($1, 'telegram', '1', $2, 'running', $3, $3)",
            f"session-{n}-of-{mind_id}", mind_id, now,
        )


@pytest_asyncio.fixture
async def graph_rows():
    """Removes the graph rows a test seeds with explicit ids. The wipe
    leaves `memory` alone, and rows left behind there put the identity
    sequence behind the table, which the next pool open refuses."""
    yield
    await execute("DELETE FROM memory.edges WHERE id = 9101")
    await execute("DELETE FROM memory.nodes WHERE id IN (9001, 9002)")


async def _named_by_the_memory_graph(mind_id: str) -> None:
    """Two nodes and an edge between them, all stamped with the mind."""
    await execute(
        "INSERT INTO memory.schema_types (name, kind, property_schema, created_at) "
        "VALUES ('Concept', 'first-class', '{}', 0) ON CONFLICT DO NOTHING")
    for node in (9001, 9002):
        await execute(
            "INSERT INTO memory.nodes (id, mind_id, type, name) "
            "VALUES ($1, $2, 'Concept', $3)", node, mind_id, f"n{node}")
    await execute(
        "INSERT INTO memory.edges (id, mind_id, source_id, target_id, type) "
        "VALUES (9101, $1, 9001, 9002, 'RELATES_TO')", mind_id)


async def _grant(mind_name: str, key: str) -> None:
    await execute(
        "INSERT INTO core.secret_scopes (mind_name, secret_key, granted_at) "
        "VALUES ($1, $2, 0)", mind_name, key,
    )


async def test_a_mind_with_conversations_is_refused_by_name_and_left_whole(
    broker_store,
) -> None:
    await put_mind(MIND, "doomed")
    await _with_conversations(MIND, 2)
    await _grant("doomed", "GITHUB_TOKEN")

    with pytest.raises(broker.MindHasConversations) as refused:
        await broker.delete_mind("doomed")

    assert "conversation" in str(refused.value)
    assert refused.value.sessions == 2
    assert await fetchval(
        "SELECT count(*) FROM core.minds WHERE mind_id = $1", MIND) == 1
    # The refusal is the whole unit's: the grants are not half-cleared.
    assert await fetchval(
        "SELECT count(*) FROM core.secret_scopes WHERE mind_name = 'doomed'"
    ) == 1


async def test_deleting_a_mind_clears_its_grants_and_its_claims_only(
    broker_store,
) -> None:
    await put_mind(MIND, "doomed")
    await put_mind(OTHER, "kept")
    for name in ("doomed", "kept"):
        await _grant(name, "GITHUB_TOKEN")
    await execute(
        "INSERT INTO core.surface_claims (mind_id, surface, bot_ref) "
        "VALUES ($1, 'telegram', 'doomed_bot')", MIND,
    )

    assert await broker.delete_mind("doomed") is True

    assert await fetchval(
        "SELECT count(*) FROM core.minds WHERE mind_id = $1", MIND) == 0
    assert await fetchval(
        "SELECT count(*) FROM core.secret_scopes WHERE mind_name = 'doomed'"
    ) == 0
    assert await fetchval(
        "SELECT count(*) FROM core.surface_claims WHERE mind_id = $1", MIND
    ) == 0
    assert await fetchval(
        "SELECT count(*) FROM core.secret_scopes WHERE mind_name = 'kept'"
    ) == 1


async def test_the_console_is_told_the_mind_still_has_conversations(
    broker_store, monkeypatch,
) -> None:
    """Driven over ASGI on this test's loop, where the pool is: a refusal
    the route let escape would arrive as a 500 with no sentence in it."""
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN)
    from comms import server

    await put_mind(MIND, "doomed")
    await _with_conversations(MIND, 2)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://comms.test",
        headers={"Authorization": f"Bearer {ADMIN}"},
    ) as client:
        answer = await client.delete("/broker/minds/doomed")

    assert answer.status_code == 409
    assert "still has 2 conversations" in answer.json()["error"]


async def test_a_mind_the_memory_graph_still_names_is_refused_and_left_whole(
    broker_store, graph_rows,
) -> None:
    """`memory.edges.mind_id` is RESTRICT too: a mind with no conversations
    but with knowledge-graph edges is refused by name, not with a 500, and
    the refusal undoes the grant clearing that ran before it."""
    await put_mind(MIND, "remembered")
    await _grant("remembered", "GITHUB_TOKEN")
    await _named_by_the_memory_graph(MIND)

    with pytest.raises(broker.MindStillReferenced):
        await broker.delete_mind("remembered")

    assert await fetchval(
        "SELECT count(*) FROM core.minds WHERE mind_id = $1", MIND) == 1
    assert await fetchval(
        "SELECT count(*) FROM core.secret_scopes WHERE mind_name = 'remembered'"
    ) == 1


async def test_the_console_is_told_the_memory_graph_still_names_the_mind(
    broker_store, graph_rows, monkeypatch,
) -> None:
    """The graph refusal reaches the console as a 409 with its sentence,
    not as the database's foreign-key violation surfacing as a 500."""
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN)
    from comms import server

    await put_mind(MIND, "remembered")
    await _named_by_the_memory_graph(MIND)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://comms.test",
        headers={"Authorization": f"Bearer {ADMIN}"},
    ) as client:
        answer = await client.delete("/broker/minds/remembered")

    assert answer.status_code == 409
    assert "still referenced" in answer.json()["error"]
    assert await fetchval(
        "SELECT count(*) FROM core.minds WHERE mind_id = $1", MIND) == 1
