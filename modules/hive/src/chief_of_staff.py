"""The Chief of Staff page: a live tree of what the hive is working on.

Daniel's shape, 2026-11-12. The chief of staff is the root. Beneath him one
node per ledger task, and the node's depth follows the work's own shape: a
task that is just a task is a single node, a task that names a board feature
is a node with one child per story, and a task that names an epic is a node
whose children have children. What finishes leaves the tree rather than
greying inside it - it goes to the barrel on the right - so the tree is only
what is actually being worked, which is the point of the page. A node still
being worked that holds a question for Daniel keeps its place and carries a
bubble, and every bubble is counted at the root, because the number of
answers the hive is waiting on is the one thing he reads the page for.

Each node also says three things about itself that the page draws in the
corners of its card: what kind of work it is, which mind is assigned to it,
and whether it is actually being worked right now. All three are decided
here rather than on the page, because all three are reads of the hive's own
rows and the page cannot check any of them.

**What "assigned" means.** The mind working a task is the mind of a session
dispatched to it. There is no assigned-mind column and deliberately so:
`staff.attach_session` already is the act of assignment, and a second place
to record it would be a second answer to the same question. The consequence
is named rather than hidden: comms deletes a `core.sessions` row when its
conversation ends, so a task whose sessions have all ended loses both the
mind and the session name, and its card draws without them. Keeping them
would mean copying the mind and the name onto the pairing row at attach
time, which is a schema change nobody has asked for.

**What "active" means.** Two conditions, and the second is why there are
two. The watch records a stop in `staff.tasks.stopped_at` and clears it when
output starts again, so an absent stop is the watch saying this work is
running. But an absent stop is also what a *stopped watch* leaves behind,
and a hung turn is one the watch does not currently notice at all - so on
that record alone the page would spin forever on precisely the work most in
need of attention. The session's own `last_active`, which comms moves and
the watch does not, bounds it: a session silent for longer than a whole turn
timeout is not inside a turn, whatever the watch last wrote. A missing, idle
or unknown live state must not read as active, and an absence is not a
signal.

No SQL here and no second opinion about anything. The ledger comes through
`hive_data.staff`, the board through `kanban_data`, and whether a sub-agent
has outlived its turn is `staff.agent_stale`, which the ledger's own tick
and tooling read the same way.
"""

from __future__ import annotations

import time
from typing import Any

import kanban_data
import mind_images
from hive_data import core, staff

#: What a ledger task in this status is: finished, and drawn in the barrel
#: rather than in the tree.
DONE = "done"

#: A board story in this status is finished. Only a story carries a status
#: on this board - a feature's state is its stories' - so this is the one
#: word that decides what leaves the tree. The board's own word for it
#: happens to match the ledger's; they are separate vocabularies and are
#: named separately so that renaming one does not silently move the other.
BOARD_DONE = "done"

#: How long a sub-agent may be open before it reads as orphaned rather than
#: working. The mind's turn timeout: a child lives inside the turn's process,
#: so one still open past it was killed and could not write its own ending.
#: `turn_timeout_seconds` for the chief of staff, which is 900.
AGENT_TIMEOUT = 15 * 60

#: How long a session may be silent and still count as working. The same
#: turn timeout, for the same reason read from the other end: a session that
#: has produced nothing for longer than a turn may be allowed to exist, but
#: it cannot still be *inside* a turn, so no card it is on spins. This is
#: what stops a stopped watch, or a hung turn the watch has not noticed,
#: from spinning every card in the tree indefinitely.
LIVE_WITHIN = AGENT_TIMEOUT

#: The longest session name a card draws. Derived from the card rather than
#: picked: a node is 174 pixels wide with a 116-pixel text column at 11px, so
#: a line holds roughly twenty characters, and the name sits under a task
#: name that already wraps to two. Forty characters was three more wrapped
#: lines than a 62-pixel card has room for, which pushed the label out over
#: the kind icon and the face. Truncated rather than dropped - a long name is
#: still the answer to "which session", just not all of it.
NAME_LIMIT = 24

#: The chief of staff's node. One root, named rather than derived: the page
#: is about this mind and a hive with no ledger rows still draws him.
CHIEF_ID = "chief"

