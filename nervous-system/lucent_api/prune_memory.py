"""Per-class pruning dispatcher.

Four pruners, one per class. Each reads its class's entries through
`hive_data.graph.memories_for_class` — the anchor columns `/memory/list`
strips are in that read — applies the class-specific strategy, deletes
through `hive_data.memory.delete`, and returns a per-class outcome.

Why the read and the delete both moved. This module used to open its own
`sqlite3.connect(LUCENT_DB_PATH)` on a path that was not even the one lucent
itself used, and delete over HTTP against `/memory/{id}`. Left alone after
the store moved to Postgres, that read finds no file, logs an error, returns
an empty list — and every pruner answers `{"deleted": [], "kept": 0,
"errors": []}`, which is a clean nightly success report for a prune that
read nothing. With the old file still on disk it is worse: it reads ids the
load renumbered and deletes rows it never looked at. Both lie in the log.

So every outcome now carries a `read` count beside `deleted` and `kept`. A
pass that deleted nothing because there was nothing to delete and a pass
that deleted nothing because it was pointed at an empty store are the same
four dicts without it, and the scheduler in `server.py` says the second one
out loud.

Behavior per class:
- ``ephemeral`` — no-op. Nothing is stored, nothing to prune.
- ``current-state`` — anchor priority:
    1. ``codebase_ref`` set → check the file(s)/symbol(s) still exist;
       delete if the anchor has vanished.
    2. ``expires_at`` set → delete after the timestamp passes.
    3. ``kg_entity`` set → re-query the entity, drop entries that
       contradict newer facts on the same entity (TODO).
    4. No anchor → decay_only(half_life_days=180, delete_below=0.02).
- ``future-state`` — POST entry + recent ``current-state`` entries to
  ``/ollama/structured`` with schema ``{shipped: bool, reason: string}``;
  delete on ``shipped: true``. Then apply decay_only(90, 0.02) to the
  remainder.
- ``feedback`` — decay_only(half_life_days=90, delete_below=0.02).
  Standing-tier entries are exempt and never reach the pruner.
  Contradiction-detection runs at capture time (in the capture scripts),
  not here.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests

import hive_data
from hive_data import graph
from hive_data import memory as store
from hive_data.schema import qualified

log = logging.getLogger(__name__)

HIVE_TOOLS_URL = os.environ.get("HIVE_TOOLS_URL", "http://127.0.0.1:9421")
HIVE_TOOLS_TOKEN = os.environ.get("HIVE_TOOLS_TOKEN", "")

# Per-class decay parameters (per the design).
CURRENT_STATE_HALF_LIFE_DAYS = 180
CURRENT_STATE_DELETE_BELOW = 0.02
FUTURE_STATE_HALF_LIFE_DAYS = 90
FUTURE_STATE_DELETE_BELOW = 0.02
FEEDBACK_HALF_LIFE_DAYS = 90
FEEDBACK_DELETE_BELOW = 0.02


# ============================================================================
# Helpers
# ============================================================================

def _decay_score(created_at: float, now: float, half_life_days: int) -> float:
    age_seconds = max(now - float(created_at), 0.0)
    half_life_seconds = half_life_days * 86400
    return math.exp(-age_seconds / half_life_seconds)


async def _read_class_entries(
    data_class: str, *, include_standing: bool = False
) -> list[dict]:
    """One class's entries, with the anchors, from the store it is pruned in.

    `/memory/list` strips `codebase_ref` and `expires_at`; the pruners need
    both, which is why this is a named read in the access layer rather than
    an HTTP call.
    """
    return await graph.memories_for_class(
        data_class=data_class, include_standing=include_standing)


async def _delete_entry(entry_id: int) -> bool:
    """Remove one memory, in the same store the entries were read from.

    Through the access layer rather than over HTTP to this service's own
    `/memory/{id}`: a prune that deletes over the network needs a credential,
    a reachable port and a route guard that happens to admit it, and each of
    those failing looks identical in the log to a row the pruner decided to
    keep.
    """
    try:
        result = await store.delete(entry_id)
    except hive_data.HiveDataError:
        log.exception("delete failed for id=%s", entry_id)
        return False
    if not result.get("rows"):
        log.error("prune: id=%s was read and is already gone", entry_id)
        return False
    return True


async def _decay_pass(
    entries: list[dict], *, half_life_days: int, delete_below: float, now: float
) -> tuple[list[int], int]:
    """Apply decay to a list of entries. Returns (deleted_ids, kept_count)."""
    deleted: list[int] = []
    kept = 0
    for e in entries:
        score = _decay_score(e.get("created_at") or 0, now, half_life_days)
        if score < delete_below:
            if await _delete_entry(int(e["id"])):
                deleted.append(int(e["id"]))
        else:
            kept += 1
    return deleted, kept


async def _codebase_ref_exists(codebase_ref: str) -> bool:
    """Check that every path/symbol in the comma-separated codebase_ref still resolves.

    Each token is either:
    - a file path (relative or absolute) — exists on disk?
    - a symbol like ``Module.Class.method`` or ``func_name`` — at least
      one source file under the project root grep-matches it?

    Relative tokens resolve against ``PRUNE_PROJECT_ROOT``. When that env
    var is unset or names a directory this process can't see (the usual
    case inside the lucent container, which has no source mount), the ref
    is unverifiable — treat the entry as still valid rather than deleting
    on missing evidence.

    Async, and the `grep` goes behind a thread, for the same reason
    `_ollama_structured` does: the pruner is awaited on the scheduler's
    event loop now, so a ten-second subprocess inline is ten seconds —
    per unresolved token, per entry — with `/health`, every memory read and
    the pool's own protocol handling stopped dead.
    """
    if not codebase_ref:
        return True  # nothing to verify
    root = os.environ.get("PRUNE_PROJECT_ROOT", "")
    project_root = Path(root) if root else None
    if project_root is None or not project_root.is_dir():
        return True  # unverifiable — keep
    for raw in codebase_ref.split(","):
        token = raw.strip()
        if not token:
            continue
        # Try as path first
        candidate = (
            Path(token) if Path(token).is_absolute() else project_root / token
        )
        if candidate.exists():
            continue
        # Try as symbol via grep (any source file)
        if shutil.which("grep"):
            try:
                r = await asyncio.to_thread(
                    subprocess.run,
                    ["grep", "-r", "-l", token, str(project_root),
                     "--include=*.py", "--include=*.md", "--include=*.sh",
                     "--exclude-dir=.venv", "--exclude-dir=node_modules",
                     "--exclude-dir=.git"],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    continue
            except Exception:
                pass
        # Token didn't resolve as path or symbol
        return False
    return True


def _parse_iso(ts: str) -> float | None:
    if not ts:
        return None
    try:
        # Accept "2026-04-01T15:00:00Z" or with offset
        ts2 = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts2).timestamp()
    except Exception:
        return None


async def _ollama_structured(prompt: str, schema: dict) -> dict | None:
    """POST to /ollama/structured. Returns parsed JSON, or None on error.

    In a thread. The call is `requests` with a two-minute timeout and the
    pruner is awaited on the scheduler's event loop now, so inline it would
    hold that loop — and the loop is the one serving `/health` and every
    memory route — for up to two minutes per future-state entry.
    """
    if not HIVE_TOOLS_TOKEN:
        log.warning("HIVE_TOOLS_TOKEN not set; skipping Ollama call")
        return None

    def _ask() -> dict:
        resp = requests.post(
            f"{HIVE_TOOLS_URL}/ollama/structured",
            json={"prompt": prompt, "schema": schema},
            headers={"Authorization": f"Bearer {HIVE_TOOLS_TOKEN}"},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        return await asyncio.to_thread(_ask)
    except Exception:
        log.exception("ollama/structured call failed")
        return None


# ============================================================================
# Per-class pruners
# ============================================================================

async def prune_ephemeral() -> dict:
    """Ephemeral is `discard` action — nothing is stored. No-op.

    `read: 0` because it genuinely reads nothing, which is why the "every
    pruner read zero rows" check in `server.py` is `any(...)` over the four
    rather than a check on each.
    """
    return {"data_class": "ephemeral", "deleted": [], "updated": [],
            "kept": 0, "read": 0, "errors": []}


async def prune_current_state(*, now: float | None = None) -> dict:
    """Anchor-priority dispatch over current-state entries."""
    now = now if now is not None else time.time()
    entries = await _read_class_entries("current-state")
    deleted: list[int] = []
    kept = 0
    errors: list[str] = []
    no_anchor: list[dict] = []

    for e in entries:
        eid = int(e["id"])
        codebase_ref = (e.get("codebase_ref") or "").strip()
        expires_at = (e.get("expires_at") or "").strip()
        # kg_entity is not currently a separate column; deferred.

        if codebase_ref:
            if not await _codebase_ref_exists(codebase_ref):
                if await _delete_entry(eid):
                    deleted.append(eid)
                else:
                    errors.append(f"delete failed: id={eid}")
            else:
                kept += 1
        elif expires_at:
            ts = _parse_iso(expires_at)
            if ts is not None and ts < now:
                if await _delete_entry(eid):
                    deleted.append(eid)
                else:
                    errors.append(f"delete failed: id={eid}")
            else:
                kept += 1
        else:
            # No anchor → decay
            no_anchor.append(e)

    decay_deleted, decay_kept = await _decay_pass(
        no_anchor,
        half_life_days=CURRENT_STATE_HALF_LIFE_DAYS,
        delete_below=CURRENT_STATE_DELETE_BELOW,
        now=now,
    )
    deleted.extend(decay_deleted)
    kept += decay_kept

    return {
        "data_class": "current-state",
        "deleted": deleted,
        "updated": [],
        "kept": kept,
        "read": len(entries),
        "errors": errors,
    }


async def prune_future_state(*, now: float | None = None) -> dict:
    """For each entry: ask Ollama if it has shipped. If yes, delete; otherwise decay."""
    now = now if now is not None else time.time()
    entries = await _read_class_entries("future-state")
    if not entries:
        return {"data_class": "future-state", "deleted": [], "updated": [],
                "kept": 0, "read": 0, "errors": []}

    # Pull recent current-state entries (top 30 by created_at) as context for the
    # shipped check. Caller does not need full content; a representative subset
    # is enough for Ollama to decide if the planned thing is now reality.
    current_state_pool = sorted(
        await _read_class_entries("current-state"),
        key=lambda r: r.get("created_at") or 0,
        reverse=True,
    )[:30]
    context = "\n".join(
        f"- {(r.get('content') or '').strip()[:500]}"
        for r in current_state_pool
    )

    schema = {
        "type": "object",
        "properties": {
            "shipped": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["shipped", "reason"],
        "additionalProperties": False,
    }

    deleted: list[int] = []
    not_shipped: list[dict] = []
    errors: list[str] = []

    for e in entries:
        eid = int(e["id"])
        prompt = (
            "You are checking whether a planned thing has shipped.\n\n"
            "### Future-state entry (the plan)\n"
            f"{(e.get('content') or '').strip()}\n\n"
            "### Recent current-state entries (what's now true)\n"
            f"{context}\n\n"
            "Has the planned thing in the future-state entry been implemented "
            "according to the current-state entries? Default to false unless "
            "there is clear evidence the plan has been realized."
        )
        verdict = await _ollama_structured(prompt, schema)
        if verdict is None:
            errors.append(f"ollama call failed: id={eid}")
            not_shipped.append(e)
            continue
        if verdict.get("shipped") is True:
            if await _delete_entry(eid):
                deleted.append(eid)
            else:
                errors.append(f"delete failed: id={eid}")
        else:
            not_shipped.append(e)

    decay_deleted, kept = await _decay_pass(
        not_shipped,
        half_life_days=FUTURE_STATE_HALF_LIFE_DAYS,
        delete_below=FUTURE_STATE_DELETE_BELOW,
        now=now,
    )
    deleted.extend(decay_deleted)

    return {
        "data_class": "future-state",
        "deleted": deleted,
        "updated": [],
        "kept": kept,
        "read": len(entries),
        "errors": errors,
    }


async def prune_feedback(*, now: float | None = None) -> dict:
    """Decay-only over contextual-tier feedback. Standing tier is exempt."""
    now = now if now is not None else time.time()
    # include_standing=False means we skip standing-tier entries.
    entries = await _read_class_entries("feedback", include_standing=False)
    deleted, kept = await _decay_pass(
        entries,
        half_life_days=FEEDBACK_HALF_LIFE_DAYS,
        delete_below=FEEDBACK_DELETE_BELOW,
        now=now,
    )
    return {
        "data_class": "feedback",
        "deleted": deleted,
        "updated": [],
        "kept": kept,
        "read": len(entries),
        "errors": [],
    }


# ============================================================================
# Run all
# ============================================================================

STANDING_CAP = 10


async def _reset_overflow_warning_if_under_cap() -> None:
    """Remove the standing-cap-warned state file when the count drops back
    to at or below ``STANDING_CAP``. Symmetric with the always_remember.sh
    trigger that creates it on cross-up. The log root is per-mind (each mind
    ships its own auto-remember pipeline), so this only runs if
    AUTO_REMEMBER_LOG_DIR is set in the env.

    Counted in the store rather than over this service's own HTTP. The
    request carried a bearer token read from the environment and swallowed
    every failure into `return`, so a missing token or a port not yet
    listening left the warning file in place for good — and the file is what
    suppresses the next warning.
    """
    log_root = os.environ.get("AUTO_REMEMBER_LOG_DIR")
    if not log_root:
        return
    sql = (f'SELECT count(*) AS "n" FROM {qualified("memories")} '
           f'WHERE "tier" = $1')
    try:
        count = await hive_data.run(
            lambda c: c.fetchval(sql, "standing"))
    except hive_data.HiveDataError:
        log.exception("prune: could not count standing-tier memories")
        return
    warn_file = Path(log_root) / "standing-cap-warned"
    if int(count or 0) <= STANDING_CAP and warn_file.exists():
        try:
            warn_file.unlink()
        except OSError:
            log.exception("prune: could not clear %s", warn_file)


async def run_all() -> list[dict]:
    """Dispatch all four per-class pruners and aggregate the report.

    A coroutine, awaited by the scheduler. It cannot be a function run
    through `asyncio.to_thread` any more: `hive_data` refuses a pool reached
    from another event loop, and a nested `asyncio.run` gets a pool bound to
    a loop that is already closed.

    Serial rather than gathered. The pruners share one pool, two of them
    read the same `current-state` rows, and a delete in one running
    concurrently with a read in another would have the second deciding about
    rows the first has already removed.
    """
    out = [
        await prune_ephemeral(),
        await prune_current_state(),
        await prune_future_state(),
        await prune_feedback(),
    ]
    await _reset_overflow_warning_if_under_cap()
    return out
