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


# -------------------------------------------- sub-agents a harness reported
#
# Step 10. A harness knows its mind, its conversation, the kind of child and
# which child spawned it, and knows nothing about the ledger. Its reports
# are detached so that a slow recorder can never delay a spawn, which is
# what makes every ordering below reachable rather than theoretical.
#
# Each test reports as a mind of its own. The stored id is namespaced by the
# mind, so that is also what keeps one test's children out of another's
# reads - this suite shares a database across a run and does not truncate.


@pytest.fixture
def a_reporting_mind():
    """A mind id no other test is using."""
    return f"mind-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def other_mind():
    """A second mind, for the reads and collisions that need two."""
    return f"mind-{uuid.uuid4().hex[:8]}"


async def test_a_reported_child_and_a_delegate_child_both_name_their_kind(
        a_reporting_mind, connected, inspector, clock):
    """One shape: a reader asks what kind of child this is, once.

    The chief's delegates are registered with the brief file they were
    handed, and a harness names an agent type. Both land in the same field,
    so nothing downstream has to know which writer made the row.
    """
    task = await staff.create_task("Tend the ledger", "in progress", now=clock)
    delegate = await staff.start_agent(task["id"], "chase.md", now=clock)

    reported = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        agent_type="refute-tests", label="story 6.9.1", now=clock)

    assert delegate["agent_type"] == "chase.md"
    assert reported["agent_type"] == "refute-tests"
    assert reported["about"] == "story 6.9.1"
    kinds = await inspector.fetch(
        "SELECT agent_type FROM staff.agents WHERE id = ANY($1::text[]) "
        "ORDER BY agent_type", [delegate["id"], reported["id"]])
    assert [r["agent_type"] for r in kinds] == ["chase.md", "refute-tests"]


async def test_a_reported_child_keeps_the_conversation_and_the_mind(
        a_reporting_mind, connected, inspector, clock):
    reported = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        conversation_ref="conv-77", session_id="sess-9", now=clock)

    assert reported["mind_id"] == a_reporting_mind
    assert reported["session_id"] == "sess-9"
    assert reported["conversation_ref"] == "conv-77"
    assert reported["task_id"] is None
    stored = await inspector.fetchrow(
        "SELECT mind_id, session_id, conversation_ref, task_id, brief "
        "FROM staff.agents WHERE id = $1", reported["id"])
    assert dict(stored) == {"mind_id": a_reporting_mind,
                            "session_id": "sess-9",
                            "conversation_ref": "conv-77", "task_id": None,
                            "brief": None}


async def test_two_minds_reporting_one_harness_id_are_two_children(
        a_reporting_mind, other_mind, connected, clock):
    """A harness agent id is unique in its own process and nowhere else.

    Unnamespaced, the second mind's start would be refused as a duplicate and
    its ending would close the first mind's row.
    """
    first = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    second = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=other_mind, now=clock)

    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=other_mind, outcome="ok",
        now=clock + 30)

    assert first["id"] != second["id"]
    rows = {r["id"]: r for r in await staff.agents()}
    assert rows[first["id"]]["finished_at"] is None
    assert rows[second["id"]]["outcome"] == "ok"


async def test_the_same_report_twice_lands_once_and_changes_nothing(
        a_reporting_mind, connected, clock):
    """A retried write is the ordinary case: the reporter is detached and
    cannot see whether its first attempt landed."""
    first = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        agent_type="refute-edges", now=clock)

    again = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        agent_type="refute-edges", now=clock + 400)

    assert again["id"] == first["id"]
    assert again["started_at"] == clock
    assert len(await staff.agents(mind_id=a_reporting_mind)) == 1


async def test_an_ending_that_arrives_before_its_own_start_still_ends(
        a_reporting_mind, connected, clock):
    """Both reports are in flight at once for a child that finishes fast."""
    ended = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="ok",
        now=clock + 4)

    late_start = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        agent_type="refute-tests", now=clock + 9)

    assert ended["finished_at"] == clock + 4
    assert late_start["finished_at"] == clock + 4, "a start must not reopen"
    assert late_start["outcome"] == "ok"
    assert late_start["agent_type"] == "refute-tests", "the kind still lands"


