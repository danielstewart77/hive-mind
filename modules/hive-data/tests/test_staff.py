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


# ------------------------------------------------------- the tick's writes


async def test_note_stopped_keeps_the_first_tick_moment_not_the_latest(
        connected, inspector, clock):
    """A second tick must not overwrite when the stop was first seen."""
    made = await staff.create_task("Watched work", "in progress", now=clock)

    first = await staff.note_stopped(made["id"], now=clock + 60)
    again = await staff.note_stopped(made["id"], now=clock + 600)

    assert first is not None and first["stopped_at"] == clock + 60
    assert again is None, "the guarded update reports nothing changed"
    assert await inspector.fetchval(
        "SELECT stopped_at FROM staff.tasks WHERE id = $1",
        made["id"]) == clock + 60


async def test_note_stopped_moves_updated_at_so_the_sitrep_sees_the_stop(
        connected, inspector, clock):
    made = await staff.create_task("Watched work", "in progress", now=clock)

    await staff.note_stopped(made["id"], now=clock + 60)

    assert await inspector.fetchval(
        "SELECT updated_at FROM staff.tasks WHERE id = $1",
        made["id"]) == clock + 60


async def test_note_woken_leaves_updated_at_where_the_work_last_moved(
        connected, inspector, clock):
    """Delivering a wake is bookkeeping, not movement in the work."""
    made = await staff.create_task("Watched work", "in progress", now=clock)
    await staff.note_stopped(made["id"], now=clock + 60)

    await staff.note_woken(made["id"], now=clock + 120)

    row = await inspector.fetchrow(
        "SELECT updated_at, woken_at FROM staff.tasks WHERE id = $1",
        made["id"])
    assert row["woken_at"] == clock + 120
    assert row["updated_at"] == clock + 60


async def test_note_running_clears_the_stop_so_a_later_one_is_noticed(
        connected, inspector, clock):
    made = await staff.create_task("Watched work", "in progress", now=clock)
    await staff.note_stopped(made["id"], now=clock + 60)

    cleared = await staff.note_running(made["id"])
    stopped_again = await staff.note_stopped(made["id"], now=clock + 900)

    assert cleared is not None and cleared["stopped_at"] is None
    assert stopped_again is not None, (
        "with the first stop cleared, the guard lets the next one record")
    assert await inspector.fetchval(
        "SELECT stopped_at FROM staff.tasks WHERE id = $1",
        made["id"]) == clock + 900


async def test_note_running_changes_nothing_when_no_stop_was_recorded(
        connected, clock):
    """Every tick calls this for every live task; it must stay a no-op."""
    made = await staff.create_task("Watched work", "in progress", now=clock)

    assert await staff.note_running(made["id"]) is None


async def test_the_ticks_writes_never_move_a_status(connected, inspector,
                                                    clock):
    """A status is Daniel's reading of the work; the machine never sets one."""
    made = await staff.create_task("Watched work", "on hold", now=clock)

    await staff.note_stopped(made["id"], now=clock + 60)
    await staff.note_woken(made["id"], now=clock + 120)
    await staff.note_running(made["id"])

    assert await inspector.fetchval(
        "SELECT status FROM staff.tasks WHERE id = $1",
        made["id"]) == "on hold"


async def test_a_wake_is_pending_from_the_two_timestamps_alone(connected,
                                                               clock):
    made = await staff.create_task("Watched work", "in progress", now=clock)
    assert staff.wake_pending(await staff.task(made["id"])) is False

    await staff.note_stopped(made["id"], now=clock + 60)
    assert staff.wake_pending(await staff.task(made["id"])) is True

    await staff.note_woken(made["id"], now=clock + 120)
    assert staff.wake_pending(await staff.task(made["id"])) is False


async def test_a_task_that_stops_again_after_a_wake_goes_pending_again(
        connected, clock):
    """The comparison is what makes a second stop news with nothing cleared."""
    made = await staff.create_task("Watched work", "in progress", now=clock)
    await staff.note_stopped(made["id"], now=clock + 60)
    await staff.note_woken(made["id"], now=clock + 120)
    await staff.note_running(made["id"])

    await staff.note_stopped(made["id"], now=clock + 600)

    assert staff.wake_pending(await staff.task(made["id"])) is True


async def test_the_ticks_writes_report_nothing_for_a_task_that_is_not_there(
        connected, clock):
    assert await staff.note_stopped("no-such-task", now=clock) is None
    assert await staff.note_woken("no-such-task", now=clock) is None
    assert await staff.note_running("no-such-task") is None


# ---------------------------------------------- the board item and question
#
# What the Chief of Staff page draws a tree and its bubbles from (step 9).


async def test_a_task_carries_the_board_item_it_covers(connected, inspector,
                                                       clock):
    """Without it a task that *is* a feature can only ever be one node."""
    made = await staff.create_task("Chief of staff page", "in progress",
                                   now=clock)

    await staff.update_task(made["id"], board_item="f1deb69399136e16d",
                            now=clock + 1)

    assert (await staff.task(made["id"]))["board_item"] == "f1deb69399136e16d"
    assert await inspector.fetchval(
        "SELECT board_item FROM staff.tasks WHERE id = $1",
        made["id"]) == "f1deb69399136e16d"


