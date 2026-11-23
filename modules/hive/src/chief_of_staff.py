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
and tooling read the same way. Whether a sub-agent is working or merely
running is `staff.agent_state`, the one reading the sweep shares.
"""

from __future__ import annotations

import re
import time
from typing import Any

import kanban_data
import mind_images
import store
from hive_data import core, staff

#: What a ledger task in this status is: finished, and drawn in the barrel
#: rather than in the tree.
DONE = "done"

#: A board item in this status is finished. The board's own word for it
#: happens to match the ledger's; they are separate vocabularies and are
#: named separately so that renaming one does not silently move the other.
#: Every kind carries a status since board step 13, but a feature that
#: finished before then still reads backlog, so a feature is also finished
#: when every one of its stories is (`_feature_finished`).
BOARD_DONE = "done"

#: How far back the barrel reaches when nobody chose (feature 6.11,
#: 2026-11-16): finished work older than this many days is left out of the
#: answer, never deleted, and a wider window brings it back.
DEFAULT_WINDOW_DAYS = 7

#: The widest window the page may ask for, ten years: wide enough to mean
#: "everything", and bounded so a typed URL cannot ask for an overflow.
MAX_WINDOW_DAYS = 3650

#: Seconds in a day, the unit the window is chosen in.
DAY = 24 * 60 * 60

#: How long a sub-agent may be open before it reads as orphaned rather than
#: working. The mind's turn timeout: a child lives inside the turn's process,
#: so one still open past it was killed and could not write its own ending.
#: `turn_timeout_seconds` for the chief of staff, which is 900.
AGENT_TIMEOUT = 15 * 60

#: How often the page asks for the tree. The template's own `REFRESH_MS`,
#: in seconds; named here because the working window below is derived
#: from it.
REFRESH = 20

#: How long one activity report from a harness lets a sub-agent read as
#: working. Three refreshes, which is also how long the page goes on
#: trusting a payload of its own before it takes every spinner off: a child
#: whose reporter samples on the page's cadence has a fresh stamp at every
#: reload, and one whose stamp is older than three reloads is no longer
#: evidence of work. Deliberately short - see `staff.agent_state`.
FRESH_WITHIN = 3 * REFRESH

#: What a sub-agent node says when its writer has named nothing it could
#: be called by - a dsh start carries neither kind nor work, and its ending
#: fills them in. A robot with no words under it is not a status indicator.
AGENT_FALLBACK = "sub-agent"

#: The kinds of board item a story is. A bug is a story with a subtype, so
#: wherever this page asks "is this a story" - the barrel, the chain, the
#: spinner a working child lends - a bug is one.
STORY_KINDS = ("story", "bug")

#: What a brief file's name ends in, and is stripped of before it is shown.
BRIEF_EXTENSIONS = (".md", ".markdown", ".txt")

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

#: The root remains present to anchor the work even before a chief is chosen.
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
    present: set[str] = set()
    for session_id in session_ids:
        row = await core.get_session(session_id)
        if row is None:
            continue
        alive = True
        present.add(session_id)
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
            "session_names": session_names, "present": present}


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


def _board_label(item: dict[str, Any], numbers: dict[str, str]) -> str:
    return f"{numbers.get(item['id'], '')} {item['title']}".strip()


def _barrel_item(item: dict[str, Any], kind: str, numbers: dict[str, str],
                 kinds: dict[str, str],
                 finished_at: float | None) -> dict[str, Any]:
    """A finished board item as the barrel draws it, by its own board id,
    with the moment it finished, which is what the window counts from."""
    return _node(item["id"], _board_label(item, numbers), kind,
                 status=DONE, work_kind=kinds.get(item["id"], kind),
                 finished_at=finished_at)


def _barrel_story(story: dict[str, Any], numbers: dict[str, str],
                  kinds: dict[str, str]) -> dict[str, Any]:
    """A finished story, finished when the board stamped it done."""
    return _barrel_item(story, "story", numbers, kinds, story.get("done_at"))


def _barrel_task(task: dict[str, Any]) -> dict[str, Any]:
    """A finished ledger task as itself: the work it was named no board item
    the barrel can draw in its place, and it still finished."""
    return _node(task["id"], task["name"], "task", status=DONE,
                 work_kind=TASK_KIND, finished_at=task.get("done_at"))


def _feature_finished(feature: dict[str, Any],
                      stories: list[dict[str, Any]]) -> tuple[bool,
                                                              float | None]:
    """Whether a feature is finished, and when.

    Finished when the board says so, at the moment it did; or, for a
    feature whose status still reads what step 13 backfilled, when it has
    stories and every one is done, at the moment the last of them finished.
    """
    if feature.get("status") == BOARD_DONE:
        return True, feature.get("done_at")
    mine = [s for s in stories if s.get("feature_id") == feature["id"]]
    if mine and all(s.get("status") == BOARD_DONE for s in mine):
        times = [s["done_at"] for s in mine if s.get("done_at") is not None]
        return True, max(times) if times else None
    return False, None


def _in_window(rows: list[dict[str, Any]], *, now: float,
               days: int) -> list[dict[str, Any]]:
    """The finished rows that finished within the last `days` days, newest
    first.

    Time is the only thing that hides a row here (requirement 5 of
    2026-11-16): a row whose finish was never recorded cannot be judged by
    time, so it is kept, after every row that can be.
    """
    since = now - days * DAY
    kept = [r for r in rows
            if r.get("finished_at") is None or r["finished_at"] >= since]
    return sorted(kept, key=lambda r: (r.get("finished_at") is None,
                                       -(r.get("finished_at") or 0.0),
                                       r["id"]))


def _board_children(item_id: str, features: list[dict[str, Any]],
                    stories: list[dict[str, Any]],
                    numbers: dict[str, str],
                    kinds: dict[str, str]) -> tuple[list[dict[str, Any]],
                                                    list[dict[str, Any]],
                                                    list[dict[str, Any]]]:
    """The live sub-tree under a board item, plus what of it is finished.

    Returns nodes, edges and the finished rows for the barrel. A story that
    is done is in the barrel and not in the tree. A feature every one of
    whose stories is done leaves the tree too, and goes to the barrel with
    its stories: the barrel holds finished stories, features and tasks
    (feature 6.11, 2026-11-16).

    None of these children carries an assigned mind or a session name, and
    none spins of its own accord; a story spins only when a working child is
    drawn beneath it, which `get_tree` decides once the tree is whole. The
    assignment is on the item a session was dispatched to, and
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
                barrel.append(_barrel_story(story, numbers, kinds))
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
            # The feature leaves the tree for the barrel, and its stories,
            # which are finished stories, go with it.
            _, finished_at = _feature_finished(feature, stories)
            barrel.append(_barrel_item(feature, "feature", numbers, kinds,
                                       finished_at))
            for story in mine:
                barrel.append(_barrel_story(story, numbers, kinds))
            continue
        nodes.append(_node(feature["id"], label, "feature", status="",
                           work_kind="feature", active=False))
        edges.append({"source": item_id, "target": feature["id"]})
        _add_stories(feature["id"], feature["id"])
    return nodes, edges, barrel


