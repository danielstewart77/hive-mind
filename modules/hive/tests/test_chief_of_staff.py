"""The Chief of Staff tree, below the route.

Against the suite's scratch database, through the same two layers the page
uses: ledger rows written with `hive_data.staff`, board rows with
`kanban_data`. Nothing is stubbed. What is asserted on — the depth of the
tree, what leaves it for the barrel, which bubbles exist and what the root
counts — is what `chief_of_staff` built out of those rows.
"""

from __future__ import annotations

import time

import pytest

import chief_of_staff as cos
import kanban_data
import store
from hive_data import core, staff
from hive_site import testing as suite

#: The present, to the second. The board stamps when an item finished with
#: the database's own clock, which no test can fix, so the tree is read at
#: the moment it is written: a clock years away would put every finished
#: story outside the barrel's window (feature 6.11).
NOW = float(int(time.time()))

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


async def test_a_feature_whose_every_story_is_done_goes_to_the_barrel_with_its_stories():
    """A feature with nothing left under it is not a place to hang live
    children. The barrel holds finished stories, features and tasks
    (feature 6.11), so the feature goes there and its stories with it."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Done one")
    story = await kanban_data.create_story("Under it",
                                           feature_id=feature["id"])
    await kanban_data.update_story(story["id"], status="done")
    await _task("The epic", board_item=epic["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "story") == []
    assert _ids(tree["nodes"], "feature") == []
    assert sorted(_ids(tree["barrel"])) == sorted([feature["id"], story["id"]])


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


async def test_a_sub_agent_that_ended_disappears_and_does_not_go_to_the_barrel():
    """DECIDED 2026-11-14: the barrel holds finished tasks and stories only."""
    task = await _task()
    agent = await staff.start_agent(task["id"], "chase.md", now=NOW)
    await staff.finish_agent(agent["id"], outcome="waiting", now=NOW + 30)

    tree = await cos.get_tree(now=NOW + 60)

    assert agent["id"] not in _ids(tree["nodes"])
    assert agent["id"] not in _ids(tree["barrel"])


async def test_a_sub_agent_open_past_the_turn_timeout_is_drawn_as_orphaned():
    """It died with its turn's process and could not write its own ending."""
    task = await _task()
    agent = await staff.start_agent(task["id"], "chase.md", now=NOW)

    working = await cos.get_tree(now=NOW + cos.AGENT_TIMEOUT - 1)
    orphaned = await cos.get_tree(now=NOW + cos.AGENT_TIMEOUT + 1)

    assert _by_id(working, agent["id"])["status"] == "running"
    assert _by_id(orphaned, agent["id"])["status"] == "orphaned"


# ------------------------------------------------------ who and what state


async def test_a_task_names_the_minds_whose_sessions_are_working_it():
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    assert [m["name"] for m in _by_id(tree, task["id"])["minds"]] == ["Cypher"]


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


# ------------------------------------------------ what kind of work a card is
#
# Daniel's second protection: a card shows the work type of the thing it
# stands for. The kind is looked up across all three board kinds rather than
# guessed, which is what makes a deleted item draw as a task instead of
# claiming to be an epic.


async def test_a_task_covering_a_feature_is_drawn_as_a_feature():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    task = await _task(board_item=feature["id"])

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["work_kind"] == "feature"


async def test_a_task_covering_an_epic_is_drawn_as_an_epic():
    epic = await kanban_data.create_epic("Chief of Staff")
    task = await _task(board_item=epic["id"])

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["work_kind"] == "epic"


async def test_a_task_covering_a_story_is_drawn_as_a_story():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The page", feature_id=feature["id"])
    task = await _task(board_item=story["id"])

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["work_kind"] == "story"