#: What a card draws in its upper-left corner when the work it stands for is
#: not a board item at all. Migration 0009 says in as many words that a
#: dangling `board_item` draws as a plain task, and an id that resolves to
#: nothing is indistinguishable from no id at all.
TASK_KIND = "task"


def _mind_names(minds: list[dict[str, Any]]) -> dict[str, str]:
    """Mind id to display name. `mind_id` is the key on a `core.minds` row,
    not `id`, and a session points at that."""
    return {m["mind_id"]: m.get("name") or m["mind_id"] for m in minds}


def _face(mind_id: str, name: str) -> dict[str, Any]:
    """What the page needs to draw one mind in a card's corner.

    The version is the point of this being computed here: the image lives at
    one URL per mind, which a browser is entitled to cache, so a replaced
    face would keep showing the old one. The page carries this number in the
    URL and the replacement lands immediately.
    """
    return {
        "mind_id": mind_id,
        "name": name,
        "initial": mind_images.initial_of(name),
        "image_version": mind_images.image_version(mind_id),
    }


async def _assigned(session_ids: list[str], names: dict[str, str],
                    *, now: float) -> dict[str, Any]:
    """Who is working a task, what their sessions are called, and if any live.

    A paired session with no `core.sessions` row has ended - comms deletes
    the row when a conversation does - so `alive` is what separates a task
    whose delegates are still there from one whose have all gone.

    `fresh` is the separate question of whether any of them has produced
    anything recently enough to be inside a turn. It is not derived from
    `alive`: an ended conversation leaves no row, but a *surviving* row that
    has been silent for hours is the ordinary state of most of this table.
    """
    alive = False
    fresh = False
    rows: list[dict[str, Any]] = []
    for session_id in session_ids:
        row = await core.get_session(session_id)
        if row is None:
            continue
        alive = True
        age = now - float(row["last_active"])
        # Bounded at both ends. A `last_active` in the future - a host whose
        # clock runs ahead - would otherwise read as fresh for as long as the
        # drift lasts, which quietly reduces the two conditions this module
        # is built around back to the one that was not enough.
        if -LIVE_WITHIN <= age <= LIVE_WITHIN:
            fresh = True
        rows.append(row)

    # Most recently active first. The pairing table is ordered by when a
    # session was attached, and taking the first of that draws whoever
    # started the work rather than whoever has it: a task handed from one
    # mind to another went on showing the first mind's face and the first
    # mind's session name for as long as that session row survived, stating
    # the wrong owner with complete confidence. "Who has it" is one of the
    # three things a card exists to say, and a hand-off is how work moves.
    rows.sort(key=lambda r: float(r["last_active"]), reverse=True)

    minds: list[dict[str, Any]] = []
    seen: set[str] = set()
    session_names: list[str] = []
    for row in rows:
        mind_id = row.get("mind_id") or ""
        name = names.get(mind_id)
        if name and mind_id not in seen:
            seen.add(mind_id)
            minds.append(_face(mind_id, name))
        # A session with no name contributes no name. The id is never shown
        # in its place: it is the thing Daniel asked to stop seeing, and a
        # card carrying one would be the requirement failing quietly.
        session_name = (row.get("name") or "").strip()
        if session_name:
            session_names.append(session_name[:NAME_LIMIT])
    return {"minds": minds, "alive": alive, "fresh": fresh,
            "session_names": session_names}


def _activity(task: dict[str, Any], sessions: list[str], alive: bool) -> str:
    """Whether this task is being worked, stopped, ended, or never dispatched.

    From the rows alone rather than from the live feed. `stopped_at` is what
    the watch wrote the moment every session on this task stopped producing
    output, and it is cleared the moment one starts again.

    A task with no sessions at all is not the same as one whose sessions have
    ended: nothing was ever dispatched for it, and drawing the two alike
    would have the page report work as finished that never began.
    """
    if not sessions:
        return "unassigned"
    if not alive:
        return "ended"
    return "stopped" if task.get("stopped_at") is not None else "working"


def _active(activity: str, fresh: bool) -> bool:
    """Whether this work gets a spinner: the watch and the session must agree.

    `activity` is the watch's reading and `fresh` is the session's own. Both
    are required, which is the whole of the difference between "nobody has
    recorded a stop" and "this is running" - see the module docstring.
    """
    return activity == "working" and fresh


def _node(node_id: str, label: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "label": label, "kind": kind, **extra}


