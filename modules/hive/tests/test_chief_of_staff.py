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


async def test_a_sub_agent_is_never_drawn_as_active():
    """A child's own status says "working" for as long as it is unfinished,
    and that is not the live record a spinner is allowed to read."""
    task = await _task()
    agent = await staff.start_agent(task["id"], "refute-edges", now=NOW)

    tree = await cos.get_tree(now=NOW)

    node = _by_id(tree, agent["id"])
    assert node["status"] == "working"
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
