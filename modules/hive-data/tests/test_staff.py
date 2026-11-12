"""The chief of staff's ledger, through the `staff` operations.

What a write produced is read back with a plain driver connection as well as
through the layer, for the reason the round-trip suite gives: a layer that
writes and reads the same wrong column consistently passes its own round
trip.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

import hive_data
from hive_data import staff


async def _session(mind_id: str, clock: float) -> str:
    session_id = f"sess-{uuid.uuid4().hex[:10]}"
    await hive_data.core.create_session(
        id=session_id, surface="telegram", surface_id="chat-1",
        mind_id=mind_id, created_at=clock, last_active=clock)
    return session_id


async def test_a_created_task_comes_back_with_its_name_description_and_status(
        connected, inspector, clock):
    made = await staff.create_task(
        "Move coach into the health app", "waiting on response",
        description="implementing a new feature for the health app, "
                    "feature 6.2", now=clock)
    other = await staff.create_task("Another task", "on hold", now=clock)

    found = await staff.task(made["id"])
    assert found is not None
    assert found["name"] == "Move coach into the health app"
    assert found["description"] == ("implementing a new feature for the "
                                    "health app, feature 6.2")
    assert found["status"] == "waiting on response"
    assert (await staff.task(other["id"]))["name"] == "Another task"
    stored = await inspector.fetchrow(
        "SELECT name, description, status FROM staff.tasks WHERE id = $1",
        made["id"])
    assert dict(stored) == {
        "name": "Move coach into the health app",
        "description": "implementing a new feature for the health app, "
                       "feature 6.2",
        "status": "waiting on response"}


async def test_two_sessions_on_one_task_are_both_listed_and_each_finds_it(
        connected, a_mind, clock):
    made = await staff.create_task("Bake-off", "in progress", now=clock)
    other = await staff.create_task("Something else", "in progress",
                                    now=clock)
    first = await _session(a_mind, clock)
    second = await _session(a_mind, clock)
    elsewhere = await _session(a_mind, clock)
    # Attached in the reverse of id order, at distinct moments, so the
    # listing's order can only come from when each was attached.
    early, late = sorted([first, second], reverse=True)
    await staff.attach_session(made["id"], early, now=clock)
    await staff.attach_session(other["id"], elsewhere, now=clock + 1)
    await staff.attach_session(made["id"], late, now=clock + 2)

    listed = await staff.task_sessions(made["id"])
    assert [r["session_id"] for r in listed] == [early, late]
    assert (await staff.task_for_session(first))["id"] == made["id"]
    assert (await staff.task_for_session(second))["id"] == made["id"]
    assert (await staff.task_for_session(elsewhere))["id"] == other["id"]


async def test_a_pair_outlives_comms_ending_its_session(
        connected, a_mind, clock):
    made = await staff.create_task("Watched", "in progress", now=clock)
    session_id = await _session(a_mind, clock)
    await staff.attach_session(made["id"], session_id, now=clock)

    await hive_data.core.delete_session(session_id)

    assert [r["session_id"] for r in await staff.task_sessions(made["id"])] \
        == [session_id]
    assert (await staff.task_for_session(session_id))["id"] == made["id"]


async def test_listing_by_a_status_not_in_the_lookup_is_refused(connected):
    with pytest.raises(hive_data.MissingReference):
        await staff.tasks(status="in-progress")


async def test_an_update_expecting_a_status_the_task_has_left_changes_nothing(
        connected, inspector, clock):
    made = await staff.create_task("Closed by Daniel", "done", now=clock)

    out = await staff.update_task(made["id"], status="on hold",
                                  expected_status="in progress",
                                  now=clock + 60)

    assert out is None
    assert await inspector.fetchval(
        "SELECT status FROM staff.tasks WHERE id = $1", made["id"]) == "done"
    moved = await staff.update_task(made["id"], status="in progress",
                                    expected_status="done", now=clock + 61)
    assert moved["status"] == "in progress"
    assert moved["updated_at"] == clock + 61


@pytest.mark.parametrize("blank", ["", "   ", "\u00a0", "\u200b\u3000"])
async def test_a_blank_looking_name_or_id_is_refused(connected, inspector,
                                                     clock, blank):
    with pytest.raises(hive_data.ValueRefused):
        await staff.create_task(blank, "in progress", now=clock)
    with pytest.raises(hive_data.ValueRefused):
        await staff.create_task("Named", "in progress", task_id=blank,
                                now=clock)
    with pytest.raises(hive_data.ValueRefused):
        await staff.add_status(blank)
    # And by the schema itself, for a writer that skips this module.
    with pytest.raises(asyncpg.CheckViolationError):
        await inspector.execute(
            "INSERT INTO staff.statuses (name) VALUES ($1)", blank)
    with pytest.raises(asyncpg.CheckViolationError):
        await inspector.execute(
            "INSERT INTO staff.tasks (id, name, status, created_at, "
            "updated_at) VALUES ($1, 'x', 'done', 0, 0)", blank)


async def test_a_status_not_in_the_lookup_is_refused_and_the_task_is_unchanged(
        connected, inspector, clock):
    made = await staff.create_task("Watch this", "on hold", now=clock)

    with pytest.raises(hive_data.MissingReference):
        await staff.update_task(made["id"], status="abandoned",
                                name="renamed", now=clock + 60)

    stored = await inspector.fetchrow(
        "SELECT name, status, updated_at FROM staff.tasks WHERE id = $1",
        made["id"])
    assert dict(stored) == {"name": "Watch this", "status": "on hold",
                            "updated_at": clock}


async def test_deleting_a_task_removes_it_and_its_pairs_and_leaves_others(
        connected, inspector, a_mind, clock):
    doomed = await staff.create_task("Opened in error", "in progress",
                                     now=clock)
    kept = await staff.create_task("Real work", "in progress", now=clock)
    session_id = await _session(a_mind, clock)
    await staff.attach_session(doomed["id"], session_id, now=clock)

    out = await staff.delete_task(doomed["id"])

    assert out["rows"] == 1
    assert await staff.task(doomed["id"]) is None
    assert await inspector.fetchval(
        "SELECT count(*) FROM staff.task_sessions WHERE session_id = $1",
        session_id) == 0
    assert (await staff.task(kept["id"]))["name"] == "Real work"
    assert (await staff.delete_task(doomed["id"]))["rows"] == 0


async def test_the_staff_role_reaches_the_ledger_and_no_other_schema(
        connected, inspector, clock):
    made = await staff.create_task("Visible to the chief", "in progress",
                                   now=clock)
    assert await inspector.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_database d, "
        "aclexplode(d.datacl) a WHERE d.datname = current_database() "
        "AND a.grantee = 'hive_staff'::regrole AND a.privilege_type = "
        "'CONNECT')")
    # Rolled back at the end, so nothing the probe makes outlives it.
    outer = inspector.transaction()
    await outer.start()
    try:
        # Made by the migrating role, as a later step's table would be.
        await inspector.execute(
            "CREATE TABLE staff.later_table (x int); "
            "INSERT INTO staff.later_table VALUES (1)")
        await inspector.execute("SET LOCAL ROLE hive_staff")
        assert await inspector.fetchval(
            "SELECT name FROM staff.tasks WHERE id = $1",
            made["id"]) == "Visible to the chief"
        await inspector.execute(
            "UPDATE staff.tasks SET status = 'on hold' WHERE id = $1",
            made["id"])
        await inspector.execute(
            "INSERT INTO staff.tasks (id, name, status, created_at, "
            "updated_at) VALUES ('by-the-chief', 'x', 'done', 0, 0)")
        assert await inspector.execute(
            "DELETE FROM staff.tasks WHERE id = 'by-the-chief'") == "DELETE 1"
        assert await inspector.fetchval(
            "SELECT x FROM staff.later_table") == 1
        refused = [f"SELECT 1 FROM {table} LIMIT 1" for table in (
            "core.sessions", "tools.users", "board.work_items",
            "memory.memories")] + ["CREATE TABLE staff.mine (x int)"]
        for statement in refused:
            # The savepoint rolls back the refusal, so the next one is read.
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with inspector.transaction():
                    await inspector.execute(statement)
    finally:
        await outer.rollback()
