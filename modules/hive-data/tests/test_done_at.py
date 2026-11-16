"""When a piece of work finished (step 15, story 6.11.1).

The Chief of Staff barrel hides finished work by how long ago it finished,
so the moment a status moved into done is recorded by the database on every
board item and every ledger task. Read back with a plain driver connection,
for the reason `test_board.py` gives.
"""

from __future__ import annotations

import uuid

from hive_data import board, staff


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


async def _board_done_at(inspector, item_id: str):
    return await inspector.fetchval(
        "SELECT done_at FROM board.work_items WHERE id = $1", item_id)


async def test_a_board_item_records_when_it_finished_and_keeps_it_through_edits(
        connected, inspector):
    story = _id("s")
    await board.create_work_item(story, "story", "a story", 1,
                                 status="in_progress")
    assert await _board_done_at(inspector, story) is None

    before = await inspector.fetchval("SELECT extract(epoch FROM now())")
    await board.update_work_item(story, {"status": "done"})
    finished = await _board_done_at(inspector, story)
    assert finished is not None and finished >= before

    # An edit to a finished item is not a second finish.
    await board.update_work_item(story, {"title": "renamed", "status": "done"})
    assert await _board_done_at(inspector, story) == finished
    # The layer's own read carries it, which is what the page counts from.
    assert (await board.work_item(story))["done_at"] == finished


async def test_reopening_a_board_item_clears_when_it_finished(connected,
                                                              inspector):
    feature = _id("f")
    await board.create_work_item(feature, "feature", "a feature", 1,
                                 status="done")
    assert await _board_done_at(inspector, feature) is not None

    await board.update_work_item(feature, {"status": "in_progress"})
    assert await _board_done_at(inspector, feature) is None


async def test_a_ledger_task_finishes_at_the_moment_its_status_moved_to_done(
        connected, inspector):
    task = await staff.create_task("ship it", "in progress", now=1_000.0)
    assert task["done_at"] is None

    await staff.update_task(task["id"], status="done", now=2_000.0)
    # A later edit - a question cleared, a stop marked - moves updated_at
    # and must not move the finish.
    await staff.update_task(task["id"], question="anything?", now=3_000.0)
    await staff.note_stopped(task["id"], now=4_000.0)
    row = await inspector.fetchrow(
        "SELECT done_at, updated_at FROM staff.tasks WHERE id = $1",
        task["id"])
    assert row["done_at"] == 2_000.0
    assert row["updated_at"] == 4_000.0

    await staff.update_task(task["id"], status="in progress", now=5_000.0)
    assert (await staff.task(task["id"]))["done_at"] is None
