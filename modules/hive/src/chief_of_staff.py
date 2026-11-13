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

No SQL here and no second opinion about anything. The ledger comes through
`hive_data.staff`, the board through `kanban_data`, and whether a sub-agent
has outlived its turn is `staff.agent_stale`, which the ledger's own tick
and tooling read the same way.
"""

from __future__ import annotations

import time
from typing import Any

import kanban_data
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

#: The chief of staff's node. One root, named rather than derived: the page
#: is about this mind and a hive with no ledger rows still draws him.
CHIEF_ID = "chief"


def _mind_names(minds: list[dict[str, Any]]) -> dict[str, str]:
    """Mind id to display name. `mind_id` is the key on a `core.minds` row,
    not `id`, and a session points at that."""
    return {m["mind_id"]: m.get("name") or m["mind_id"] for m in minds}


async def _assigned(session_ids: list[str],
                    names: dict[str, str]) -> tuple[list[str], bool]:
    """The minds working a task, and whether any of its sessions still exists.

    A paired session with no `core.sessions` row has ended - comms deletes
    the row when a conversation does - so the second answer is what separates
    a task whose delegates are still there from one whose have all gone.
    """
    minds: list[str] = []
    alive = False
    for session_id in session_ids:
        row = await core.get_session(session_id)
        if row is None:
            continue
        alive = True
        name = names.get(row.get("mind_id") or "")
        if name and name not in minds:
            minds.append(name)
    return minds, alive


def _activity(task: dict[str, Any], sessions: list[str], alive: bool) -> str:
    """Whether this task is being worked, stopped, ended, or never dispatched.

    From the rows alone rather than from the live feed. `stopped_at` is what
    the watch wrote the moment every session on this task stopped producing
    output, and it is cleared the moment one starts again, so it is already
    the answer and asking comms a second time could only disagree with it.

    A task with no sessions at all is not the same as one whose sessions have
    ended: nothing was ever dispatched for it, and drawing the two alike
    would have the page report work as finished that never began.
    """
    if not sessions:
        return "unassigned"
    if not alive:
        return "ended"
    return "stopped" if task.get("stopped_at") is not None else "working"


def _node(node_id: str, label: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "label": label, "kind": kind, **extra}


def _board_children(item_id: str, features: list[dict[str, Any]],
                    stories: list[dict[str, Any]],
                    numbers: dict[str, str]) -> tuple[list[dict[str, Any]],
                                                      list[dict[str, Any]],
                                                      list[dict[str, Any]]]:
    """The live sub-tree under a board item, plus what of it is finished.

    Returns nodes, edges and the finished rows for the barrel. A story that
    is done is in the barrel and not in the tree. So is a feature every one
    of whose stories is done: only a story carries a status on this board, so
    a finished feature is one with nothing left under it, and leaving it in
    the tree would draw a branch with no live work on it.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    barrel: list[dict[str, Any]] = []

    def _add_stories(parent_id: str, feature_id: str) -> None:
        for story in stories:
            if story.get("feature_id") != feature_id:
                continue
            label = f"{numbers.get(story['id'], '')} {story['title']}".strip()
            if story.get("status") == BOARD_DONE:
                barrel.append(_node(story["id"], label, "story", status=DONE))
                continue
            nodes.append(_node(story["id"], label, "story",
                               status=story.get("status", "")))
            edges.append({"source": parent_id, "target": story["id"]})

    as_feature = next((f for f in features if f["id"] == item_id), None)
    if as_feature is not None:
        _add_stories(item_id, item_id)
        return nodes, edges, barrel

    # An epic: its features, and each feature's stories beneath it.
    owned = [f for f in features if f.get("epic_id") == item_id]
    for feature in owned:
        label = f"{numbers.get(feature['id'], '')} {feature['title']}".strip()
        mine = [s for s in stories if s.get("feature_id") == feature["id"]]
        if mine and all(s.get("status") == BOARD_DONE for s in mine):
            barrel.append(_node(feature["id"], label, "feature", status=DONE))
            continue
        nodes.append(_node(feature["id"], label, "feature", status=""))
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
    names = _mind_names(await core.list_minds())
    features = await kanban_data.list_features()
    stories = await kanban_data.list_stories()
    numbers = await kanban_data.backlog_numbers()
    agent_rows = await staff.agents()

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    barrel: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    for task in tasks:
        label = task["name"]
        if task["status"] == DONE:
            barrel.append(_node(task["id"], label, "task", status=DONE,
                                finished_at=task.get("updated_at")))
            continue

        sessions = [p["session_id"]
                    for p in await staff.task_sessions(task["id"])]
        minds, alive = await _assigned(sessions, names)
        question = task.get("question")
        board_item = task.get("board_item")
        nodes.append(_node(
            task["id"], label, "task", status=task["status"],
            activity=_activity(task, sessions, alive), minds=minds,
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
                board_item, features, stories, numbers)
            nodes.extend(sub_nodes)
            # The board item's own children hang off the task node, which is
            # the item: there is no second node for the feature itself.
            edges.extend({"source": task["id"], "target": e["target"]}
                         if e["source"] == board_item else e
                         for e in sub_edges)
            barrel.extend(sub_barrel)

        for agent in (a for a in agent_rows if a["task_id"] == task["id"]):
            if agent.get("finished_at") is not None:
                barrel.append(_node(
                    agent["id"], agent["brief"], "agent", status=DONE,
                    about=agent.get("about", ""),
                    outcome=agent.get("outcome", ""),
                    finished_at=agent["finished_at"]))
                continue
            stale = staff.agent_stale(agent, now=moment,
                                      timeout=AGENT_TIMEOUT)
            nodes.append(_node(
                agent["id"], agent["brief"], "agent",
                status="orphaned" if stale else "working",
                about=agent.get("about", ""), stale=stale,
                started_at=agent["started_at"]))
            edges.append({"source": task["id"], "target": agent["id"]})

    root = _node(CHIEF_ID, chief, "chief", questions=len(questions))
    return {"nodes": [root, *_unique(nodes)], "edges": _unique_edges(edges),
            "barrel": _unique(barrel), "questions": questions,
            "generated_at": moment}