async def test_an_ending_a_harness_could_not_describe_is_not_an_abandoned_one(
        a_reporting_mind, connected, clock):
    """A clean finish nobody described and a child killed with its turn are
    different facts, and a reader deciding whether anything went wrong has to
    tell them apart."""
    reported = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, now=clock)

    assert reported["outcome"] == staff.OUTCOME_UNKNOWN
    assert staff.OUTCOME_UNKNOWN != staff.OUTCOME_ABANDONED


async def test_a_child_nothing_was_heard_from_is_recorded_as_abandoned(
        a_reporting_mind, connected, inspector, clock):
    """P5: a record that only reads as dead is live again in every other
    reader, so the sweep writes the ending down."""
    open_child = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    fresh = await staff.record_agent_event(
        "d4e5f6", staff.SPAWNED, mind_id=a_reporting_mind, now=clock + 1900)

    # The sweep deliberately runs later than the window, so that the moment
    # it *would* write if it used its own clock is distinct from the moment
    # the child was last known alive. With the two equal, a sweep stamping
    # `now - timeout` passes this test by arithmetic coincidence.
    retired = await staff.retire_stale_agents(now=clock + 2000,
                                              timeout=1800.0)

    mine = [r for r in retired if r["mind_id"] == a_reporting_mind]
    assert [r["id"] for r in mine] == [open_child["id"]]
    retired = mine
    assert retired[0]["outcome"] == staff.OUTCOME_ABANDONED
    # The ending is the last moment the child was known alive, not the
    # moment somebody happened to sweep.
    assert retired[0]["finished_at"] == clock
    assert staff.agent_state(retired[0], now=clock + 2000,
                             fresh_within=60.0) == staff.ENDED
    still_open = await inspector.fetchrow(
        "SELECT finished_at FROM staff.agents WHERE id = $1", fresh["id"])
    assert still_open["finished_at"] is None


async def test_a_child_still_reporting_work_is_not_swept_at_the_timeout(
        a_reporting_mind, connected, clock):
    """Keyed on the start alone, a child reporting every minute would be
    retired mid-report."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind,
        now=clock + 1790)

    retired = await staff.retire_stale_agents(now=clock + 1800,
                                              timeout=1800.0)

    assert [r for r in retired if r["mind_id"] == a_reporting_mind] == []


async def test_a_real_ending_replaces_a_swept_one_and_never_a_reported_one(
        a_reporting_mind, connected, clock):
    """The sweep's guess must give way to the truth; one report must not
    overwrite another."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    await staff.retire_stale_agents(now=clock + 1800, timeout=1800.0)

    corrected = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="ok",
        now=clock + 1805)
    again = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="error",
        now=clock + 1900)

    assert corrected["outcome"] == "ok"
    assert again["outcome"] == "ok"
    assert again["finished_at"] == clock + 1805


async def test_a_child_woken_after_it_ended_is_working_again_until_it_ends_again(
        a_reporting_mind, connected, clock):
    """dsh sends a finished child a message and it works again under the
    same id. On 2026-11-23 a builder fixing review findings was drawn as
    finished for an hour."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="ok",
        now=clock + 600)

    woken_at = clock + 600 + staff.REOPEN_AFTER_SECONDS + 1
    woken = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind, now=woken_at)

    assert staff.agent_state(woken, now=woken_at,
                             fresh_within=60.0) == staff.WORKING

    done = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind,
        outcome="error", now=woken_at + 300)

    assert done["finished_at"] == woken_at + 300
    assert done["outcome"] == "error"


async def test_the_tail_of_a_run_that_just_ended_does_not_reopen_it(
        a_reporting_mind, connected, clock):
    """The reporters are detached: word of the last thing a child did can
    land a moment after word that it stopped."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="ok",
        now=clock + 600)

    tail = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind,
        now=clock + 600 + staff.REOPEN_AFTER_SECONDS - 1)

    assert tail["finished_at"] == clock + 600
    assert tail["outcome"] == "ok"