async def test_a_task_covering_a_bug_is_drawn_as_a_bug():
    """The board offers a bug as a type of its own, so a card says so."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    bug = await kanban_data.create_story("The spinner lies",
                                         feature_id=feature["id"],
                                         subtype="bug")
    task = await _task(board_item=bug["id"])

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["work_kind"] == "bug"


async def test_a_task_naming_no_board_item_is_drawn_as_a_task():
    task = await _task()

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["work_kind"] == cos.TASK_KIND


async def test_a_task_naming_a_board_item_that_is_gone_is_drawn_as_a_task():
    """Migration 0009 says a dangling board item draws as a plain task. The
    page used to treat anything that was not a feature as an epic, so a
    deleted row claimed to be one and dragged a stranger's features under it.

    The board is seeded first and deliberately: against an empty board a
    mis-drawn epic has nothing to draw, so the assertion would hold for a
    page that had the bug.
    """
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    await kanban_data.create_story("The page", feature_id=feature["id"])
    task = await _task(board_item="w-no-such-item")

    tree = await cos.get_tree(now=NOW)

    node = _by_id(tree, task["id"])
    assert node["work_kind"] == cos.TASK_KIND
    # Nothing hangs off it: the dead id is not somebody else's epic.
    assert _ids(tree["nodes"], "feature") == []
    assert _ids(tree["nodes"], "story") == []
    assert [e for e in tree["edges"] if e["source"] == task["id"]] == []


async def test_a_story_drawn_under_its_feature_says_it_is_a_story():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The page", feature_id=feature["id"])
    await _task(board_item=feature["id"])

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, story["id"])["work_kind"] == "story"


# ------------------------------------------------------- what is really active
#
# Daniel's third protection: a card shows activity only when the hive's own
# live record says the work is running, and a task status alone never makes
# it show as active. Two conditions, because an absent stop is also what a
# stopped watch and a hung turn leave behind.


async def test_work_with_a_live_recent_session_and_no_recorded_stop_is_active():
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["active"] is True


async def test_the_watch_recording_a_stop_takes_the_activity_away():
    """The status does not move: only the live record does."""
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task(status="in progress")
    await staff.attach_session(task["id"], session, now=NOW)
    await staff.note_stopped(task["id"], now=NOW - 10)

    tree = await cos.get_tree(now=NOW)

    node = _by_id(tree, task["id"])
    assert node["status"] == "in progress"
    assert node["active"] is False


async def test_a_status_of_in_progress_cannot_by_itself_make_work_active():
    """The whole of Daniel's protection in one case: the status says the work
    is in progress and nothing has been dispatched for it."""
    task = await _task(status="in progress")

    tree = await cos.get_tree(now=NOW)

    node = _by_id(tree, task["id"])
    assert node["status"] == "in progress"
    assert node["active"] is False


async def test_a_session_silent_for_twenty_minutes_is_not_active():
    """The case the watch cannot report. `stopped_at` is still null — a hung
    turn is one the watch does not notice, and a stopped watch never writes
    again — so on that record alone this card would spin forever.

    Twenty minutes is longer than any turn this hive allows, and it is spelled
    out rather than read off `cos.LIVE_WITHIN`: a test that computes its clock
    from the constant moves with it, so a window narrowed to one second would
    pass while every card stopped spinning a second after its last output.
    """
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task(status="in progress")
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW + 20 * 60)

    node = _by_id(tree, task["id"])
    assert node["activity"] == "working"
    assert node["active"] is False


async def test_a_session_silent_for_five_minutes_is_still_active():
    """The other side of the bound, and the half that pins it from below: a
    turn is routinely quiet for minutes, so five minutes must still read as
    inside one."""
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW + 5 * 60)

    assert _by_id(tree, task["id"])["active"] is True


async def test_work_whose_sessions_have_all_ended_is_not_active():
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)
    await core.delete_session(session)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["active"] is False


async def test_a_delegate_known_only_to_be_unfinished_reads_as_running():
    """Nothing reports a delegate's ongoing work, so it is running and does
    not spin."""
    task = await _task()
    agent = await staff.start_agent(task["id"], "refute-edges", now=NOW)

    tree = await cos.get_tree(now=NOW)

    node = _by_id(tree, agent["id"])
    assert node["status"] == "running"
    assert node["active"] is False


# ------------------------------------------- whose session name a card carries
#
# Daniel's fourth protection: the session name appears on the item the
# session was dispatched to, and on none of that item's children.


async def _named_session(mind_id, session_id="sess-named", name="Visualization"):
    await core.create_session(id=session_id, surface="telegram",
                              surface_id="chat-1", mind_id=mind_id,
                              created_at=NOW, last_active=NOW, name=name)
    return session_id


async def test_the_assigned_item_carries_its_session_s_name():
    mind = await _mind("Cypher")
    session = await _named_session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["session_names"] == ["Visualization"]


async def test_the_session_name_is_on_the_assigned_item_and_not_its_children():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The page", feature_id=feature["id"])
    mind = await _mind("Cypher")
    session = await _named_session(mind)
    task = await _task(board_item=feature["id"])
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    parent = _by_id(tree, task["id"])
    child = _by_id(tree, story["id"])
    assert parent["session_names"] == ["Visualization"]
    assert child.get("session_names", []) == []
    # The same rule, for the other two things an assignment carries. A story
    # drawn under an assigned feature is not itself assigned to anybody, so it
    # inherits neither the mind's face nor the spinner.
    assert parent["minds"] != []
    assert child.get("minds", []) == []
    assert parent["active"] is True
    assert child["active"] is False


async def test_a_session_with_no_name_contributes_no_name():
    """And never its id in place of one: the id is the thing Daniel asked to
    stop seeing, so a card with no name carries nothing rather than a
    fallback."""
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["session_names"] == []


async def test_a_session_name_is_cut_to_what_a_card_can_hold():
    """Spelled out rather than read off `cos.NAME_LIMIT`, which a cap of 500
    would otherwise satisfy. A card is 174 pixels wide; 80 characters is
    already more than it can draw."""
    mind = await _mind("Cypher")
    session = await _named_session(mind, name="x" * 300)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    kept = _by_id(tree, task["id"])["session_names"][0]
    assert kept == "x" * cos.NAME_LIMIT
    assert len(kept) <= 80


# ------------------------------------------------------------ what a tile says
#
# Daniel, 2026-11-21: a tile says the work's whole-number item ID first - the
# ID column of the backlog, not its place in the order - then a very few
# words of its name, and nothing else.


async def test_a_story_tile_says_its_item_id_then_its_name():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The tree",
                                           feature_id=feature["id"])
    await _task("Chief of staff page", board_item=feature["id"])

    tree = await cos.get_tree(now=NOW)

    number = story["item_number"]
    assert isinstance(number, int)
    assert _by_id(tree, story["id"])["tile"] == f"{number}\nThe tree"


async def test_a_task_on_a_board_item_is_called_by_that_items_id_and_name():
    """The ID and the words beside it belong to the same thing, and the
    session on the work is not a third thing on the tile."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    mind = await _mind("Cypher")
    session = await _named_session(mind, name="Late shift")
    task = await _task("Watch the page get built", board_item=feature["id"])
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    number = feature["item_number"]
    assert _by_id(tree, task["id"])["tile"] == f"{number}\nVisualization"