def _work_kinds(epics: list[dict[str, Any]], features: list[dict[str, Any]],
                stories: list[dict[str, Any]]) -> dict[str, str]:
    """Every board item's own kind, as a card draws it in its upper-left.

    Looked up across all three kinds rather than guessed. The page used to
    need only "is this a feature", and anything else was treated as an epic;
    a card that says what kind of work it is cannot do that, because a
    deleted item and a typo would both claim to be epics.

    A bug is a story with a subtype and the board offers it as a fourth type
    of its own (`kanban_data.current_type`), so it gets its own word here.
    """
    kinds = {epic["id"]: "epic" for epic in epics}
    kinds.update({feature["id"]: "feature" for feature in features})
    kinds.update({
        story["id"]: "bug" if story.get("subtype") == "bug" else "story"
        for story in stories})
    return kinds


def _board_children(item_id: str, features: list[dict[str, Any]],
                    stories: list[dict[str, Any]],
                    numbers: dict[str, str],
                    kinds: dict[str, str]) -> tuple[list[dict[str, Any]],
                                                    list[dict[str, Any]],
                                                    list[dict[str, Any]]]:
    """The live sub-tree under a board item, plus what of it is finished.

    Returns nodes, edges and the finished rows for the barrel. A story that
    is done is in the barrel and not in the tree. So is a feature every one
    of whose stories is done: only a story carries a status on this board, so
    a finished feature is one with nothing left under it, and leaving it in
    the tree would draw a branch with no live work on it.

    None of these children carries an assigned mind, a session name or a
    spinner. The assignment is on the item a session was dispatched to, and
    copying it down the tree is the one thing Daniel said it must not do:
    a story drawn under an assigned feature is not itself assigned to
    anybody.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    barrel: list[dict[str, Any]] = []

    def _add_stories(parent_id: str, feature_id: str) -> None:
        for story in stories:
            if story.get("feature_id") != feature_id:
                continue
            label = f"{numbers.get(story['id'], '')} {story['title']}".strip()
            work_kind = kinds.get(story["id"], "story")
            if story.get("status") == BOARD_DONE:
                barrel.append(_node(story["id"], label, "story", status=DONE,
                                    work_kind=work_kind))
                continue
            nodes.append(_node(story["id"], label, "story",
                               status=story.get("status", ""),
                               work_kind=work_kind, active=False))
            edges.append({"source": parent_id, "target": story["id"]})

    if kinds.get(item_id) == "feature":
        _add_stories(item_id, item_id)
        return nodes, edges, barrel
    if kinds.get(item_id) != "epic":
        # Not a board item this hive has: a deleted row, or an id that was
        # never one. It draws as a plain task with no children, which is
        # what a task naming no board item draws as anyway.
        return nodes, edges, barrel

    # An epic: its features, and each feature's stories beneath it.
    owned = [f for f in features if f.get("epic_id") == item_id]
    for feature in owned:
        label = f"{numbers.get(feature['id'], '')} {feature['title']}".strip()
        mine = [s for s in stories if s.get("feature_id") == feature["id"]]
        if mine and all(s.get("status") == BOARD_DONE for s in mine):
            barrel.append(_node(feature["id"], label, "feature", status=DONE,
                                work_kind="feature"))
            continue
        nodes.append(_node(feature["id"], label, "feature", status="",
                           work_kind="feature", active=False))
        edges.append({"source": item_id, "target": feature["id"]})
        _add_stories(feature["id"], feature["id"])
    return nodes, edges, barrel


def _unique(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows, first of each id kept.

    Two tasks may cover the same board item - one watching the work, one
    watching a question about it - and its stories would then be drawn
    twice. A repeated id is not a second node, it is the same node, and a
    graph handed one twice draws neither reliably.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        out.append(row)
    return out


def _unique_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The edges, one per pair, for the reason `_unique` gives."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for edge in edges:
        pair = (edge["source"], edge["target"])
        if pair in seen:
            continue
        seen.add(pair)
        out.append(edge)
    return out


def _chief_face(chief: str,
                minds: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The chief of staff's own face, when the chief is a registered mind.

    The root's label is a display name this function is handed, not a
    registry row, so it is matched by name rather than by id. Case-folded
    because the label is written for a reader ("Stash") and the registry
    holds the slug ("stash").
    """
    wanted = (chief or "").strip().casefold()
    for row in minds:
        if (row.get("name") or "").strip().casefold() == wanted:
            return _face(row["mind_id"], row.get("name") or chief)
    return None