async def test_an_ending_is_never_stamped_before_the_child_started(
        a_reporting_mind, connected, clock):
    """Two hosts, two clocks: the recorder's own is the only shared one, and
    a child that ended before it began is a record nobody can read."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock + 100)

    ended = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, now=clock)

    assert ended["finished_at"] == clock + 100


async def test_a_child_is_working_only_while_its_report_is_fresh(
        a_reporting_mind, connected, clock):
    """One activity report standing for a whole turn would spin a card for
    fifteen minutes after the child died."""
    row = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind, now=clock)

    window = 60.0
    assert staff.agent_state(row, now=clock + window - 1,
                             fresh_within=window) == staff.WORKING
    assert staff.agent_state(row, now=clock + window + 1,
                             fresh_within=window) == staff.RUNNING


async def test_a_child_no_harness_can_observe_reads_as_running_not_working(
        a_reporting_mind, connected, clock):
    """Two of the three harnesses can only say that a child began."""
    row = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)

    assert row["active_at"] is None
    assert staff.agent_state(row, now=clock + 1,
                             fresh_within=60.0) == staff.RUNNING


async def test_a_child_spawned_by_a_child_names_that_child_as_its_parent(
        a_reporting_mind, connected, clock):
    parent = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)

    child = await staff.record_agent_event(
        "d4e5f6", staff.SPAWNED, mind_id=a_reporting_mind,
        parent_agent_id="a1b2c3", now=clock)

    assert child["parent_agent_id"] == parent["id"]


async def test_a_child_whose_parent_was_never_recorded_is_still_recorded(
        a_reporting_mind, connected, clock):
    """A spawn is never delayed for the recorder, so a lost parent write is
    allowed - and must not take the whole subtree with it."""
    child = await staff.record_agent_event(
        "d4e5f6", staff.SPAWNED, mind_id=a_reporting_mind,
        parent_agent_id="never-recorded", now=clock)

    assert child["parent_agent_id"] == f"{a_reporting_mind}:never-recorded"
    assert await staff.agents(mind_id=a_reporting_mind) != []


async def test_what_ran_overnight_is_readable_by_mind_and_by_when(
        a_reporting_mind, other_mind, connected, clock):
    """A record carrying no task cannot be found by a read filtered on one,
    and "what ran overnight" is a read across every mind at once."""
    await staff.record_agent_event("a1b2c3", staff.SPAWNED,
                                   mind_id=a_reporting_mind, now=clock)
    # Ended before the window opens. A child still open is still running,
    # so it belongs in every later window by the overlap rule and could not
    # be used to show that the filter narrows anything.
    await staff.record_agent_event("a1b2c3", staff.EVENT_ENDED,
                                   mind_id=a_reporting_mind, outcome="ok",
                                   now=clock + 20)
    await staff.record_agent_event("d4e5f6", staff.SPAWNED,
                                   mind_id=other_mind, now=clock + 500)

    mine = await staff.agents(mind_id=a_reporting_mind)
    recent = [r for r in await staff.agents(since=clock + 100)
              if r["mind_id"] in (a_reporting_mind, other_mind)]

    assert [r["mind_id"] for r in mine] == [a_reporting_mind]
    assert [r["mind_id"] for r in recent] == [other_mind]


async def test_the_children_of_one_conversation_are_read_without_a_task(
        a_reporting_mind, connected, clock):
    """The page narrows by session: an unfiltered read is every child the
    hive has ever spawned."""
    await staff.record_agent_event("a1b2c3", staff.SPAWNED,
                                   mind_id=a_reporting_mind, session_id=f"{a_reporting_mind}-s1",
                                   now=clock)
    await staff.record_agent_event("d4e5f6", staff.SPAWNED,
                                   mind_id=a_reporting_mind, session_id=f"{a_reporting_mind}-s2",
                                   now=clock)

    found = await staff.agents(session_id=f"{a_reporting_mind}-s1")

    assert [r["id"] for r in found] == [f"{a_reporting_mind}:a1b2c3"]


async def test_an_event_nobody_defined_is_refused_rather_than_stored(
        a_reporting_mind, connected, clock):
    with pytest.raises(hive_data.errors.ValueRefused):
        await staff.record_agent_event("a1b2c3", "pondering",
                                       mind_id=a_reporting_mind, now=clock)


async def test_a_later_event_carrying_less_does_not_erase_what_is_known(
        a_reporting_mind, connected, inspector, clock):
    """Three harnesses send three different subsets of the same facts.

    Dsh's `finished` event names the child and its provider and nothing
    about the conversation; a working report names no parent. Each of those
    arrives as an event carrying nulls where the start carried values, and a
    record that took the newest value for every field would blank the
    conversation it belongs to and the child that spawned it - requirements
    1 and 6 failing silently, on the ordinary path rather than on a
    failure.
    """
    task = await staff.create_task("Watched work", "in progress", now=clock)
    await staff.record_agent_event(
        "d4e5f6", staff.SPAWNED, mind_id=a_reporting_mind,
        conversation_ref="conv-77", session_id="sess-9",
        parent_agent_id="a1b2c3", task_id=task["id"], now=clock)

    # The conversation travels on every event, because it is part of the
    # child's identity: a reporter always knows which conversation it is in.
    # What this event carries *less* of is the kind, the label, the parent
    # and the task.
    ended = await staff.record_agent_event(
        "d4e5f6", staff.EVENT_ENDED, mind_id=a_reporting_mind,
        conversation_ref="conv-77", outcome="ok", now=clock + 30)

    assert ended["session_id"] == "sess-9"
    assert ended["conversation_ref"] == "conv-77"
    assert ended["parent_agent_id"] == f"{a_reporting_mind}:conv-77:a1b2c3"
    assert ended["task_id"] == task["id"]
    stored = await inspector.fetchrow(
        "SELECT session_id, conversation_ref, parent_agent_id, task_id "
        "FROM staff.agents WHERE id = $1", ended["id"])
    assert dict(stored) == {"session_id": "sess-9",
                            "conversation_ref": "conv-77",
                            "parent_agent_id":
                                f"{a_reporting_mind}:conv-77:a1b2c3",
                            "task_id": task["id"]}


async def test_an_activity_stamp_in_the_future_does_not_read_as_working(
        a_reporting_mind, connected, clock):
    """A reporting host whose clock runs ahead, or one bad event.

    Bounded at both ends for the reason the page bounds its own freshness
    check: an unbounded upper reading would hold the child as working for as
    long as the drift lasted, and that is the one state the whole feature
    exists to keep honest.
    """
    window = 60.0
    row = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind,
        now=clock + 10 * window)

    assert staff.agent_state(row, now=clock,
                             fresh_within=window) == staff.RUNNING


async def test_a_delegate_reporting_late_replaces_the_sweep_s_guess(
        a_reporting_mind, connected, clock):
    """The chief's own delegates are swept on the same window as anybody's
    children, so the ending they report afterwards has to land the same way
    a harness's does. Without it, a sweep firing one tick early makes the
    truth unwritable for ever through this path too.
    """
    task = await staff.create_task("Watched work", "in progress", now=clock)
    agent = await staff.start_agent(task["id"], "chase.md", now=clock)
    await staff.retire_stale_agents(now=clock + 1800, timeout=1800.0)

    corrected = await staff.finish_agent(agent["id"], outcome="finished",
                                         now=clock + 1805)
    again = await staff.finish_agent(agent["id"], outcome="failed",
                                     now=clock + 1900)

    assert corrected is not None
    assert corrected["outcome"] == "finished"
    assert again is None, "one report must not overwrite another"


async def test_an_ending_carrying_no_kind_does_not_blank_the_one_recorded(
        a_reporting_mind, connected, clock):
    """The ordinary shape of a stop report: claude's names the agent and
    nothing about what it was for. Taking the newest value for every field
    would blank what the spawn said."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        agent_type="refute-tests", label="story 6.9.1", now=clock)

    ended = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="ok",
        now=clock + 30)

    assert ended["agent_type"] == "refute-tests"
    assert ended["about"] == "story 6.9.1"