async def test_a_task_on_a_board_task_is_called_by_that_tasks_id():
    item = await kanban_data.create_task("Tidy the tiles")
    task = await _task("Ledger name for it", board_item=item["id"])

    tree = await cos.get_tree(now=NOW)

    number = item["item_number"]
    assert _by_id(tree, task["id"])["tile"] == f"{number}\nTidy the tiles"


async def test_a_tile_keeps_only_the_first_few_words_of_a_long_name():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story(
        "Show the id and then a short name on every tile of the board",
        feature_id=feature["id"])
    await _task("Chief of staff page", board_item=feature["id"])

    tree = await cos.get_tree(now=NOW)

    number = story["item_number"]
    assert _by_id(tree, story["id"])["tile"] == (
        f"{number}\nShow the id and\u2026")


async def test_work_with_no_place_on_the_board_says_only_its_few_words():
    task = await _task("Fix the printer")

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["tile"] == "Fix the printer"


async def test_a_name_that_is_one_long_word_is_cut_to_what_a_tile_can_hold():
    """Spelled out rather than read off `cos.TILE_LIMIT`: a tile's text
    column holds about twenty characters a line."""
    task = await _task("x" * 60)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["tile"] == "x" * 23 + "\u2026"


# ------------------------------------------------------------- the chief's own
#
# The root is a mind too, and Daniel's first protection is about a mind that
# has a picture showing it. The root's label is a display name rather than a
# registry row, so it is resolved by name.


async def test_an_assigned_card_carries_what_the_page_fetches_its_face_with():
    """Daniel's first protection, on the card rather than on the route. The
    page builds `/api/minds/<mind_id>/image?v=<version>`, so a card carrying
    only a name and a letter is a card no picture ever reaches — and the
    version is the whole reason the card computes anything, because one fixed
    URL per mind is one a browser is entitled to keep serving stale.
    """
    mind = await _mind("Cypher")
    session = await _session(mind)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    face = _by_id(tree, task["id"])["minds"][0]
    assert face["mind_id"] == mind
    assert "image_version" in face


async def test_the_root_carries_the_chief_s_own_face():
    """A second mind is registered on purpose: with one row in the registry
    this passes for a lookup that returns whichever mind came first, and on a
    real hive the root would wear a stranger's face. `_mind` names them so
    that "Ada" sorts ahead of "stash", which is the order `list_minds` uses.
    """
    await _mind("Ada")
    await _mind("stash")

    tree = await cos.get_tree(chief="Stash", now=NOW)

    root = _by_id(tree, cos.CHIEF_ID)
    assert [m["name"] for m in root["minds"]] == ["stash"]
    assert root["minds"][0]["initial"] == "S"


