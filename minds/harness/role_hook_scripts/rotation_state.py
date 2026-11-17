"""On-disk state shared by the three hooks a staged rotation runs through.

A terminal rotation used to begin and end inside one detached Stop child.
It now spans three separate hook processes — the Stop that stages it, every
Stop/SubagentStop that reports what is still running, and the
UserPromptSubmit that fires it — with an unbounded human-shaped gap in the
middle. None of them share memory, any of them can be SIGKILLed by the
respawn, and the pane survives a restart of the mind's service, so the handoff
has to be on disk.

Everything here is keyed by ``conversation_id``: several terminal panes share one
``CLAUDE_CONFIG_DIR`` and one hooks directory on this host, and the
conversation is the only thing that distinguishes them. It is also the right
key for correctness — a marker written for a conversation that has since
been replaced must not fire against its successor.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

def state_dir() -> Path:
    """Where the three hooks hand state to each other, resolved per call.

    ``AUTO_REMEMBER_LOG_DIR`` wins when set, which is how this mind reaches a
    directory it can write to at all: its project root is a read-only bind
    mount and only its own subtree is mounted writable back over it. The
    per-turn harvest log already honours the same variable, so the three
    hooks that hand each other state and the one that writes the log agree
    on one directory rather than two.

    Not a module constant: ``HIVE_PROJECT_DIR`` can come from the mind's
    ``.env``, which each hook loads at the top of its own run, so a constant
    bound at import time reads whatever the pane happened to inherit.

    With neither variable set it is the mind's own folder, two levels above
    the hooks directory this file is installed into.
    """
    explicit = os.environ.get("AUTO_REMEMBER_LOG_DIR")
    if explicit:
        return Path(explicit)
    return Path(
        os.environ.get("HIVE_PROJECT_DIR")
        or Path(__file__).resolve().parents[2]
    ) / "data" / "auto-remember"

# A marker older than this belongs to a conversation nobody is going to type
# into again — a pane that was closed, a service that was restarted, a
# session abandoned overnight. Firing one is a respawn nobody asked for.
MARKER_TTL_SECONDS = 24 * 3600

# A message that beats the summary waits this long for it, and no longer.
# Composition is 1-6 minutes of Ollama work, and this is a UserPromptSubmit
# hook — every second spent here is a second the pane sits frozen with the
# user's message unsent and no indication why. Waiting out a full
# composition is indistinguishable from a hung terminal, so the wait only
# covers the near-miss where the seed is seconds away. Past it the message
# goes through, the marker is left alone, and the next typed turn fires.
COMPOSE_WAIT_SECONDS = 8

# A fire that got as far as the gateway but has not reported back. The
# marker survives in this state so a failed swap can be retried, and a
# second prompt arriving mid-fire does not start a rival respawn against
# the same pane and the same seed file.
FIRE_LEASE_SECONDS = 120

STATE_COMPOSING = "composing"
STATE_READY = "ready"
STATE_FIRING = "firing"


def _marker_path(conversation_id: str) -> Path:
    return state_dir() / f"rotation-staged-{conversation_id}.json"


def _bgtasks_path(conversation_id: str) -> Path:
    return state_dir() / f"bgtasks-{conversation_id}.json"


def _write_json(path: Path, payload: dict) -> bool:
    """Write a state file atomically, or report that it could not be.

    A half-written marker read by the fire hook is a rotation fired against
    a conversation whose seed does not exist yet, so the rename is the whole
    point of this function.
    """
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload))
        os.replace(temp, path)
        return True
    except OSError:
        return False


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The staged-rotation marker
# ---------------------------------------------------------------------------

def stage_marker(
    conversation_id: str, *, client_type: str, client_ref: str, forced: bool
) -> bool:
    """Record that this conversation is rotating, before any work is done.

    Called at the top of the Stop hook's rotation path, not the bottom. The
    old design armed after composition finished, which measured at 90
    seconds to 3.5 minutes after the assistant's turn ended — so a user who
    replied promptly, which is the normal case, replied into a conversation
    that was not yet pending anything and the rotation landed on some later
    message instead. Staging first is what makes "the next message" true.
    """
    return _write_json(_marker_path(conversation_id), {
        "conversation_id": conversation_id,
        "client_type": client_type,
        "client_ref": client_ref,
        "forced": bool(forced),
        "state": STATE_COMPOSING,
        "staged_at": time.time(),
    })


def mark_ready(conversation_id: str) -> bool:
    """Promote a staged marker once the gateway is holding the seed."""
    marker = read_marker(conversation_id)
    if marker is None:
        return False
    marker["state"] = STATE_READY
    marker["ready_at"] = time.time()
    return _write_json(_marker_path(conversation_id), marker)


def claim_for_firing(conversation_id: str) -> bool:
    """Take the marker for a fire, or report that someone else has it.

    Reading the marker and clearing it are seconds apart, and two prompts
    queued during that window would otherwise both fire: two
    ``respawn-pane -k`` against the same pane, using the same staged
    conversation id and therefore the same seed file, where the first pane's
    ``rm`` can delete the second's seed between its read and its exec. One
    of the two messages is lost and the successor may open on nothing.

    The claim expires, because the process holding it is the one the respawn
    kills — a lease that only its holder could release would strand every
    later turn.
    """
    marker = read_marker(conversation_id)
    if marker is None:
        return False
    if marker.get("state") == STATE_FIRING:
        held_for = time.time() - float(marker.get("firing_at") or 0)
        if held_for < FIRE_LEASE_SECONDS:
            return False
    marker["state"] = STATE_FIRING
    marker["firing_at"] = time.time()
    return _write_json(_marker_path(conversation_id), marker)


def release_firing_claim(conversation_id: str) -> None:
    """Hand a marker back after a fire the gateway refused.

    Nothing was replaced, so the rotation is still owed. Returning it to
    ``ready`` is what lets the next typed turn try again instead of paying
    another six minutes of composition for a seed the gateway already holds.
    """
    marker = _read_json(_marker_path(conversation_id))
    if marker is None or marker.get("state") != STATE_FIRING:
        return
    marker["state"] = STATE_READY
    marker.pop("firing_at", None)
    _write_json(_marker_path(conversation_id), marker)


def read_marker(conversation_id: str) -> dict | None:
    """The live marker for this conversation, or None.

    An expired marker is deleted on the way past rather than returned: the
    alternative is a file that answers "pending" forever for a pane that no
    longer exists.
    """
    marker = _read_json(_marker_path(conversation_id))
    if marker is None:
        return None
    if time.time() - float(marker.get("staged_at") or 0) > MARKER_TTL_SECONDS:
        clear_marker(conversation_id)
        return None
    return marker


def clear_marker(conversation_id: str) -> None:
    try:
        _marker_path(conversation_id).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# What is still running
# ---------------------------------------------------------------------------

# A job that is winding down still owns its process group, and a respawn
# kills the group. ``stopping`` is this harness's word for "cancel requested,
# not yet settled" — counting only ``running`` lets the rotation fire into a
# build that is still unwinding.
LIVE_JOB_STATUSES = ("running", "stopping")


def record_background_tasks(conversation_id: str, tasks: list) -> bool:
    """Snapshot the harness's own account of what is still running.

    ``background_tasks`` rides on every Stop and SubagentStop payload and
    covers both backgrounded shells and subagents, each with a live status.
    UserPromptSubmit does not carry it, which is why it is snapshotted here
    for the fire hook to read.

    The snapshot is never stale at the moment it matters: a background task
    finishing is itself injected as a prompt, so it produces an assistant
    turn and therefore a Stop, which rewrites this file. When the last one
    drains, a Stop fires carrying an empty list.
    """
    live = [
        t for t in (tasks or [])
        if isinstance(t, dict) and (t.get("status") or "") in LIVE_JOB_STATUSES
    ]
    return _write_json(_bgtasks_path(conversation_id), {
        "conversation_id": conversation_id,
        "at": time.time(),
        "running": live,
    })


def running_background_tasks(conversation_id: str) -> list:
    snapshot = _read_json(_bgtasks_path(conversation_id))
    if not snapshot:
        return []
    running = snapshot.get("running")
    return running if isinstance(running, list) else []


def sweep_dead_state() -> None:
    """Delete state files for conversations that are over.

    Every rotation leaves a snapshot behind under a conversation id that no
    longer exists — the respawn mints a new one — and nothing else ever
    collects them. The marker has its own expiry because reading a stale one
    is a *correctness* problem; these are only ever a directory that grows
    without limit, so they are swept on the same clock and no sooner.
    """
    cutoff = time.time() - MARKER_TTL_SECONDS
    try:
        # The lock files belong to the Stop hook, but they are the same
        # shape of litter in the same directory on the same clock: one per
        # conversation, keyed to a conversation_id the next rotation replaces, and
        # released by the process exiting rather than by anything deleting
        # the file.
        root = state_dir()
        stale = list(root.glob("bgtasks-*.json")) + list(root.glob("rotation-*.lock"))
    except OSError:
        return
    for path in stale:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def describe_background_tasks(tasks: list) -> str:
    """A one-line account of what is holding the rotation, for the model."""
    parts = []
    for task in tasks:
        kind = task.get("type") or "task"
        label = (task.get("description") or task.get("command") or "").strip()
        parts.append(f"{kind}: {label}" if label else kind)
    return "; ".join(parts)