async def test_an_activity_stamp_is_not_erased_by_a_later_quiet_event(
        a_reporting_mind, connected, clock):
    """Only a working report carries a stamp, so every other event carries a
    null one - and a record that took the newest value would forget that the
    child was ever observed working at all."""
    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind,
        now=clock + 10)

    after = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock + 20)

    assert after["active_at"] == clock + 10


async def test_two_activity_reports_out_of_order_keep_the_later_moment(
        a_reporting_mind, connected, clock):
    """The reports are detached and race, so the one that arrives second is
    not always the one that was observed second."""
    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind,
        now=clock + 90)

    older = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind,
        now=clock + 30)

    assert older["active_at"] == clock + 90


async def test_a_child_reporting_work_after_the_sweep_is_live_again(
        a_reporting_mind, connected, clock):
    """The sweep only ever guessed that the child was dead. A harness saying
    it is still producing work is the one piece of evidence that outranks
    the guess, and discarding it left working unreachable for the rest of
    that child's life."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    await staff.retire_stale_agents(now=clock + 2000, timeout=1800.0)

    alive = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind,
        now=clock + 2100)

    assert alive["finished_at"] is None
    assert alive["outcome"] == ""
    assert staff.agent_state(alive, now=clock + 2100,
                             fresh_within=60.0) == staff.WORKING


async def test_a_reported_ending_is_not_reopened_by_a_later_activity_report(
        a_reporting_mind, connected, clock):
    """Only the sweep's guess gives way. A harness that said its child was
    over has said the one thing nothing else knows better."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="ok",
        now=clock + 30)

    late = await staff.record_agent_event(
        "a1b2c3", staff.EVENT_WORKING, mind_id=a_reporting_mind,
        now=clock + 40)

    assert late["finished_at"] == clock + 30
    assert late["outcome"] == "ok"


