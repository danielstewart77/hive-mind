"""The Chief of Staff tree, below the route.

Against the suite's scratch database, through the same two layers the page
uses: ledger rows written with `hive_data.staff`, board rows with
`kanban_data`. Nothing is stubbed. What is asserted on — the depth of the
tree, what leaves it for the barrel, which bubbles exist and what the root
counts — is what `chief_of_staff` built out of those rows.
"""

from __future__ import annotations

import pytest

import chief_of_staff as cos
import kanban_data
from hive_data import core, staff
from hive_site import testing as suite

NOW = 1_800_000_000.0

#: The statuses the schema seeds, re-seeded here because the suite truncates
#: every staff table between tests and a status is a row like any other.
STATUSES = ("in progress", "waiting on response", "on hold", "done",
            "with Daniel")


async def _seed_statuses():
    for name in STATUSES:
        await staff.add_status(name)


@pytest.fixture(autouse=True)
def _statuses():
    suite.run(_seed_statuses())


async def _mind(name="Cypher"):
    mind_id = f"mind-{name}"
    await core.upsert_mind(mind_id=mind_id, name=name,
                           gateway_url="http://localhost:8426", model="m",
                           harness="dsh", registered_at=NOW, last_seen=NOW)
    return mind_id


async def _session(mind_id, session_id="sess-1"):
    await core.create_session(id=session_id, surface="telegram",
                              surface_id="chat-1", mind_id=mind_id,
                              created_at=NOW, last_active=NOW)
    return session_id


async def _task(name="Some work", status="in progress", **extra):
    row = await staff.create_task(name, status, now=NOW)
    if extra:
        row = await staff.update_task(row["id"], now=NOW, **extra)
    return row


def _ids(rows, kind=None):
    return [r["id"] for r in rows if kind is None or r["kind"] == kind]


def _by_id(tree, node_id):
    return next(n for n in tree["nodes"] if n["id"] == node_id)


# ------------------------------------------------------------ the depth


async def test_a_task_naming_no_board_item_is_a_single_node():
    task = await _task("Fix the printer")

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "task") == [task["id"]]
    assert _ids(tree["nodes"], "story") == []
    assert {"source": cos.CHIEF_ID, "target": task["id"]} in tree["edges"]


