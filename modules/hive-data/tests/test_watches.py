"""Who is owed a wake when a watched session stops (feature 2.1).

Each test names the statement the gateway relies on to decide who is woken,
and reads what it produced back on a plain driver connection.
"""

from __future__ import annotations

import hashlib
import uuid

from hive_data import core, watches


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


async def _session(mind_id: str, clock: float) -> str:
    session_id = _id("sess")
    await core.create_session(
        id=session_id, surface="telegram", surface_id="1", mind_id=mind_id,
        created_at=clock, last_active=clock, conversation_id=_id("conv"))
    return session_id


async def _armed(watched: str, sender: str, mind: str, clock: float,
                 secret: str = "cap") -> None:
    await watches.ask(watched, sender, "task", _digest(secret), now=clock)
    await watches.arm(watched, sender, 0)


async def test_a_stop_wakes_the_conversation_that_asked_and_no_other(
        a_mind, clock, inspector):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    bystander = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock)

    owed = await watches.record_stop(worker, "t1", "ended", turn_id=1)

    assert owed == [sender]
    rows = await inspector.fetch(
        "SELECT sender_session_id FROM core.session_wakes "
        "WHERE watched_session_id = $1", worker)
    assert [r["sender_session_id"] for r in rows] == [sender]
    assert bystander not in [r["sender_session_id"] for r in rows]


async def test_the_same_stop_found_twice_owes_one_wake(
        a_mind, clock, inspector):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock)

    first = await watches.record_stop(worker, "t7", "ended", turn_id=7)
    second = await watches.record_stop(worker, "t7", "ended", turn_id=7)

    assert first == [sender]
    assert second == []
    count = await inspector.fetchval(
        "SELECT count(*) FROM core.session_wakes "
        "WHERE watched_session_id = $1", worker)
    assert count == 1


async def test_a_delivered_wake_is_never_claimed_again(
        a_mind, clock, inspector):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock)
    await watches.record_stop(worker, "t1", "ended", turn_id=1)

    claimed = await watches.claim_pending(sender)
    assert await watches.claim_pending(sender) == []
    await watches.mark_delivered([row["id"] for row in claimed])
    # The slow check rediscovering the same stop must owe nothing new.
    await watches.record_stop(worker, "t1", "ended", turn_id=1)
    await watches.release_stale_claims(older_than=clock + 10_000_000_000)

    assert len(claimed) == 1
    assert await watches.claim_pending(sender) == []


async def test_a_wake_released_after_a_failed_delivery_is_claimed_once_more(
        a_mind, clock):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock)
    await watches.record_stop(worker, "t1", "died", turn_id=1)

    first = await watches.claim_pending(sender)
    await watches.release_claims([row["id"] for row in first])
    again = await watches.claim_pending(sender)

    assert [r["id"] for r in again] == [r["id"] for r in first]
    assert again[0]["outcome"] == "died"


async def test_a_stop_after_the_owner_marked_it_finished_wakes_nobody(
        a_mind, clock):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock, secret="mine")
    assert await watches.record_stop(worker, "t1", "ended", turn_id=1) == [sender]

    refused = await watches.finish(worker, sender, _digest("someone else"))
    stillwatched = await watches.record_stop(worker, "t2", "ended", turn_id=2)
    finished = await watches.finish(worker, sender, _digest("mine"))
    after = await watches.record_stop(worker, "t3", "ended", turn_id=3)

    assert refused == "refused"
    assert stillwatched == [sender]
    assert finished == "finished"
    assert after == []
    assert await watches.claim_pending(sender) == []


async def test_a_turn_that_was_itself_a_wake_wakes_nobody_and_stays_fetchable(
        a_mind, clock):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock)

    owed = await watches.record_stop(worker, "t4", "ended", wake=True, turn_id=4)

    assert owed == []
    latest = await watches.latest_stop(worker)
    assert latest["stop_key"] == "t4" and latest["wake"] is True


async def test_a_watch_not_yet_armed_is_not_woken_by_a_turn_already_running(
        a_mind, clock):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await watches.ask(worker, sender, "task", _digest("cap"), now=clock)

    before_brief = await watches.record_stop(worker, "t1", "ended", turn_id=1)
    await watches.arm(worker, sender, 5)
    old_turn = await watches.record_stop(worker, "t3", "ended", turn_id=3)
    brief_turn = await watches.record_stop(worker, "t9", "ended", turn_id=9)

    assert before_brief == []
    assert old_turn == []
    assert brief_turn == [sender]


async def test_a_brief_that_failed_before_it_started_owes_its_sender_one_wake(
        a_mind, clock):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await watches.ask(worker, sender, "task", _digest("cap"), now=clock)

    first = await watches.owe_wake(worker, sender, "b-1", "died")
    second = await watches.owe_wake(worker, sender, "b-1", "died")

    assert (first, second) == (True, False)
    claimed = await watches.claim_pending(sender)
    assert [r["outcome"] for r in claimed] == ["died"]


async def test_moving_a_ledger_task_to_done_ends_the_watch_on_its_session(
        a_mind, clock, inspector):
    from hive_data import staff

    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock)
    task = await staff.create_task("build it", "in progress", now=clock)
    await staff.attach_session(task["id"], worker, now=clock)

    await staff.update_task(task["id"], status="in progress", now=clock + 1)
    still = await watches.on_session(worker)
    await staff.update_task(task["id"], status="done", now=clock + 2)

    assert len(still) == 1
    assert await watches.on_session(worker) == []
    assert await watches.record_stop(worker, "t1", "ended", turn_id=1) == []


async def test_a_brief_that_failed_before_it_started_arms_the_watch_for_later_stops(
        a_mind, clock):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await watches.ask(worker, sender, "task", _digest("cap"), now=clock)
    await watches.owe_wake(worker, sender, "b-1", "died")

    owed = await watches.record_stop(worker, "t9", "ended", turn_id=9)

    assert owed == [sender]


async def test_a_wake_held_for_a_suspended_or_closed_conversation_is_not_owed_to_the_mind(
        a_mind, clock):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock)
    await watches.record_stop(worker, "t1", "ended", turn_id=1)
    live = await watches.wake_state(a_mind)

    await core.update_session(sender, status="suspended")

    assert live["owed"] == 1
    assert (await watches.wake_state(a_mind))["owed"] == 0


async def test_the_wakes_of_a_wake_turn_the_mind_never_took_are_owed_again_without_their_ids(
        a_mind, clock):
    worker = await _session(a_mind, clock)
    sender = await _session(a_mind, clock)
    await _armed(worker, sender, a_mind, clock)
    await watches.record_stop(worker, "t1", "ended", turn_id=1)
    claimed = await watches.claim_pending(sender)
    await watches.mark_delivered([row["id"] for row in claimed])
    assert await watches.turn_is_wake(sender) is True

    undone = await watches.undeliver_open(sender)

    assert undone == 1
    assert await watches.turn_is_wake(sender) is False
    assert [r["outcome"] for r in await watches.claim_pending(sender)] == ["ended"]