def _finished_item(item: dict[str, Any] | None, kind: str | None,
                   stories: list[dict[str, Any]], numbers: dict[str, str],
                   kinds: dict[str, str]) -> dict[str, Any] | None:
    """The barrel row a finished ledger task's board item draws as, or None
    when the item is not one the barrel draws or the board does not call it
    finished - in which case the task stands for itself."""
    if item is None:
        return None
    if kind in STORY_KINDS or kind == "task":
        if item.get("status") != BOARD_DONE:
            return None
        return _barrel_item(item, "story" if kind in STORY_KINDS else "task",
                            numbers, kinds, item.get("done_at"))
    if kind == "feature":
        done, finished_at = _feature_finished(item, stories)
        if not done:
            return None
        return _barrel_item(item, "feature", numbers, kinds, finished_at)
    return None


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


def _cut(text: str, limit: int) -> str:
    """The text, or as much as fits with an ellipsis saying it was cut."""
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _tile(number: Any, name: str) -> str:
    """What a tile says, and all it says: the work's whole-number item ID
    first - the ID column of the backlog, not its place in the order
    (Daniel, 2026-11-21) - then its name on the line beneath, whole: the
    page wraps it and grows the tile to hold it (Daniel, 2026-11-22; it was
    a few words until then). Work with no place on the board has no ID, and
    says only its name."""
    return "\n".join(part for part in (str(number or ""), name.strip())
                     if part)