async def test_a_reporter_may_not_send_the_machinerys_own_abandoned_outcome(
        a_reporting_mind, connected, clock):
    """It is the one ending a later report may replace, so a reporter able
    to send it could have its own first ending overwritten by its own
    second one."""
    with pytest.raises(hive_data.errors.ValueRefused):
        await staff.record_agent_event(
            "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind,
            outcome=staff.OUTCOME_ABANDONED, now=clock)


async def test_an_outcome_that_renders_as_nothing_is_refused(
        a_reporting_mind, connected, clock):
    with pytest.raises(hive_data.errors.ValueRefused):
        await staff.record_agent_event(
            "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind,
            outcome="​", now=clock)


async def test_the_stored_key_refuses_its_own_separator_in_either_half(
        connected, clock):
    """Without this the composition is not injective: mind `m` with child
    `a:b` and mind `m:a` with child `b` compose one key and merge two minds'
    children into one record."""
    with pytest.raises(hive_data.errors.ValueRefused):
        staff.agent_key("mind-one", "a:b")
    with pytest.raises(hive_data.errors.ValueRefused):
        staff.agent_key("mind:one", "ab")


async def test_what_ran_overnight_includes_a_child_that_began_before_it(
        a_reporting_mind, connected, clock):
    """A read of the last four hours matching the start alone missed the
    child that began at half past eleven and ran until six, which is
    precisely the overnight work the question is asked about."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="ok",
        now=clock + 30_000)
    await staff.record_agent_event(
        "d4e5f6", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)
    await staff.record_agent_event(
        "d4e5f6", staff.EVENT_ENDED, mind_id=a_reporting_mind, outcome="ok",
        now=clock + 50)

    recent = await staff.agents(mind_id=a_reporting_mind, since=clock + 100)

    assert [r["id"] for r in recent] == [f"{a_reporting_mind}:a1b2c3"]


async def test_a_child_of_a_conversation_still_producing_output_is_spared(
        a_reporting_mind, connected, clock):
    """The sweep's one real reading rather than a guess. A child dies with
    its turn's process, so a conversation still producing output has not
    ended its turn - whatever that mind's turn length is, and this hive's
    minds allow 900 seconds, 14400 and no bound at all."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        session_id="sess-live", now=clock)
    await staff.record_agent_event(
        "d4e5f6", staff.SPAWNED, mind_id=a_reporting_mind,
        session_id="sess-quiet", now=clock)

    retired = await staff.retire_stale_agents(
        now=clock + 20_000, timeout=1800.0, alive=["sess-live"])

    mine = [r["id"] for r in retired if r["mind_id"] == a_reporting_mind]
    assert mine == [f"{a_reporting_mind}:d4e5f6"]