async def get_tree(*, chief: str = "Stash",
                   now: float | None = None) -> dict[str, Any]:
    """Everything the page draws, in one answer.

    `nodes` and `edges` are the live tree, rooted at `CHIEF_ID`. `barrel` is
    what has finished, which is deliberately not in the tree: Daniel's
    instruction is that a finished child disappears from the work and goes to
    the right. `questions` is every bubble, in the order the tasks holding
    them last moved, and the root carries their count because that number is
    what the page is read for.
    """
    moment = time.time() if now is None else now
    tasks = await staff.tasks()
    mind_rows = await core.list_minds()
    names = _mind_names(mind_rows)
    # One read of the board rather than three. `get_data`'s own docstring
    # says why: three reads are three moments, and a feature created between
    # two of them is absent from the kind lookup, so a task naming it would
    # draw as a plain task with no children — indistinguishable from a task
    # naming no board item at all.
    board = await kanban_data.get_data()
    epics = board["epics"]
    features = board["features"]
    stories = board["stories"]
    numbers = await kanban_data.backlog_numbers()
    kinds = _work_kinds(epics, features, stories)
    agent_rows = await staff.agents()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    barrel: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    for task in tasks:
        label = task["name"]
        board_item = task.get("board_item")
        work_kind = kinds.get(board_item or "", TASK_KIND)
        if task["status"] == DONE:
            barrel.append(_node(task["id"], label, "task", status=DONE,
                                work_kind=work_kind,
                                finished_at=task.get("updated_at")))
            continue

        sessions = [p["session_id"]
                    for p in await staff.task_sessions(task["id"])]
        working = await _assigned(sessions, names, now=moment)
        activity = _activity(task, sessions, working["alive"])
        question = task.get("question")
        nodes.append(_node(
            task["id"], label, "task", status=task["status"],
            activity=activity, work_kind=work_kind,
            active=_active(activity, working["fresh"]),
            minds=working["minds"], session_names=working["session_names"],
            sessions=sessions, question=question,
            board_item=board_item,
            board_number=numbers.get(board_item or "", ""),
            description=task.get("description", "")))
        edges.append({"source": CHIEF_ID, "target": task["id"]})
        if question:
            questions.append({"task": task["id"], "name": label,
                              "question": question})

        if board_item:
            sub_nodes, sub_edges, sub_barrel = _board_children(
                board_item, features, stories, numbers, kinds)
            nodes.extend(sub_nodes)
            # The board item's own children hang off the task node, which is
            # the item: there is no second node for the feature itself.
            edges.extend({"source": task["id"], "target": e["target"]}
                         if e["source"] == board_item else e
                         for e in sub_edges)
            barrel.extend(sub_barrel)

        # Sub-agents are drawn exactly as they were. A child is a brief, not
        # a mind: it has no registry row, so no face, and its own status is
        # not the live record a spinner is allowed to read, so no spinner
        # either. The one thing it gains is the work-type icon every card
        # carries.
        for agent in (a for a in agent_rows if a["task_id"] == task["id"]):
            if agent.get("finished_at") is not None:
                barrel.append(_node(
                    agent["id"], agent["brief"], "agent", status=DONE,
                    work_kind="agent", about=agent.get("about", ""),
                    outcome=agent.get("outcome", ""),
                    finished_at=agent["finished_at"]))
                continue
            stale = staff.agent_stale(agent, now=moment,
                                      timeout=AGENT_TIMEOUT)
            nodes.append(_node(
                agent["id"], agent["brief"], "agent",
                status="orphaned" if stale else "working",
                work_kind="agent", active=False,
                about=agent.get("about", ""), stale=stale,
                started_at=agent["started_at"]))
            edges.append({"source": task["id"], "target": agent["id"]})

    root = _node(CHIEF_ID, chief, "chief", questions=len(questions),
                 work_kind="chief", active=False)
    chief_face = _chief_face(chief, mind_rows)
    if chief_face is not None:
        root["minds"] = [chief_face]
    return {"nodes": [root, *_unique(nodes)], "edges": _unique_edges(edges),
            "barrel": _unique(barrel), "questions": questions,
            "generated_at": moment}