def _brief_words(brief: str) -> str:
    """A brief file's name as the words it is made of.

    `read-and-judge.md` is the only record of what one of the chief's own
    delegates is doing, so it is what the node says - but as words. The
    file name itself is what Daniel asked to stop seeing (2026-11-15), so
    the folder, the extension and the separators all go.
    """
    stem = brief.replace("\\", "/").rsplit("/", 1)[-1]
    # Only an extension a brief file actually has: a dot inside the name
    # ("v1.2-notes") is part of the words.
    for extension in BRIEF_EXTENSIONS:
        if stem.lower().endswith(extension) and len(stem) > len(extension):
            stem = stem[:-len(extension)]
            break
    return " ".join(stem.replace("-", " ").replace("_", " ").split())


#: A reported child's description may end by naming the board item it was
#: sent to build - ` #<board id>` or ` #<item number>` - which is the only
#: thing that says which story a coder's child is on (Daniel, 2026-11-22: a
#: feature's task spun and none of its stories did). The reporter puts it
#: there, read from the child's own brief; it rides in the description
#: because that is the one free field a report carries, and it is taken off
#: again before the description is drawn.
_ITEM_REF = re.compile(r"\s*#([0-9A-Za-z]+)\s*$")


def _about(agent: dict[str, Any]) -> tuple[str, str]:
    """A reported child's description, and the board item it names if any."""
    about = (agent.get("about") or "").strip()
    found = _ITEM_REF.search(about)
    if found is None:
        return about, ""
    return about[:found.start()].strip(), found.group(1)


def _agent_label(agent: dict[str, Any]) -> str:
    """What a sub-agent node says: a few words of what it is doing.

    Shown the way a session name is shown, and cut to the same length
    (requirement 1 of 2026-11-15). A reported child's own one-line
    description is what it is doing; its kind is only what it is, so it is
    the fallback rather than a second line. A delegate the chief registered
    says what its brief is called, as words.
    """
    brief = (agent.get("brief") or "").strip()
    if brief:
        words = _brief_words(brief)
    else:
        words = (_about(agent)[0]
                 or (agent.get("agent_type") or "").strip())
    return _cut(words, NAME_LIMIT) if words else AGENT_FALLBACK


