"""Row counts, units of work, ordering, and serializing across processes."""

from __future__ import annotations

import asyncio
import uuid

import pytest

import hive_data
from hive_data import core


async def test_a_conditional_claim_tells_exactly_one_caller_it_won(
        connected, a_mind, clock, inspector):
    """This row count is the whole arbitration between two people sending a
    bot's claim phrase at the same moment. A write that could only answer
    with rows would answer `[]` to both and confirm two owners."""
    await core.upsert_claim(mind_id=a_mind, surface="telegram")

    first = await core.claim_surface_if_unclaimed(
        mind_id=a_mind, surface="telegram", claimed_ref="daniel",
        claimed_at=clock)
    second = await core.claim_surface_if_unclaimed(
        mind_id=a_mind, surface="telegram", claimed_ref="somebody-else",
        claimed_at=clock + 1)

    assert first["rows"] == 1
    assert second["rows"] == 0
    assert await inspector.fetchval(
        'SELECT "claimed_ref" FROM core."surface_claims" '
        'WHERE "mind_id" = $1 AND "surface" = $2', a_mind,
        "telegram") == "daniel"


async def test_a_conditional_update_that_matches_nothing_reports_no_rows(
        connected, a_mind, clock):
    """"Is there still a session with that id" is asked by exactly this
    means: a context report for a session that has been swept must not
    answer ok."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id="chat-1",
        mind_id=a_mind, created_at=clock, last_active=clock)

    landed = await core.update_session_if(
        session_id, expected={"status": "running"}, context_tokens=100)
    missed = await core.update_session_if(
        session_id, expected={"status": "closed"}, context_tokens=200)
    gone = await core.update_session("no-such-session", context_tokens=300)

    assert landed["rows"] == 1
    assert missed["rows"] == 0
    assert gone["rows"] == 0
    assert (await core.get_session(session_id))["context_tokens"] == 100


async def test_the_guess_counter_is_incremented_in_the_read(
        connected, a_mind, inspector):
    """Read-modify-write counts rounds rather than guesses: parallel attempts
    all read the same number and all write the same number, so the lockout
    that stops somebody taking an unclaimed bot by brute force never
    arrives."""
    await core.upsert_claim(mind_id=a_mind, surface="telegram")

    results = await asyncio.gather(*[
        core.count_phrase_attempt(mind_id=a_mind, surface="telegram")
        for _ in range(8)
    ])

    assert sorted(r["row"]["phrase_attempts"] for r in results) == list(
        range(1, 9))
    assert await inspector.fetchval(
        'SELECT "phrase_attempts" FROM core."surface_claims" '
        'WHERE "mind_id" = $1 AND "surface" = $2', a_mind, "telegram") == 8


async def test_a_unit_whose_second_write_is_refused_leaves_no_trace_of_the_first(
        connected, a_mind, clock, inspector):
    """The likely defect in a pool-backed layer is a transaction helper that
    acquires a connection per operation, so the first write commits on its
    own connection and only the second rolls back."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"

    with pytest.raises(hive_data.MissingReference):
        async with hive_data.transaction() as unit:
            await core.create_session(
                id=session_id, surface="telegram", surface_id="chat-1",
                mind_id=a_mind, created_at=clock, last_active=clock,
                unit=unit)
            await core.add_message(
                id=f"m-{uuid.uuid4().hex[:8]}",
                conversation_id="no-such-conversation", from_mind="skippy",
                to_mind="mordecai", message_number=1, content="orphan",
                status="queued", timestamp=clock, unit=unit)

    assert await inspector.fetchval(
        'SELECT count(*) FROM core."sessions" WHERE "id" = $1',
        session_id) == 0


async def test_a_unit_abandoned_by_an_exception_returns_its_connection(
        connected, a_mind, clock):
    """A rolled-back unit that leaked its connection would exhaust the pool
    after a handful of failures, and every later operation would wait on
    acquire behind work that is never going to finish."""
    for _ in range(hive_data.settings_in_use().pool_max + 3):
        with pytest.raises(RuntimeError):
            async with hive_data.transaction():
                raise RuntimeError("the caller gave up")

    assert await core.get_mind(a_mind) is not None


async def test_a_unit_cannot_be_opened_inside_an_async_generator(connected):
    """`send_message` is an async generator that writes to the database
    inside its own loop over the model's response. A unit opened there pins
    a connection for the length of a turn, and a closed browser tab abandons
    the generator with the transaction still open."""
    async def streaming_turn():
        async with hive_data.transaction() as unit:
            yield unit

    with pytest.raises(hive_data.TransactionMisuse):
        async for _ in streaming_turn():
            pass