async def test_chief_is_blank_until_a_mind_holds_the_chief_of_staff_role():
    await _mind("stash")
    chosen = await _mind("Smith")
    tree = await cos.get_tree(now=NOW)
    assert _by_id(tree, cos.CHIEF_ID)["label"] == ""
    assert "minds" not in _by_id(tree, cos.CHIEF_ID)

    role = await store.create_role("Chief of Staff", slug="chief-of-staff")
    await store.set_mind_roles(chosen, [role])
    tree = await cos.get_tree(now=NOW)
    root = _by_id(tree, cos.CHIEF_ID)
    assert root["label"] == "Smith"
    assert [face["mind_id"] for face in root["minds"]] == [chosen]

    await store.set_mind_roles(chosen, [])
    tree = await cos.get_tree(now=NOW)
    assert _by_id(tree, cos.CHIEF_ID)["label"] == ""
    assert "minds" not in _by_id(tree, cos.CHIEF_ID)


async def test_a_chief_who_is_not_a_registered_mind_carries_no_face():
    tree = await cos.get_tree(chief="Nobody", now=NOW)

    assert "minds" not in _by_id(tree, cos.CHIEF_ID)


async def test_a_card_names_the_mind_that_has_the_work_now():
    """Work handed from one mind to another draws the mind that has it.

    The pairing table is ordered by when a session was attached, so drawing
    the first of it showed whoever *started* the work — a card stating the
    wrong owner, with no mark to say it was one of several, for as long as
    the original session row survived. "Who has it" is one of the three
    things a card exists to say.
    """
    first = await _mind("Cypher")
    second = await _mind("Nagatha")
    started = await _named_session(first, "sess-first", "First pass")
    took_over = await _named_session(second, "sess-second", "Second pass")
    await core.update_session(started, last_active=NOW - 3600)
    await core.update_session(took_over, last_active=NOW)

    task = await _task()
    await staff.attach_session(task["id"], started, now=NOW - 3600)
    await staff.attach_session(task["id"], took_over, now=NOW)

    tree = await cos.get_tree(now=NOW)

    node = _by_id(tree, task["id"])
    assert [m["name"] for m in node["minds"]] == ["Nagatha", "Cypher"]
    assert node["session_names"][0] == "Second pass"


async def test_a_session_whose_clock_runs_ahead_does_not_spin_forever():
    """A `last_active` in the future would otherwise read as fresh for as
    long as the drift lasted, leaving the watch's record as the only thing
    holding the spinner back — which is the one condition that was never
    enough on its own."""
    mind = await _mind("Cypher")
    session = await _named_session(mind, "sess-ahead", "Ahead")
    await core.update_session(session, last_active=NOW + 48 * 3600)
    task = await _task()
    await staff.attach_session(task["id"], session, now=NOW)

    tree = await cos.get_tree(now=NOW)

    assert _by_id(tree, task["id"])["active"] is False


# ------------------------------------------- what harnesses report (6.9.5)
#
# Protected behaviours 3 and 4: a sub-agent is drawn as working only while a
# harness has genuinely reported ongoing work, and it is placed beneath its
# own parent or not at all - never beneath work it cannot be tied to.

MIND = "mind-reporter"
CONVERSATION = "conv-reported"


async def _reported(agent_id, state="spawned", *, session_id="sess-rep",
                    parent=None, kind="refute-edges", label="", now=NOW):
    return await staff.record_agent_event(
        agent_id, state, mind_id=MIND, agent_type=kind, label=label,
        conversation_ref=CONVERSATION, session_id=session_id,
        parent_agent_id=parent, now=now)


async def _paired_task(session_id="sess-rep", name="Some work"):
    """A live task whose conversation is still open, so comms has its row."""
    task = await _task(name)
    await _session(await _mind(), session_id)
    await staff.attach_session(task["id"], session_id, now=NOW)
    return task


async def test_a_reported_child_hangs_beneath_the_task_its_conversation_works():
    task = await _paired_task()
    child = await _reported("a1")

    tree = await cos.get_tree(now=NOW)

    assert child["id"] in _ids(tree["nodes"], "agent")
    assert {"source": task["id"], "target": child["id"]} in tree["edges"]


async def test_a_child_is_drawn_beneath_its_own_task_and_not_another():
    mine = await _paired_task("sess-mine", name="Mine")
    other = await _paired_task("sess-other", name="Other")
    child = await _reported("a1", session_id="sess-mine")

    tree = await cos.get_tree(now=NOW)

    assert {"source": mine["id"], "target": child["id"]} in tree["edges"]
    assert {"source": other["id"], "target": child["id"]} not in tree["edges"]