async def test_a_child_of_no_conversation_is_retired_on_the_clock_alone(
        a_reporting_mind, connected, clock):
    """Nothing can be asked about it, so the grace window is all there is."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind, now=clock)

    retired = await staff.retire_stale_agents(
        now=clock + 20_000, timeout=1800.0, alive=["sess-live"])

    assert [r["id"] for r in retired
            if r["mind_id"] == a_reporting_mind] == [
        f"{a_reporting_mind}:a1b2c3"]


async def test_one_id_reused_in_a_later_conversation_is_a_second_child(
        a_reporting_mind, connected, clock):
    """A harness id is unique inside the conversation that spawned it and
    nowhere else. Keyed on the mind alone, the second child merged into the
    first child's finished record, kept the first's kind and label, and a
    reader saw no children at all in the second conversation."""
    await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        conversation_ref="conv-one", agent_type="explore", label="first",
        now=clock)
    await staff.record_agent_event(
        "a1b2c3", staff.EVENT_ENDED, mind_id=a_reporting_mind,
        conversation_ref="conv-one", outcome="ok", now=clock + 30)

    second = await staff.record_agent_event(
        "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
        conversation_ref="conv-two", agent_type="plan", label="second",
        now=clock + 60)

    assert second["finished_at"] is None
    assert second["agent_type"] == "plan"
    assert second["about"] == "second"
    assert len(await staff.agents(mind_id=a_reporting_mind)) == 2


async def test_a_child_cannot_be_its_own_parent(
        a_reporting_mind, connected, clock):
    """A self-loop is a tree no reader can draw: the node hangs off nothing
    and the walk that finds its parents does not end."""
    with pytest.raises(hive_data.errors.ValueRefused):
        await staff.record_agent_event(
            "a1b2c3", staff.SPAWNED, mind_id=a_reporting_mind,
            parent_agent_id="a1b2c3", now=clock)


# -- what a child has in flight (feature 916) --------------------------------

async def test_a_sub_agent_with_a_tool_call_in_flight_reads_working_however_long_it_runs(
        a_reporting_mind, connected, clock):
    """The ten-minute test run of 2026-11-27: one call, nothing written until
    it returns, and the child drawn as dead the whole time."""
    await staff.record_agent_event(
        "b1", staff.EVENT_DOING, mind_id=a_reporting_mind,
        tool="Bash: run the suite", tool_running_for=4,
        reports_thinking=True, reports_output=True, now=clock)
    row = (await staff.agents(mind_id=a_reporting_mind))[0]

    assert staff.agent_reading(row, now=clock + 10 * 60,
                               fresh_within=60) == staff.WORKING
    assert row["tool"] == "Bash: run the suite"
    assert row["tool_started_at"] == clock - 4


async def test_a_sub_agent_seen_thinking_after_a_long_silence_reads_working(
        a_reporting_mind, connected, clock):
    await staff.record_agent_event(
        "b2", staff.EVENT_DOING, mind_id=a_reporting_mind,
        reports_thinking=True, reports_output=True, now=clock)
    await staff.record_agent_event(
        "b2", staff.EVENT_DOING, mind_id=a_reporting_mind,
        thinking=True, reports_thinking=True, reports_output=True, now=clock + 300)
    row = (await staff.agents(mind_id=a_reporting_mind))[0]

    assert staff.agent_reading(row, now=clock + 320,
                               fresh_within=60) == staff.WORKING
    assert row["thinking_at"] == clock + 300


async def test_thirty_seconds_of_silence_with_nothing_in_flight_reads_stalled_and_just_under_does_not(
        a_reporting_mind, connected, clock):
    await staff.record_agent_event(
        "b3", staff.EVENT_DOING, mind_id=a_reporting_mind,
        reports_thinking=True, reports_output=True, now=clock)
    row = (await staff.agents(mind_id=a_reporting_mind))[0]

    assert staff.agent_reading(row, now=clock + 29.9,
                               fresh_within=60) == staff.WORKING
    assert staff.agent_reading(row, now=clock + 30,
                               fresh_within=60) == staff.STALLED


async def test_where_thinking_cannot_be_seen_silence_reads_quiet_from_thirty_seconds_and_stalled_at_two_minutes(
        a_reporting_mind, connected, clock):
    await staff.record_agent_event(
        "b4", staff.EVENT_DOING, mind_id=a_reporting_mind,
        reports_output=True, now=clock)
    row = (await staff.agents(mind_id=a_reporting_mind))[0]

    read = [staff.agent_reading(row, now=clock + after, fresh_within=60)
            for after in (29.9, 30, 119.9, 120)]

    assert read == [staff.WORKING, staff.QUIET, staff.QUIET, staff.STALLED]


async def test_a_tool_call_that_returned_no_longer_holds_the_sub_agent_working(
        a_reporting_mind, connected, clock):
    """What is in flight is said as it stands, so the report after the call
    returned replaces the one that named it."""
    await staff.record_agent_event(
        "b5", staff.EVENT_DOING, mind_id=a_reporting_mind,
        tool="Bash: run the suite", reports_thinking=True, reports_output=True, now=clock)
    await staff.record_agent_event(
        "b5", staff.EVENT_DOING, mind_id=a_reporting_mind,
        reports_thinking=True, reports_output=True, now=clock + 600)
    row = (await staff.agents(mind_id=a_reporting_mind))[0]

    assert row["tool"] == ""
    assert staff.agent_reading(row, now=clock + 631,
                               fresh_within=60) == staff.STALLED