async def test_a_unit_handed_to_another_task_is_refused(connected):
    """Two tasks on one connection inside one transaction means each can roll
    back the other's committed-looking work."""
    async def borrow(unit):
        return await core.list_minds(unit=unit)

    with pytest.raises(hive_data.TransactionMisuse):
        async with hive_data.transaction() as unit:
            await asyncio.create_task(borrow(unit))


async def test_the_newest_turn_is_the_newest_when_timestamps_tie(
        connected, a_mind, clock, inspector):
    """`created_at` is whole epoch seconds and `now()` is constant for a
    transaction, so two turns written in one unit of work carry the same
    timestamp. Without the identity tiebreak the order is whichever the plan
    produced, and the mind appears to answer before it was asked."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="telegram", surface_id="chat-1",
        mind_id=a_mind, created_at=clock, last_active=clock)

    async with hive_data.transaction() as unit:
        first = await core.record_turn(
            session_id=session_id, role="user", content="the question",
            created_at=clock, unit=unit)
        second = await core.record_turn(
            session_id=session_id, role="assistant", content="the answer",
            created_at=clock, unit=unit)

    # What this guards, and what it cannot. It catches an ordering that is
    # wrong or inverted — and the identity tiebreak the code carries is not
    # falsifiable through this interface at all: Postgres answers a tie the
    # same way with and without it, on every arrangement of rows I could
    # construct, because its top-N sort happens to keep the row this
    # ordering wants. So the tiebreak stays in the code on the strength of
    # the reasoning rather than of a test, and that is said here rather
    # than implied by a test that would pass either way.
    newest = await core.latest_turn(session_id)
    assert newest["content"] == "the answer"
    assert newest["id"] == second["row"]["id"]
    assert newest["id"] > first["row"]["id"]

    assert [t["content"] for t in await core.list_turns(session_id)] == [
        "the question", "the answer"]
    assert (await core.latest_turn(session_id, role="user"))["content"] == (
        "the question")


async def test_a_filtered_listing_returns_the_matching_rows_only(
        connected, a_mind, clock):
    marker = uuid.uuid4().hex[:8]
    wanted, unwanted = f"s-{marker}-a", f"s-{marker}-b"
    await core.create_session(
        id=wanted, surface="terminal", surface_id="tile-1", mind_id=a_mind,
        created_at=clock, last_active=clock + 1)
    await core.create_session(
        id=unwanted, surface="telegram", surface_id="chat-1", mind_id=a_mind,
        created_at=clock, last_active=clock + 2)

    found = await core.list_sessions(surfaces=["terminal"], mind_id=a_mind)

    assert [s["id"] for s in found] == [wanted]


async def test_a_held_key_serializes_two_units_across_connections(
        connected, a_mind, clock, inspector):
    """What replaces the first consumer's in-process locks. Those are per
    process, and the only reason they sufficed is that comms is the sole
    writer holding SQLite's lock for the life of its container — the exact
    property Postgres removes. The adopt sequence is a delete and then a
    write, and run twice at once it leaves two rows for one session."""
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    tile = f"tile-{uuid.uuid4().hex[:8]}"
    await core.create_session(
        id=session_id, surface="terminal", surface_id=tile,
        mind_id=a_mind, created_at=clock, last_active=clock)
    order: list[str] = []

    async def adopt(label: str, delay: float) -> None:
        async with hive_data.transaction() as unit:
            await unit.hold_conversation(session_id)
            order.append(f"{label}-in")
            await core.unbind_surface("terminal", tile, unit=unit)
            await asyncio.sleep(delay)
            await core.bind_surface(
                client_type="terminal", client_ref=tile,
                session_id=session_id, unit=unit)
            order.append(f"{label}-out")

    await asyncio.gather(adopt("first", 0.2), adopt("second", 0.0))

    # Neither pair interleaves: whoever got the lock finished before the
    # other started.
    assert order in (
        ["first-in", "first-out", "second-in", "second-out"],
        ["second-in", "second-out", "first-in", "first-out"],
    ), order
    assert await inspector.fetchval(
        'SELECT count(*) FROM core."active_sessions" '
        'WHERE "session_id" = $1', session_id) == 1