def _place_agents(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    """Where each open record hangs: its drawn ancestor, None for the task.

    `rows` is every record this task's work has produced, ended ones
    included, because an ended record is still the evidence that a child's
    parent existed. Returns an entry only for the records that are drawn.

    * A record whose parent is open and drawn hangs beneath it.
    * A record whose parent has ended hangs beneath that parent's nearest
      ancestor still drawn, or beneath the task when none is: the parent has
      left the tree, as every ended child does, and the child is still its
      descendant and still this task's work.
    * A record whose parent resolves to nothing - never recorded, or a chain
      that loops back on itself - is not drawn at all. The migration that
      made the column says so: a parent that resolves to nothing draws as
      unplaceable, and an unplaceable child is left off the tree rather than
      hung under work it cannot be shown to belong to.
    """
    by_id = {row["id"]: row for row in rows}
    placed: dict[str, str | None] = {}
    for row in rows:
        if row.get("finished_at") is not None:
            continue
        anchor: str | None = None
        seen = {row["id"]}
        parent = row.get("parent_agent_id")
        ok = True
        while parent:
            if parent in seen or parent not in by_id:
                ok = False
                break
            seen.add(parent)
            ancestor = by_id[parent]
            if ancestor.get("finished_at") is None:
                anchor = parent
                break
            parent = ancestor.get("parent_agent_id")
        if ok:
            placed[row["id"]] = anchor
    # A record is drawn only when its chain of open anchors reaches the task.
    # Resolved here rather than in the walk, because a parent that is open
    # but unplaceable takes its whole subtree off the tree with it, and two
    # open records naming each other anchor on each other and reach nothing.
    def _reaches_task(agent_id: str) -> bool:
        visited: set[str] = set()
        current: str | None = agent_id
        while current is not None:
            if current in visited or current not in placed:
                return False
            visited.add(current)
            current = placed[current]
        return True

    return {agent_id: anchor for agent_id, anchor in placed.items()
            if _reaches_task(agent_id)}


async def _task_agents(task_id: str,
                       sessions: list[str]) -> list[dict[str, Any]]:
    """Every record of this task's work: the chief's delegates and the
    children harnesses reported from the task's own conversations.

    The task is resolved when the record is read, not when it is written: a
    harness knows its conversation and nothing about the ledger, so a
    reported child belongs to whichever task its session is paired with now.
    """
    rows = list(await staff.agents(task_id=task_id))
    for session_id in sessions:
        rows.extend(await staff.agents(session_id=session_id))
    return _unique(rows)


def _chief_face(chief: str,
                minds: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The chief of staff's own face, when the chief is a registered mind.

    The root's label is a display name this function is handed, not a
    registry row, so it is matched by name rather than by id. Case-folded
    because display names and registry names may use different casing.
    """
    wanted = (chief or "").strip().casefold()
    for row in minds:
        if (row.get("name") or "").strip().casefold() == wanted:
            return _face(row["mind_id"], row.get("name") or chief)
    return None


def _place_work(nodes: dict[str, dict[str, Any]], parent: dict[str, str],
                board_nodes: dict[str, dict[str, Any]],
                board_parent: dict[str, str], claimed: dict[str, str],
                extra: list[str], by_id: dict[str, dict[str, Any]],
                kinds: dict[str, str], numbers: dict[str, str]) -> None:
    """Hang every task and every board node beneath the one parent it has.

    Requirement 3 of 2026-11-15: work under the same epic and feature is one
    chain - the feature, its stories beneath it, each story's sub-agents
    beneath that. A board item is drawn once, so where a task covers an item
    some other task's expansion also draws, the task takes the item's place
    and the item is not drawn as itself. A task covering a story hangs
    beneath that story's feature wherever the feature is drawn, and when no
    task draws it the feature is drawn from the board for the purpose,
    carrying only the stories tasks cover: it is a heading for live work, not
    a second copy of the board.

    Nothing here climbs the board. A feature's parent counts only when it is
    an epic and a story's only when it is a feature or an epic, one step,
    because the board lets a feature sit beneath a feature and lets two
    items name each other, and a walk up that would loop.
    """
    def drawn(item_id: str | None) -> str | None:
        if not item_id:
            return None
        if item_id in claimed:
            return claimed[item_id]
        return item_id if item_id in nodes else None

    def epic_of(item: dict[str, Any]) -> str | None:
        epic_id = item.get("epic_id")
        return epic_id if kinds.get(epic_id or "") == "epic" else None

    for node_id, node in board_nodes.items():
        if node_id in claimed:
            continue
        nodes[node_id] = node
        source = board_parent[node_id]
        parent[node_id] = claimed.get(source, source)

    for item_id, task_id in claimed.items():
        kind = kinds[item_id]
        item = by_id[item_id]
        if item_id in board_parent:
            source = board_parent[item_id]
            parent[task_id] = claimed.get(source, source)
        elif kind in STORY_KINDS:
            feature_id = item.get("feature_id")
            if kinds.get(feature_id or "") == "feature":
                if drawn(feature_id) is None:
                    feature = by_id[feature_id]
                    nodes[feature_id] = _node(
                        feature_id, _board_label(feature, numbers), "feature",
                        status="", work_kind="feature", active=False)
                    parent[feature_id] = drawn(epic_of(feature)) or CHIEF_ID
                parent[task_id] = drawn(feature_id)
            else:
                parent[task_id] = drawn(epic_of(item)) or CHIEF_ID
        elif kind == "feature":
            parent[task_id] = drawn(epic_of(item)) or CHIEF_ID
        else:
            parent[task_id] = CHIEF_ID

    # A second task on the same item is the same work seen twice - one
    # watching it, one watching a question about it - so it hangs beside
    # the first rather than taking a place of its own.
    for task_id in extra:
        parent[task_id] = parent[claimed[nodes[task_id]["board_item"]]]
    for node_id, node in nodes.items():
        if node["kind"] == "task":
            parent.setdefault(node_id, CHIEF_ID)


def _lend_activity(nodes: dict[str, dict[str, Any]],
                   parent: dict[str, str]) -> None:
    """A story with a working sub-agent anywhere beneath it is working too.

    Requirement 2 of 2026-11-15. Only a working child lends it - one only
    known to be unfinished claims nothing - and it is lent to stories alone:
    a feature's state is its stories'. A story with no working child keeps
    exactly what it had.
    """
    lent: set[str] = set()
    for node in nodes.values():
        if node["kind"] != "agent" or node.get("active") is not True:
            continue
        seen: set[str] = set()
        current = parent.get(node["id"])
        while current is not None and current not in seen:
            seen.add(current)
            above = nodes.get(current)
            if above is None:
                break
            if above.get("work_kind") in STORY_KINDS:
                above["active"] = True
                lent.add(above.get("board_item") or above["id"])
            current = parent.get(current)
    # A story two tasks are on is one story, drawn twice: it is working or
    # it is not, and both its nodes say the same.
    for node in nodes.values():
        if (node.get("work_kind") in STORY_KINDS
                and (node.get("board_item") or node["id"]) in lent):
            node["active"] = True


async def get_tree(*, chief: str | None = None, now: float | None = None,
                   window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Everything the page draws, in one answer.

    `nodes` and `edges` are the live tree, rooted at `CHIEF_ID`. `barrel` is
    what has finished, which is deliberately not in the tree: Daniel's
    instruction is that a finished child disappears from the work and goes to
    the right. It holds finished stories, features and tasks that finished
    within the last `window_days` days, newest first; older ones are only
    left out of this answer. `finished_stories` counts every finished story
    the barrel would hold at any window, which is what the progress bar
    reads, so choosing a narrower window does not move the bar.
    `questions` is every bubble, in the order the tasks holding them last
    moved, and the root carries their count because that number is what the
    page is read for.
    """
    moment = time.time() if now is None else now
    tasks = await staff.tasks()
    mind_rows = await core.list_minds()
    if chief is None:
        role = await store.role_by_slug("chief-of-staff")
        holders = set(await store.role_holders(role["id"])) if role else set()
        selected = [row for row in mind_rows if row["mind_id"] in holders]
        chief = ", ".join(row.get("name") or row["mind_id"] for row in selected)
        chief_faces = [_face(row["mind_id"], row.get("name") or row["mind_id"])
                       for row in selected]
    else:
        face = _chief_face(chief, mind_rows)
        chief_faces = [face] if face is not None else []
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

    # Every drawn node has exactly one parent, recorded here as it is placed
    # and turned into edges only at the end. A node handed two parents is
    # drawn by the layout at the shallower of the two - a story task with an
    # edge from the chief as well as one from its feature sits beside the
    # feature, not beneath it, which is the side-by-side drawing requirement
    # 3 of 2026-11-15 removes.
    nodes: dict[str, dict[str, Any]] = {}
    parent: dict[str, str] = {}
    barrel: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    # The board's own nodes, drawn by a task's expansion, kept apart until
    # the tasks are placed: a board item a task covers is drawn as that task
    # and not a second time as itself.
    board_nodes: dict[str, dict[str, Any]] = {}
    board_parent: dict[str, str] = {}
    # The first live task covering each board item takes that item's place.
    claimed: dict[str, str] = {}
    extra: list[str] = []
    finished: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    board_tasks = {row["id"]: row for row in board.get("tasks", [])}
    by_id = {row["id"]: row for row in (*epics, *features, *stories)}
    # Which task takes a board item's place is decided once, by which was
    # opened first, never by the order the ledger lists them in: that is
    # most recently moved first, so a watch noting a stop on one of two
    # tasks covering a feature would hand the feature's stories from one to
    # the other and back, and redraw the whole tree each time.
    for task in sorted((t for t in tasks if t["status"] != DONE),
                       key=lambda t: (t["created_at"], t["id"])):
        item = task.get("board_item")
        if kinds.get(item or "", TASK_KIND) != TASK_KIND:
            claimed.setdefault(item, task["id"])

    for task in tasks:
        label = task["name"]
        board_item = task.get("board_item")
        work_kind = kinds.get(board_item or "", TASK_KIND)
        if task["status"] == DONE:
            # A finished task leaves the tree for the barrel. When the work
            # it was is a board story, feature or task the board also calls
            # finished, that item is what finished and is drawn in its
            # place; otherwise the ledger task itself is (feature 6.11).
            item = by_id.get(board_item or "")
            kind = kinds.get(board_item or "")
            if item is None and board_item in board_tasks:
                # A board task is not one of `kinds`: a ledger task naming
                # one draws as a plain task in the tree, with no children.
                item, kind = board_tasks[board_item], "task"
            finished.append((task, _finished_item(item, kind, stories,
                                                   numbers, kinds)))
            # The finished work beneath it finished too, and closing the
            # task that watched it must not hide it: only time hides
            # finished work (requirement 5).
            if kind in ("feature", "epic"):
                barrel.extend(_board_children(board_item, features, stories,
                                              numbers, kinds)[2])
            continue

        sessions = [p["session_id"]
                    for p in await staff.task_sessions(task["id"])]
        working = await _assigned(sessions, names, now=moment)
        activity = _activity(task, sessions, working["alive"])
        question = task.get("question")
        nodes[task["id"]] = _node(
            task["id"], label, "task", status=task["status"],
            activity=activity, work_kind=work_kind,
            active=_active(activity, working["fresh"]),
            minds=working["minds"], session_names=working["session_names"],
            sessions=sessions, question=question,
            board_item=board_item,
            board_number=numbers.get(board_item or "", ""),
            description=task.get("description", ""))
        if question:
            questions.append({"task": task["id"], "name": label,
                              "question": question})

        # The stories drawn beneath this task, by board id and by item
        # number: the only places a child naming a story may be hung.
        stories_here: dict[str, str] = {}
        if work_kind != TASK_KIND:
            if claimed[board_item] != task["id"]:
                extra.append(task["id"])
            else:
                sub_nodes, sub_edges, sub_barrel = _board_children(
                    board_item, features, stories, numbers, kinds)
                for node in sub_nodes:
                    board_nodes.setdefault(node["id"], node)
                    if node["kind"] == "story":
                        stories_here[node["id"]] = node["id"]
                        number = (by_id.get(node["id"]) or {}).get(
                            "item_number")
                        if number is not None:
                            stories_here[str(number)] = node["id"]
                for edge in sub_edges:
                    board_parent.setdefault(edge["target"], edge["source"])
                barrel.extend(sub_barrel)

        # Sub-agents: only the ones still running, beneath the work they
        # belong to. An ended one leaves the tree and goes nowhere - not the
        # barrel, which holds finished stories only, so a task's many
        # short-lived children cannot bury the work Daniel reads there
        # (DECIDED 2026-11-14, narrowed 2026-11-15). A child has no registry
        # row, so no face.
        agent_rows = await _task_agents(task["id"], sessions)
        placed = _place_agents(agent_rows)
        # Oldest first. The layout places siblings in the order it is handed
        # them, so newest-first would push every older child sideways each
        # time another spawned.
        drawn = sorted((a for a in agent_rows if a["id"] in placed),
                       key=lambda a: (a["started_at"], a["id"]))
        for agent in drawn:
            # One reading of what the record says, shared with the sweep:
            # working only while a harness's activity report is fresh, and
            # running otherwise - which is all most harnesses can ever say,
            # and all the chief's own delegates ever say.
            state = staff.agent_state(agent, now=moment,
                                      fresh_within=FRESH_WITHIN)
            # Orphaned: a running record whose child cannot still be alive.
            # For the chief's own delegates that is the clock, because their
            # turn timeout is `AGENT_TIMEOUT`. A reported child runs inside
            # some other mind's turn, which may be hours long or unbounded,
            # so the clock cannot judge it - but a child cannot outlive its
            # conversation, and comms deletes the session row when the
            # conversation ends, so a reported child whose session is gone is
            # certainly dead. Anything short of that certainty is the watch's
            # sweep to retire, which knows what is still producing output.
            if agent.get("brief"):
                stale = staff.agent_stale(agent, now=moment,
                                          timeout=AGENT_TIMEOUT)
            else:
                stale = agent.get("session_id") not in working["present"]
            stale = stale and state == staff.RUNNING
            nodes.setdefault(agent["id"], _node(
                agent["id"], _agent_label(agent), "agent",
                status="orphaned" if stale else state,
                work_kind="agent", active=state == staff.WORKING,
                agent_type=agent.get("agent_type", ""),
                about=agent.get("about", ""), stale=stale,
                started_at=agent["started_at"]))
            # A child of the conversation itself that names a story drawn
            # beneath this task hangs off that story, so the story is what
            # spins. A name that is no such story changes nothing: the
            # child stays on the task, where it certainly belongs.
            home = placed[agent["id"]] or stories_here.get(
                _about(agent)[1]) or task["id"]
            parent.setdefault(agent["id"], home)

    _place_work(nodes, parent, board_nodes, board_parent, claimed, extra,
                by_id, kinds, numbers)
    _lend_activity(nodes, parent)

    # What each tile says. A ledger task on a board item is called by that
    # item's ID, so the name beside it is that item's name too: an ID and a
    # name that belong to different things would be two labels, not one.
    for node_id, node in nodes.items():
        item_id = node.get("board_item") or node_id
        item = by_id.get(item_id) or board_tasks.get(item_id)
        node["item_number"] = item.get("item_number") if item else None
        name = item["title"] if item else node["label"]
        node["tile"] = _tile(node["item_number"] if item else "", name)
        # The name alone, for a page that draws the ID apart from it -
        # larger, because the ID is what the work is called by and at one
        # size with the name it could not be picked out (Daniel, 2026-11-21).
        node["tile_name"] = name.strip()

    # A story still being worked is not finished, whatever the board says:
    # the ledger is the authority on whether a task is over.
    live_items = {node.get("board_item") for node in nodes.values()}
    live_items.update(nodes)
    # An item still being worked is not drawn as finished, so the ledger
    # task that finished on it stands for itself instead.
    for task, row in finished:
        if row is None or row["id"] in live_items:
            row = _barrel_task(task)
        barrel.append(row)
    barrel = [row for row in _unique(barrel) if row["id"] not in live_items]
    finished_stories = sum(1 for row in barrel
                           if row.get("work_kind") in STORY_KINDS)
    barrel = _in_window(barrel, now=moment, days=window_days)

    root = _node(CHIEF_ID, chief, "chief", questions=len(questions),
                 work_kind="chief", active=False)
    if chief_faces:
        root["minds"] = chief_faces
    return {"nodes": [root, *nodes.values()],
            "edges": [{"source": source, "target": target}
                      for target, source in parent.items()],
            "barrel": barrel, "questions": questions,
            "finished_stories": finished_stories,
            "window_days": window_days,
            "generated_at": moment}