async def test_a_child_reported_working_spins_and_stops_when_the_report_ages():
    await _paired_task()
    await _reported("a1")
    child = await _reported("a1", "working", now=NOW + 10)

    # Three page refreshes of twenty seconds: a single report must not spin
    # a card for longer than the page itself trusts a payload.
    fresh = _by_id(await cos.get_tree(now=NOW + 10 + 3 * 20 - 1), child["id"])
    aged = _by_id(await cos.get_tree(now=NOW + 10 + 3 * 20 + 1), child["id"])

    assert (fresh["status"], fresh["active"]) == ("working", True)
    assert (aged["status"], aged["active"]) == ("running", False)


async def test_a_child_nobody_reported_working_is_running_and_does_not_spin():
    await _paired_task()
    child = await _reported("a1")

    node = _by_id(await cos.get_tree(now=NOW + 1), child["id"])

    assert (node["status"], node["active"]) == ("running", False)


async def test_a_child_spawned_by_a_child_hangs_beneath_that_child():
    task = await _paired_task()
    parent = await _reported("p1")
    child = await _reported("c1", parent="p1")

    tree = await cos.get_tree(now=NOW)

    assert {"source": parent["id"], "target": child["id"]} in tree["edges"]
    assert {"source": task["id"], "target": child["id"]} not in tree["edges"]


async def test_a_child_whose_parent_was_never_recorded_is_not_drawn():
    await _paired_task()
    child = await _reported("c1", parent="never-reported")

    tree = await cos.get_tree(now=NOW)

    assert child["id"] not in _ids(tree["nodes"])


async def test_a_child_whose_parent_ended_hangs_beneath_the_nearest_live_work():
    task = await _paired_task()
    grand = await _reported("g1")
    await _reported("p1", parent="g1")
    await _reported("p1", "ended", parent="g1", now=NOW + 5)
    child = await _reported("c1", parent="p1")

    tree = await cos.get_tree(now=NOW + 10)

    assert {"source": grand["id"], "target": child["id"]} in tree["edges"]
    assert {"source": task["id"], "target": child["id"]} not in tree["edges"]


async def test_children_whose_parents_loop_are_not_drawn():
    await _paired_task()
    first = await _reported("x1", parent="x2")
    second = await _reported("x2", parent="x1")

    tree = await cos.get_tree(now=NOW)

    assert first["id"] not in _ids(tree["nodes"])
    assert second["id"] not in _ids(tree["nodes"])


async def test_a_child_beneath_a_parent_that_cannot_be_placed_is_not_drawn():
    await _paired_task()
    parent = await _reported("p1", parent="never-reported")
    child = await _reported("c1", parent="p1")

    tree = await cos.get_tree(now=NOW)

    assert parent["id"] not in _ids(tree["nodes"])
    assert child["id"] not in _ids(tree["nodes"])


async def test_a_child_whose_ended_ancestors_loop_is_not_drawn():
    await _paired_task()
    await _reported("x1", "ended", parent="x2")
    await _reported("x2", "ended", parent="x1")
    child = await _reported("c1", parent="x1")

    tree = await cos.get_tree(now=NOW)

    assert child["id"] not in _ids(tree["nodes"])


async def test_a_reported_child_whose_conversation_has_ended_is_orphaned():
    """A child cannot outlive its conversation, and comms deletes the
    session row when the conversation ends."""
    await _paired_task()
    child = await _reported("a1")
    await core.delete_session("sess-rep")

    node = _by_id(await cos.get_tree(now=NOW + 1), child["id"])

    assert node["status"] == "orphaned"


async def test_a_long_reported_child_in_a_live_conversation_is_still_running():
    """The chief's turn timeout is not the reporting mind's, so the clock
    alone does not orphan a reported child."""
    await _paired_task()
    child = await _reported("a1")

    node = _by_id(await cos.get_tree(now=NOW + cos.AGENT_TIMEOUT + 60),
                  child["id"])

    assert node["status"] == "running"


# ----------------------------------------------- one chain (2026-11-15)
#
# Protected behaviour b: work under the same epic and feature is drawn as one
# chain - the feature, its stories beneath it, each story's sub-agents
# beneath that story - and nothing in a chain is drawn beside its parent.


def _parents(tree, node_id):
    return [e["source"] for e in tree["edges"] if e["target"] == node_id]


async def test_a_story_task_hangs_beneath_the_task_covering_its_feature():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The robot",
                                           feature_id=feature["id"])
    feature_task = await _task("The feature", board_item=feature["id"])
    story_task = await _paired_task("sess-rep", name="The robot")
    await staff.update_task(story_task["id"], board_item=story["id"], now=NOW)
    child = await _reported("a1")

    tree = await cos.get_tree(now=NOW)

    assert _parents(tree, feature_task["id"]) == [cos.CHIEF_ID]
    assert _parents(tree, story_task["id"]) == [feature_task["id"]]
    assert _parents(tree, child["id"]) == [story_task["id"]]
    # The story is drawn once, as the task working it.
    assert story["id"] not in _ids(tree["nodes"])