async def test_a_task_that_is_a_feature_draws_a_child_per_story():
    """The depth Daniel asked for has no source but the board item."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    first = await kanban_data.create_story("The tree",
                                           feature_id=feature["id"])
    second = await kanban_data.create_story("The barrel",
                                            feature_id=feature["id"])
    task = await _task("Chief of staff page", board_item=feature["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "story") == [first["id"], second["id"]]
    assert {"source": task["id"], "target": first["id"]} in tree["edges"]
    numbers = await kanban_data.backlog_numbers()
    assert _by_id(tree, first["id"])["label"] == (
        f"{numbers[first['id']]} The tree")


async def test_a_task_that_is_an_epic_draws_children_with_children():
    epic = await kanban_data.create_epic("Chief of Staff")
    elsewhere = await kanban_data.create_epic("Something else")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    await kanban_data.create_feature(elsewhere["id"], "Not ours")
    story = await kanban_data.create_story("The tree",
                                           feature_id=feature["id"])
    task = await _task("Chief of Staff", board_item=epic["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "feature") == [feature["id"]]
    assert {"source": task["id"], "target": feature["id"]} in tree["edges"]
    assert {"source": feature["id"], "target": story["id"]} in tree["edges"]


async def test_the_same_board_item_under_two_tasks_is_drawn_once():
    """A repeated id is the same node, and a graph handed one twice draws
    neither reliably."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The tree", feature_id=feature["id"])
    await _task("Build it", board_item=feature["id"])
    await _task("Answer about it", board_item=feature["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "story") == [story["id"]]
    assert len(tree["edges"]) == len({(e["source"], e["target"])
                                      for e in tree["edges"]})


# ----------------------------------------------------------- the barrel


async def test_a_finished_task_leaves_the_tree_for_the_barrel():
    """Daniel's instruction: the child disappears and goes to the right."""
    done = await _task("Shipped", status="done")
    live = await _task("Still going")

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "task") == [live["id"]]
    assert _ids(tree["barrel"]) == [done["id"]]


async def test_a_finished_story_leaves_the_tree_for_the_barrel():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    shipped = await kanban_data.create_story("Shipped",
                                             feature_id=feature["id"])
    await kanban_data.update_story(shipped["id"], status="done")
    next_up = await kanban_data.create_story("Next", feature_id=feature["id"])
    await _task("The feature", board_item=feature["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "story") == [next_up["id"]]
    assert _ids(tree["barrel"], "story") == [shipped["id"]]


async def test_a_feature_whose_every_story_is_done_leaves_the_tree():
    """Only a story carries a status, so a finished feature is one with
    nothing left under it, and it is not a place to hang live children."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Done one")
    story = await kanban_data.create_story("Under it",
                                           feature_id=feature["id"])
    await kanban_data.update_story(story["id"], status="done")
    await _task("The epic", board_item=epic["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "story") == []
    assert _ids(tree["barrel"], "feature") == [feature["id"]]


async def test_a_feature_with_one_story_left_stays_in_the_tree():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Half done")
    shipped = await kanban_data.create_story("Shipped",
                                             feature_id=feature["id"])
    await kanban_data.update_story(shipped["id"], status="done")
    left = await kanban_data.create_story("Left", feature_id=feature["id"])
    await _task("The epic", board_item=epic["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "feature") == [feature["id"]]
    assert _ids(tree["nodes"], "story") == [left["id"]]


async def test_a_feature_with_no_stories_yet_stays_in_the_tree():
    """Nothing done is not everything done; a feature awaiting its stories
    is work that has not started, not work that is over."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Unwritten")
    await _task("The epic", board_item=epic["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "feature") == [feature["id"]]
    assert _ids(tree["barrel"], "feature") == []


# ---------------------------------------------------------- the bubbles


async def test_a_task_holding_a_question_keeps_its_place_and_carries_it():
    """Still being worked and still owed an answer is one node, not two."""
    task = await _task("Judging", question="Which model judges?")

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["question"] == "Which model judges?"
    assert tree["questions"] == [{"task": task["id"], "name": "Judging",
                                  "question": "Which model judges?"}]


async def test_the_root_counts_every_answer_the_hive_is_waiting_on():
    """The number Daniel reads the page for."""
    await _task("One", question="Which model?")
    await _task("Two", question="Which repo?")
    await _task("Three")

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, cos.CHIEF_ID)["questions"] == 2


async def test_a_finished_task_takes_its_question_with_it():
    """An answer owed on work that is over is not owed."""
    await _task("Shipped", status="done", question="Which?")

    tree = await cos.get_tree(now=NOW)

    assert tree["questions"] == []
    assert _by_id(tree, cos.CHIEF_ID)["questions"] == 0


# --------------------------------------------------------- the delegates


async def test_a_sub_agent_still_working_hangs_off_the_task_it_works():
    task = await _task()
    agent = await staff.start_agent(task["id"], "read-and-judge.md",
                                    about="session 1a2b", now=NOW)

    tree = await cos.get_tree(now=NOW + 60)

    assert _ids(tree["nodes"], "agent") == [agent["id"]]
    assert {"source": task["id"], "target": agent["id"]} in tree["edges"]
    assert _by_id(tree, agent["id"])["about"] == "session 1a2b"


async def test_a_sub_agent_that_reported_goes_to_the_barrel():
    task = await _task()
    agent = await staff.start_agent(task["id"], "chase.md", now=NOW)
    await staff.finish_agent(agent["id"], outcome="waiting", now=NOW + 30)

    tree = await cos.get_tree(now=NOW + 60)

    assert _ids(tree["nodes"], "agent") == []
    assert _ids(tree["barrel"], "agent") == [agent["id"]]


async def test_a_sub_agent_open_past_the_turn_timeout_is_drawn_as_orphaned():
    """It died with its turn's process and could not write its own ending."""
    task = await _task()
    agent = await staff.start_agent(task["id"], "chase.md", now=NOW)

    working = await cos.get_tree(now=NOW + cos.AGENT_TIMEOUT - 1)
    orphaned = await cos.get_tree(now=NOW + cos.AGENT_TIMEOUT + 1)

    assert _by_id(working, agent["id"])["status"] == "working"
    assert _by_id(orphaned, agent["id"])["status"] == "orphaned"


# ------------------------------------------------------ who and what state


async def test_a_task_names_the_minds_whose_sessions_are_working_it():
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["minds"] == ["Cypher"]


async def test_a_task_whose_sessions_have_all_ended_reads_as_ended():
    """Comms deletes the session row when a conversation does; the pair
    outlives it, and that is how an ended delegate is told from a live one."""
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)
    await core.delete_session(session)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["activity"] == "ended"


async def test_a_task_nothing_was_ever_dispatched_for_is_not_read_as_ended():
    """Drawing the two alike reports work as finished that never began."""
    task = await _task()

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["activity"] == "unassigned"


async def test_a_task_the_watch_saw_stop_reads_as_stopped_not_working():
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)
    await staff.note_stopped(task["id"], now=NOW - 10)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["activity"] == "stopped"
