"""Per-class pruning dispatcher.

Four pruners, one per class. Each function reads its class's entries
directly from the lucent SQLite store (since /memory/list doesn't
expose the anchor columns we need), applies the class-specific
strategy, and returns a per-class outcome dict.

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

import logging
import math
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

LUCENT_URL = os.environ.get("LUCENT_URL_SELF", "http://127.0.0.1:8424")
LUCENT_BEARER = os.environ.get("LUCENT_BEARER_TOKEN", "")
_AUTH_HEADERS = {"Authorization": f"Bearer {LUCENT_BEARER}"} if LUCENT_BEARER else {}
HIVE_TOOLS_URL = os.environ.get("HIVE_TOOLS_URL", "http://127.0.0.1:9421")
HIVE_TOOLS_TOKEN = os.environ.get("HIVE_TOOLS_TOKEN", "")
LUCENT_DB = Path(
    os.environ.get(
        "LUCENT_DB_PATH",
        "/data/lucent.db",
    )
)
REQUEST_TIMEOUT = 30

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


def _read_class_entries(data_class: str, *, include_standing: bool = False) -> list[dict]:
    """Read entries directly from sqlite so we get all anchor columns.

    ``/memory/list`` strips ``codebase_ref`` and ``expires_at`` from the
    response; the pruners need them.
    """
    if not LUCENT_DB.is_file():
        log.error("lucent.db not found at %s", LUCENT_DB)
        return []
    cols = (
        "id, content, data_class, tier, mind_id, source, "
        "created_at, codebase_ref, expires_at"
    )
    where = "data_class = ?"
    params: list[Any] = [data_class]
    if not include_standing:
        where += " AND tier != 'standing'"
    conn = sqlite3.connect(str(LUCENT_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT {cols} FROM memories WHERE {where}", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _delete_entry(entry_id: int) -> bool:
    try:
        resp = requests.delete(
            f"{LUCENT_URL}/memory/{entry_id}",
            headers=_AUTH_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception:
        log.exception("delete failed for id=%s", entry_id)
        return False


def _decay_pass(
    entries: list[dict], *, half_life_days: int, delete_below: float, now: float
) -> tuple[list[int], int]:
    """Apply decay to a list of entries. Returns (deleted_ids, kept_count)."""
    deleted: list[int] = []
    kept = 0
    for e in entries:
        score = _decay_score(e.get("created_at") or 0, now, half_life_days)
        if score < delete_below:
            if _delete_entry(int(e["id"])):
                deleted.append(int(e["id"]))
        else:
            kept += 1
    return deleted, kept


def _codebase_ref_exists(codebase_ref: str) -> bool:
    """Check that every path/symbol in the comma-separated codebase_ref still resolves.

    Each token is either:
    - a file path (relative or absolute) — exists on disk?
    - a symbol like ``Module.Class.method`` or ``func_name`` — at least
      one source file under the project root grep-matches it?

    The token is resolved relative to ``/home/daniel/Storage/hive_mind_skippy``
    (the mind's project root) when relative.
    """
    if not codebase_ref:
        return True  # nothing to verify
    project_root = Path("/home/daniel/Storage/hive_mind_skippy")
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
                r = subprocess.run(
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


def _ollama_structured(prompt: str, schema: dict) -> dict | None:
    """POST to /ollama/structured. Returns parsed JSON, or None on error."""
    if not HIVE_TOOLS_TOKEN:
        log.warning("HIVE_TOOLS_TOKEN not set; skipping Ollama call")
        return None
    try:
        resp = requests.post(
            f"{HIVE_TOOLS_URL}/ollama/structured",
            json={"prompt": prompt, "schema": schema},
            headers={"Authorization": f"Bearer {HIVE_TOOLS_TOKEN}"},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("ollama/structured call failed")
        return None


# ============================================================================
# Per-class pruners
# ============================================================================

def prune_ephemeral() -> dict:
    """Ephemeral is `discard` action — nothing is stored. No-op."""
    return {"data_class": "ephemeral", "deleted": [], "updated": [], "kept": 0}


def prune_current_state(*, now: float | None = None) -> dict:
    """Anchor-priority dispatch over current-state entries."""
    now = now if now is not None else time.time()
    entries = _read_class_entries("current-state")
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
            if not _codebase_ref_exists(codebase_ref):
                if _delete_entry(eid):
                    deleted.append(eid)
                else:
                    errors.append(f"delete failed: id={eid}")
            else:
                kept += 1
        elif expires_at:
            ts = _parse_iso(expires_at)
            if ts is not None and ts < now:
                if _delete_entry(eid):
                    deleted.append(eid)
                else:
                    errors.append(f"delete failed: id={eid}")
            else:
                kept += 1
        else:
            # No anchor → decay
            no_anchor.append(e)

    decay_deleted, decay_kept = _decay_pass(
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
        "errors": errors,
    }


def prune_future_state(*, now: float | None = None) -> dict:
    """For each entry: ask Ollama if it has shipped. If yes, delete; otherwise decay."""
    now = now if now is not None else time.time()
    entries = _read_class_entries("future-state")
    if not entries:
        return {"data_class": "future-state", "deleted": [], "updated": [], "kept": 0}

    # Pull recent current-state entries (top 30 by created_at) as context for the
    # shipped check. Caller does not need full content; a representative subset
    # is enough for Ollama to decide if the planned thing is now reality.
    current_state_pool = sorted(
        _read_class_entries("current-state"),
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
        verdict = _ollama_structured(prompt, schema)
        if verdict is None:
            errors.append(f"ollama call failed: id={eid}")
            not_shipped.append(e)
            continue
        if verdict.get("shipped") is True:
            if _delete_entry(eid):
                deleted.append(eid)
            else:
                errors.append(f"delete failed: id={eid}")
        else:
            not_shipped.append(e)

    decay_deleted, kept = _decay_pass(
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
        "errors": errors,
    }


def prune_feedback(*, now: float | None = None) -> dict:
    """Decay-only over contextual-tier feedback. Standing tier is exempt."""
    now = now if now is not None else time.time()
    # include_standing=False means we skip standing-tier entries.
    entries = _read_class_entries("feedback", include_standing=False)
    deleted, kept = _decay_pass(
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
        "errors": [],
    }


# ============================================================================
# Run all
# ============================================================================

def _reset_overflow_warning_if_under_cap() -> None:
    """Remove the standing-cap-warned state file when the count drops back
    to <= 10. Symmetric with the always_remember.sh trigger that creates
    it on cross-up. The log root is per-mind (each mind ships its own
    auto-remember pipeline), so this only runs if AUTO_REMEMBER_LOG_DIR
    is set in the env.
    """
    log_root = os.environ.get("AUTO_REMEMBER_LOG_DIR")
    if not log_root:
        return
    try:
        resp = requests.get(
            f"{LUCENT_URL}/memory/list",
            params={"tier": "standing", "limit": 1},
            headers=_AUTH_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        count = int(resp.json().get("total", 0))
    except Exception:
        return
    warn_file = Path(log_root) / "standing-cap-warned"
    if count <= 10 and warn_file.exists():
        try:
            warn_file.unlink()
        except Exception:
            pass


def run_all() -> list[dict]:
    """Dispatch all four per-class pruners and aggregate the report."""
    out = [
        prune_ephemeral(),
        prune_current_state(),
        prune_future_state(),
        prune_feedback(),
    ]
    _reset_overflow_warning_if_under_cap()
    return out