async def test_a_story_task_alone_hangs_beneath_its_feature_drawn_from_the_board():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The robot",
                                           feature_id=feature["id"])
    await kanban_data.create_story("Nobody on it", feature_id=feature["id"])
    story_task = await _task("The robot", board_item=story["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["nodes"], "feature") == [feature["id"]]
    assert _by_id(tree, feature["id"])["work_kind"] == "feature"
    assert _parents(tree, feature["id"]) == [cos.CHIEF_ID]
    assert _parents(tree, story_task["id"]) == [feature["id"]]
    # A heading for the live work, not a second copy of the board.
    assert [e["target"] for e in tree["edges"]
            if e["source"] == feature["id"]] == [story_task["id"]]


async def test_a_feature_task_beneath_an_epic_task_takes_the_feature_s_place():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The robot",
                                           feature_id=feature["id"])
    epic_task = await _task("The epic", board_item=epic["id"])
    feature_task = await _task("The feature", board_item=feature["id"])

    tree = await cos.get_tree(now=NOW)

    assert feature["id"] not in _ids(tree["nodes"])
    assert _parents(tree, feature_task["id"]) == [epic_task["id"]]
    assert _parents(tree, story["id"]) == [feature_task["id"]]


async def test_work_under_an_epic_task_hangs_beneath_it_when_its_feature_is_not_drawn():
    """A feature whose stories are all done is not drawn by its epic's
    expansion, so a live task on it, or on one of its stories, still needs
    the epic's task above it - as does a story sitting straight on the
    epic."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "All done")
    story = await kanban_data.create_story("Reopened",
                                           feature_id=feature["id"])
    await kanban_data.update_story(story["id"], status="done")
    other = await kanban_data.create_feature(epic["id"], "Also done")
    under_other = await kanban_data.create_story("Shipped",
                                                 feature_id=other["id"])
    await kanban_data.update_story(under_other["id"], status="done")
    loose = await kanban_data.create_story("On the epic", epic_id=epic["id"])
    epic_task = await _task("The epic", board_item=epic["id"])
    story_task = await _task("Reopened", board_item=story["id"])
    feature_task = await _task("Also done", board_item=other["id"])
    loose_task = await _task("On the epic", board_item=loose["id"])

    tree = await cos.get_tree(now=NOW)

    assert _parents(tree, feature["id"]) == [epic_task["id"]]
    assert _parents(tree, story_task["id"]) == [feature["id"]]
    assert _parents(tree, feature_task["id"]) == [epic_task["id"]]
    assert _parents(tree, loose_task["id"]) == [epic_task["id"]]


async def test_a_second_task_on_a_story_hangs_beside_the_first():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The robot",
                                           feature_id=feature["id"])
    feature_task = await _task("The feature", board_item=feature["id"])
    await _task("Building the robot", board_item=story["id"])
    second = await staff.create_task("A question about the robot",
                                     "in progress", now=NOW + 5)
    await staff.update_task(second["id"], board_item=story["id"], now=NOW + 5)

    tree = await cos.get_tree(now=NOW + 6)

    assert _parents(tree, second["id"]) == [feature_task["id"]]


# ------------------------------- finished work in the barrel (feature 6.11)
#
# Protected behaviours of 2026-11-16: (1) finished stories, features and
# tasks within the window appear in the barrel; (2) finished work older than
# the window is left out, and widening the window brings it back; (3) with
# no choice made, the window is seven days.

DAY = 24 * 60 * 60


async def test_finished_stories_features_and_tasks_all_appear_in_the_barrel():
    epic = await kanban_data.create_epic("Chief of Staff")
    finished = await kanban_data.create_feature(epic["id"], "All done")
    under_finished = await kanban_data.create_story(
        "Shipped first", feature_id=finished["id"])
    await kanban_data.update_story(under_finished["id"], status="done")
    going = await kanban_data.create_feature(epic["id"], "Half done")
    shipped = await kanban_data.create_story("Shipped",
                                             feature_id=going["id"])
    await kanban_data.update_story(shipped["id"], status="done")
    await kanban_data.create_story("Left", feature_id=going["id"])
    elsewhere = await kanban_data.create_feature(epic["id"], "Elsewhere")
    by_a_task = await kanban_data.create_story("Closed by its task",
                                               feature_id=elsewhere["id"])
    await kanban_data.update_story(by_a_task["id"], status="done")

    epic_task = await _task("The epic", board_item=epic["id"])
    plain = await _task("A plain finished task", status="done")
    await _task("Its story's task", status="done", board_item=by_a_task["id"])
    # A finished feature named by a finished ledger row: the row's feature
    # is what finished. Marked done on the board itself, with no stories.
    other_epic = await kanban_data.create_epic("Unwatched")
    closed_feature = await kanban_data.create_feature(other_epic["id"],
                                                      "Closed whole")
    await kanban_data.update_feature(closed_feature["id"], status="done")
    await _task("The closed feature", status="done",
                board_item=closed_feature["id"])
    # A board task, finished on the board and by its ledger row.
    board_task = await kanban_data.create_task("A board task")
    await kanban_data.update_task(board_task["id"], status="done")
    await _task("The board task", status="done", board_item=board_task["id"])
    # A ledger row closed before the board story it names: the row is the
    # finished work, so it stands for itself.
    aside = await kanban_data.create_feature(other_epic["id"], "Aside")
    still_open = await kanban_data.create_story("Closed early",
                                                feature_id=aside["id"])
    early = await _task("Closed before its story", status="done",
                        board_item=still_open["id"])
    delegate = await staff.start_agent(epic_task["id"], "chase.md", now=NOW)
    await staff.finish_agent(delegate["id"], now=NOW + 1)

    tree = await cos.get_tree(now=NOW + 2)

    assert sorted(_ids(tree["barrel"])) == sorted(
        [finished["id"], under_finished["id"], shipped["id"], by_a_task["id"],
         # Its one story is done, so it is a finished feature too.
         elsewhere["id"],
         plain["id"], closed_feature["id"], board_task["id"], early["id"]])
    kinds = {row["id"]: row["kind"] for row in tree["barrel"]}
    assert kinds[finished["id"]] == kinds[closed_feature["id"]] == "feature"
    assert kinds[plain["id"]] == kinds[early["id"]] == "task"
    assert kinds[board_task["id"]] == "task"
    assert kinds[shipped["id"]] == "story"
    assert delegate["id"] not in kinds


async def test_closing_the_task_that_watched_finished_stories_keeps_them_in_the_barrel():
    """The stories under a feature and an epic finished whether or not the
    task watching them is still open; closing it is not time passing."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "All done")
    story = await kanban_data.create_story("Shipped",
                                           feature_id=feature["id"])
    await kanban_data.update_story(story["id"], status="done")
    other = await kanban_data.create_epic("Another")
    half = await kanban_data.create_feature(other["id"], "Half done")
    shipped = await kanban_data.create_story("Shipped too",
                                             feature_id=half["id"])
    await kanban_data.update_story(shipped["id"], status="done")
    await kanban_data.create_story("Left", feature_id=half["id"])
    await _task("The feature", status="done", board_item=feature["id"])
    epic_task = await _task("The other epic", status="done",
                            board_item=other["id"])

    tree = await cos.get_tree(now=NOW)

    assert sorted(_ids(tree["barrel"])) == sorted(
        [feature["id"], story["id"], shipped["id"], epic_task["id"]])


async def test_finished_work_older_than_the_window_is_left_out_until_it_is_widened():
    old = await staff.create_task("Finished a while ago", "in progress",
                                  now=NOW - 20 * DAY)
    await staff.update_task(old["id"], status="done", now=NOW - 20 * DAY)
    recent = await staff.create_task("Finished yesterday", "in progress",
                                     now=NOW - DAY)
    await staff.update_task(recent["id"], status="done", now=NOW - DAY)
    # A later edit to the old one is not a later finish.
    await staff.update_task(old["id"], question="anything left?", now=NOW)
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "The page")
    story = await kanban_data.create_story("Shipped today",
                                           feature_id=feature["id"])
    await kanban_data.update_story(story["id"], status="done")
    await kanban_data.create_story("Left", feature_id=feature["id"])
    await _task("The feature", board_item=feature["id"])

    narrow = await cos.get_tree(now=NOW, window_days=7)
    wide = await cos.get_tree(now=NOW, window_days=30)
    # Read ten days on, the story the board finished today is old too.
    later = await cos.get_tree(now=NOW + 10 * DAY, window_days=7)
    later_wide = await cos.get_tree(now=NOW + 10 * DAY, window_days=30)

    assert _ids(narrow["barrel"]) == [story["id"], recent["id"]]
    # Newest finished first.
    assert _ids(wide["barrel"]) == [story["id"], recent["id"], old["id"]]
    assert _ids(later["barrel"]) == []
    assert _ids(later_wide["barrel"]) == [story["id"], recent["id"],
                                          old["id"]]
    # Left out is not gone: the ledger and the board still hold all three.
    assert (await staff.task(old["id"]))["status"] == "done"
    assert (await kanban_data.find_item(story["id"]))["status"] == "done"


