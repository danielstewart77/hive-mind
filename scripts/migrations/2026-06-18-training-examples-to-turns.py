#!/usr/bin/env python3
"""One-shot migration: ``training_examples`` (session grain) → ``training_turns``.

Converts the historical session-grain training DB into the per-turn schema
defined in ``docs/training-capture/data-contract.md``.

The runtime moved its database file from ``data/training_examples.db`` to
``data/training_turns.db`` when the grain changed, so this migration is
**file-to-file**: it reads the old file's ``training_examples`` table, writes
per-turn rows into the new file's ``training_turns`` table, and then removes
the superseded old file. (If source and destination resolve to the same
file, it migrates in place and drops the old table instead.)

Each old row carries a flat ``turns`` JSON array (thinking already absent in
historical data). We walk that array and group it into per-turn rows:

  - a ``role == "user"`` entry starts a new turn (its content becomes
    ``user_content``);
  - ``role == "assistant"`` entries contribute a ``text`` block (when there
    is content) followed by a ``tool_use`` block per ``tool_calls`` entry;
  - ``role == "tool"`` entries contribute a ``tool_result`` block;
  - entries before the first user entry attach to a leading turn with empty
    ``user_content``.

All migrated rows get ``has_reasoning = 0`` (old data never captured
thinking). ``session_id``, ``mind_id``, ``harness``, ``source_model``,
``harness_version``, ``captured_at``, ``system_prompt`` and the curation
columns (``quality_flag``, ``judge_verdict``, ``judge_confidence``,
``exclusion_reason``) are carried over.

The migration is idempotent: once the old file is gone (or holds no
``training_examples`` table), it no-ops.

Usage::

    python scripts/migrations/2026-06-18-training-examples-to-turns.py \
        --src data/training_examples.db --dest data/training_turns.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Import the storage layer so the new table's schema stays single-sourced.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.training_capture import (  # noqa: E402
    SCHEMA,
    TrainingTurn,
    connect,
)

_CURATION_COLUMNS = (
    "quality_flag",
    "judge_verdict",
    "judge_confidence",
    "exclusion_reason",
)


def _default_dest() -> Path:
    """The runtime's training DB path, honoring ``TRAINING_DB_PATH``."""
    override = os.environ.get("TRAINING_DB_PATH")
    return Path(override) if override else _REPO_ROOT / "data" / "training_turns.db"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _group_old_turns(old_turns: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group an old flat ``turns`` array into ``(user_content, blocks)``."""
    grouped: list[tuple[str, list[dict]]] = []

    def ensure_current() -> list[dict]:
        if not grouped:
            grouped.append(("", []))
        return grouped[-1][1]

    for entry in old_turns:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content") or ""
        if role == "user":
            grouped.append((content, []))
        elif role == "assistant":
            blocks = ensure_current()
            if content:
                blocks.append({"type": "text", "text": content})
            for call in entry.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                blocks.append({
                    "type": "tool_use",
                    "name": call.get("type", ""),
                    "input": call.get("input") or {},
                    "id": call.get("id", ""),
                })
        elif role == "tool":
            ensure_current().append({
                "type": "tool_result",
                "content": content,
                "tool_call_id": entry.get("tool_call_id", ""),
            })
    return grouped


def _insert_turn(dest: sqlite3.Connection, old: dict, idx: int,
                 user_content: str, blocks: list[dict]) -> None:
    turn = TrainingTurn.from_blocks(
        session_id=old["session_id"],
        turn_index=idx,
        harness=old["harness"],
        user_content=user_content,
        assistant_blocks=blocks,
        mind_id=old.get("mind_id"),
        source_model=old.get("source_model"),
        harness_version=old.get("harness_version"),
        captured_at=old.get("captured_at"),
        system_prompt=old.get("system_prompt"),
    )
    # Old data never captured thinking.
    turn.has_reasoning = False
    values = turn._row_values() + (
        old.get("quality_flag") or "pending",
        old.get("judge_verdict"),
        old.get("judge_confidence"),
        old.get("exclusion_reason"),
    )
    dest.execute(
        "INSERT INTO training_turns ("
        "session_id, turn_index, mind_id, harness, source_model, "
        "harness_version, captured_at, system_prompt, user_content, "
        "assistant_blocks, has_reasoning, tool_call_count, "
        "length_tokens, quality_flag, judge_verdict, "
        "judge_confidence, exclusion_reason"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_id, turn_index) DO NOTHING",
        values,
    )


def migrate(src_path: str | Path, dest_path: str | Path | None = None) -> int:
    """Migrate ``training_examples`` → ``training_turns``. Returns rows written.

    Reads the ``training_examples`` table from ``src_path`` and writes per-turn
    rows into the ``training_turns`` table at ``dest_path`` (defaulting to the
    runtime DB). When the two paths differ the old file is removed on success;
    when they are the same file the old table is dropped. No-op (returns 0)
    when there is nothing to migrate.
    """
    src = Path(src_path)
    dest = Path(dest_path) if dest_path is not None else _default_dest()
    same_file = src.exists() and dest.exists() and src.resolve() == dest.resolve()

    if not src.exists():
        print(f"no DB at {src}; nothing to migrate")
        return 0

    with connect(src) as src_conn:
        if not _has_table(src_conn, "training_examples"):
            print("training_examples table absent; already migrated, no-op")
            return 0
        old_rows = [dict(r) for r in src_conn.execute("SELECT * FROM training_examples")]

    dest.parent.mkdir(parents=True, exist_ok=True)
    with connect(dest) as dest_conn:
        dest_conn.executescript(SCHEMA)
        written = 0
        for old in old_rows:
            raw = old.get("turns")
            old_turns = json.loads(raw) if raw else []
            for idx, (user_content, blocks) in enumerate(_group_old_turns(old_turns)):
                _insert_turn(dest_conn, old, idx, user_content, blocks)
                written += 1
        dest_conn.commit()

    # Retire the superseded store. Distinct files: remove the old file whole.
    # Same file: drop the old table (the new table already lives alongside).
    if same_file:
        with connect(dest) as conn:
            conn.execute("DROP TABLE training_examples")
            conn.commit()
    else:
        src.unlink()

    print(f"migrated {written} turn rows from {len(old_rows)} sessions "
          f"into {dest}; retired {src}")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        required=True,
        help="path to the old training_examples DB to read",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="path to the new training_turns DB to write "
             "(default: the runtime DB / $TRAINING_DB_PATH)",
    )
    args = parser.parse_args()
    migrate(args.src, args.dest)


if __name__ == "__main__":
    main()