async def test_a_task_carries_the_question_it_is_holding_for_daniel(
        connected, inspector, clock):
    """The status said an answer was owed; only this says which."""
    made = await staff.create_task("Judging brief", "in progress", now=clock)

    await staff.update_task(
        made["id"], question="Which model does the judging child run on?",
        now=clock + 1)

    assert (await staff.task(made["id"]))["question"] == (
        "Which model does the judging child run on?")
    assert await inspector.fetchval(
        "SELECT question FROM staff.tasks WHERE id = $1",
        made["id"]) == "Which model does the judging child run on?"


async def test_an_answered_question_is_cleared_without_touching_the_status(
        connected, clock):
    """A task can hold a question and still be worked, so the two are apart."""
    made = await staff.create_task("Judging brief", "in progress", now=clock)
    await staff.update_task(made["id"], question="Which model?", now=clock + 1)

    row = await staff.update_task(made["id"], question=None, now=clock + 2)

    assert row["question"] is None
    assert row["status"] == "in progress"


async def test_a_new_task_holds_neither_a_board_item_nor_a_question(
        connected, clock):
    made = await staff.create_task("Something small", "in progress", now=clock)

    assert made["board_item"] is None
    assert made["question"] is None


# ------------------------------------------------------------- sub-agents
#
# A sub-agent is an in-process child of a mind's turn, invisible from
# outside it, so the children register themselves (step 9).


async def test_a_sub_agent_registers_itself_against_the_task_it_works(
        connected, inspector, clock):
    task = await staff.create_task("Tend the ledger", "in progress", now=clock)

    agent = await staff.start_agent(task["id"], "read-and-judge.md",
                                    about="session 1a2b", now=clock + 5)

    assert agent["brief"] == "read-and-judge.md"
    assert agent["about"] == "session 1a2b"
    assert agent["started_at"] == clock + 5
    assert agent["finished_at"] is None
    stored = await inspector.fetchrow(
        "SELECT task_id, brief, about FROM staff.agents WHERE id = $1",
        agent["id"])
    assert dict(stored) == {"task_id": task["id"],
                            "brief": "read-and-judge.md",
                            "about": "session 1a2b"}


async def test_a_sub_agent_cannot_hang_off_a_task_that_is_not_there(
        connected, clock):
    """A typo leaving a child under nothing would draw as a loose node."""
    with pytest.raises(hive_data.errors.MissingReference):
        await staff.start_agent("no-such-task", "chase.md", now=clock)


async def test_a_sub_agent_that_reports_carries_its_outcome_and_end(
        connected, clock):
    task = await staff.create_task("Tend the ledger", "in progress", now=clock)
    agent = await staff.start_agent(task["id"], "chase.md", now=clock + 5)

    closed = await staff.finish_agent(agent["id"], outcome="waiting",
                                      now=clock + 70)

    assert closed["finished_at"] == clock + 70
    assert closed["outcome"] == "waiting"


async def test_a_brief_reporting_twice_keeps_the_moment_its_work_ended(
        connected, clock):
    task = await staff.create_task("Tend the ledger", "in progress", now=clock)
    agent = await staff.start_agent(task["id"], "chase.md", now=clock + 5)
    await staff.finish_agent(agent["id"], outcome="waiting", now=clock + 70)

    again = await staff.finish_agent(agent["id"], outcome="later",
                                     now=clock + 900)

    assert again is None
    assert (await staff.agents(task_id=task["id"]))[0]["finished_at"] == (
        clock + 70)


async def test_only_the_sub_agents_still_working_are_listed_when_asked(
        connected, clock):
    task = await staff.create_task("Tend the ledger", "in progress", now=clock)
    done = await staff.start_agent(task["id"], "chase.md", now=clock + 1)
    live = await staff.start_agent(task["id"], "read-and-judge.md",
                                   now=clock + 2)
    await staff.finish_agent(done["id"], now=clock + 3)

    working = await staff.agents(task_id=task["id"], working=True)

    assert [a["id"] for a in working] == [live["id"]]


async def test_sub_agents_are_listed_for_one_task_rather_than_every_task(
        connected, clock):
    one = await staff.create_task("First", "in progress", now=clock)
    two = await staff.create_task("Second", "in progress", now=clock)
    mine = await staff.start_agent(one["id"], "chase.md", now=clock + 1)
    await staff.start_agent(two["id"], "chase.md", now=clock + 2)

    assert [a["id"] for a in await staff.agents(task_id=one["id"])] == [
        mine["id"]]


async def test_an_open_sub_agent_past_the_turn_timeout_reads_as_orphaned(
        connected, clock):
    """A child dies with its turn's process and never writes its own ending."""
    task = await staff.create_task("Tend the ledger", "in progress", now=clock)
    agent = await staff.start_agent(task["id"], "chase.md", now=clock)
    row = (await staff.agents(task_id=task["id"]))[0]

    assert staff.agent_stale(row, now=clock + 899, timeout=15 * 60) is False
    assert staff.agent_stale(row, now=clock + 901, timeout=15 * 60) is True
    await staff.finish_agent(agent["id"], now=clock + 10)
    assert staff.agent_stale(
        (await staff.agents(task_id=task["id"]))[0], now=clock + 9000,
        timeout=15 * 60) is False


async def test_deleting_a_task_opened_in_error_takes_its_sub_agents(
        connected, inspector, clock):
    task = await staff.create_task("Opened wrong", "in progress", now=clock)
    await staff.start_agent(task["id"], "chase.md", now=clock + 1)

    await staff.delete_task(task["id"])

    assert await inspector.fetchval(
        "SELECT count(*) FROM staff.agents WHERE task_id = $1",
        task["id"]) == 0