async def test_with_no_window_chosen_the_barrel_reaches_back_seven_days():
    inside = await staff.create_task("Six days and change", "done",
                                     now=NOW - 7 * DAY + 60)
    outside = await staff.create_task("Seven days and change", "done",
                                      now=NOW - 7 * DAY - 60)

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["barrel"]) == [inside["id"]]
    assert outside["id"] not in _ids(tree["barrel"])


async def test_a_live_task_on_a_story_the_board_marks_done_stays_live_work():
    """The ledger is the authority on whether a task is over."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The robot",
                                           feature_id=feature["id"])
    await kanban_data.update_story(story["id"], status="done")
    feature_task = await _task("The feature", board_item=feature["id"])
    story_task = await _task("The robot", board_item=story["id"])

    tree = await cos.get_tree(now=NOW)

    assert _ids(tree["barrel"]) == []
    assert _parents(tree, story_task["id"]) == [feature_task["id"]]


# ------------------------------- a story working through its child (2026-11-15)
#
# Protected behaviour a: a story with a working sub-agent beneath it shows as
# working, and a working sub-agent shows its spinner.


async def _story_task_with_a_child():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The robot",
                                           feature_id=feature["id"])
    await _task("The feature", board_item=feature["id"])
    story_task = await _paired_task("sess-rep", name="The robot")
    await staff.update_task(story_task["id"], board_item=story["id"], now=NOW)
    # The watch saw the story's own conversation stop, so whatever spins it
    # is its child and not itself.
    await staff.note_stopped(story_task["id"], now=NOW)
    await _reported("p1")
    child = await _reported("c1", parent="p1")
    return story_task, child


async def test_a_story_with_a_working_child_beneath_it_is_working():
    story_task, child = await _story_task_with_a_child()
    await _reported("c1", "working", parent="p1", now=NOW + 10)

    tree = await cos.get_tree(now=NOW + 11)

    # Lent to the story only: a feature's state is its stories'.
    feature_task = _parents(tree, story_task["id"])[0]
    assert _by_id(tree, feature_task)["active"] is False
    assert _by_id(tree, child["id"])["active"] is True
    assert _by_id(tree, story_task["id"])["active"] is True


async def test_a_story_whose_child_only_runs_shows_what_it_showed_before():
    story_task, child = await _story_task_with_a_child()
    await _reported("c1", "working", parent="p1", now=NOW + 10)

    aged = await cos.get_tree(now=NOW + 10 + cos.FRESH_WITHIN + 1)

    assert _by_id(aged, child["id"])["active"] is False
    assert _by_id(aged, story_task["id"])["active"] is False


async def test_the_task_opened_first_holds_the_chain_whichever_moved_last():
    """Two tasks on one feature: the chain must not jump between them each
    time the watch touches one."""
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The robot",
                                           feature_id=feature["id"])
    first = await staff.create_task("The work", "in progress", now=NOW)
    await staff.update_task(first["id"], board_item=feature["id"], now=NOW)
    second = await staff.create_task("A question", "in progress", now=NOW + 5)
    await staff.update_task(second["id"], board_item=feature["id"],
                            now=NOW + 5)
    await staff.note_stopped(second["id"], now=NOW + 10)
    await staff.update_task(second["id"], name="A question, moved", now=NOW + 10)

    tree = await cos.get_tree(now=NOW + 11)

    assert _parents(tree, story["id"]) == [first["id"]]
    assert _parents(tree, second["id"]) == [cos.CHIEF_ID]


async def test_a_story_two_tasks_are_on_works_on_both_when_one_has_a_working_child():
    epic = await kanban_data.create_epic("Chief of Staff")
    feature = await kanban_data.create_feature(epic["id"], "Visualization")
    story = await kanban_data.create_story("The robot",
                                           feature_id=feature["id"])
    watching = await _task("Watching the robot", board_item=story["id"])
    working = await _paired_task("sess-rep", name="Building the robot")
    await staff.update_task(working["id"], board_item=story["id"], now=NOW)
    await staff.note_stopped(working["id"], now=NOW)
    await _reported("c1")
    await _reported("c1", "working", now=NOW + 10)

    tree = await cos.get_tree(now=NOW + 11)

    assert _by_id(tree, watching["id"])["active"] is True
    assert _by_id(tree, working["id"])["active"] is True
