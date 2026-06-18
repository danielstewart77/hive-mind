"""Unit test for the training_examples → training_turns migration.

Builds a synthetic old-schema DB, runs the file-to-file migration, and
asserts the per-turn rows, curation-column carry-over, has_reasoning=0, that
the destination file holds ``training_turns``, and that the superseded source
file is removed. Also covers same-file (in-place) migration and idempotency.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION = (
    _REPO_ROOT / "scripts" / "migrations"
    / "2026-06-18-training-examples-to-turns.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_examples_to_turns", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_OLD_SCHEMA = """
CREATE TABLE training_examples (
    id                INTEGER PRIMARY KEY,
    session_id        TEXT NOT NULL UNIQUE,
    mind_id           TEXT,
    harness           TEXT NOT NULL,
    source_model      TEXT,
    harness_version   TEXT,
    captured_at       INTEGER,
    system_prompt     TEXT,
    turns             TEXT,
    turn_count        INTEGER,
    tool_call_count   INTEGER,
    length_tokens     INTEGER,
    quality_flag      TEXT NOT NULL DEFAULT 'pending',
    judge_verdict     TEXT,
    judge_confidence  REAL,
    exclusion_reason  TEXT
);
"""


def _old_turns():
    return [
        {"role": "user", "content": "fix the thing"},
        {
            "role": "assistant",
            "content": "on it",
            "tool_calls": [
                {"type": "Bash", "input": {"command": "ls"}, "id": "t1"},
            ],
        },
        {"role": "tool", "content": "file.txt", "tool_call_id": "t1"},
        {"role": "user", "content": "now ship it"},
        {"role": "assistant", "content": "done"},
    ]


def _write_old_db(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO training_examples ("
        "session_id, mind_id, harness, source_model, harness_version, "
        "captured_at, system_prompt, turns, turn_count, tool_call_count, "
        "length_tokens, quality_flag, judge_verdict, judge_confidence, "
        "exclusion_reason"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sess-1", "mind-uuid", "claude_code", "claude-opus-4-7", "2.0.0",
            1781649576, "you are skippy", json.dumps(_old_turns()), 5, 1, 99,
            "clean", "keep", 0.87, None,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def src_db(tmp_path):
    db = tmp_path / "training_examples.db"
    _write_old_db(db)
    return db


def _read_turns(dest: Path) -> list[dict]:
    conn = sqlite3.connect(str(dest))
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute("SELECT * FROM training_turns ORDER BY turn_index")
    ]
    conn.close()
    return rows


def test_migration_builds_per_turn_rows(src_db, tmp_path):
    mig = _load_migration()
    dest = tmp_path / "training_turns.db"
    written = mig.migrate(src_db, dest)
    assert written == 2  # two human turns

    rows = _read_turns(dest)
    assert [r["turn_index"] for r in rows] == [0, 1]

    first = rows[0]
    assert first["session_id"] == "sess-1"
    assert first["user_content"] == "fix the thing"
    assert first["has_reasoning"] == 0
    assert first["tool_call_count"] == 1
    blocks = json.loads(first["assistant_blocks"])
    assert [b["type"] for b in blocks] == ["text", "tool_use", "tool_result"]
    assert blocks[1]["name"] == "Bash"
    assert blocks[2]["tool_call_id"] == "t1"

    second = rows[1]
    assert second["user_content"] == "now ship it"
    assert json.loads(second["assistant_blocks"]) == [
        {"type": "text", "text": "done"}
    ]


def test_migration_carries_curation_and_metadata(src_db, tmp_path):
    mig = _load_migration()
    dest = tmp_path / "training_turns.db"
    mig.migrate(src_db, dest)
    row = next(r for r in _read_turns(dest) if r["turn_index"] == 0)
    assert row["mind_id"] == "mind-uuid"
    assert row["source_model"] == "claude-opus-4-7"
    assert row["harness_version"] == "2.0.0"
    assert row["captured_at"] == 1781649576
    assert row["system_prompt"] == "you are skippy"
    assert row["quality_flag"] == "clean"
    assert row["judge_verdict"] == "keep"
    assert row["judge_confidence"] == 0.87


def test_migration_removes_superseded_source(src_db, tmp_path):
    mig = _load_migration()
    dest = tmp_path / "training_turns.db"
    mig.migrate(src_db, dest)
    assert not src_db.exists()
    assert dest.exists()


def test_migration_same_file_drops_old_table(src_db):
    mig = _load_migration()
    # Source and destination are the same file: migrate in place.
    mig.migrate(src_db, src_db)
    conn = sqlite3.connect(str(src_db))
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "training_examples" not in names
    assert "training_turns" in names


def test_migration_idempotent(src_db, tmp_path):
    mig = _load_migration()
    dest = tmp_path / "training_turns.db"
    mig.migrate(src_db, dest)
    # Second run: the source file was removed, so it must no-op without error.
    assert mig.migrate(src_db, dest) == 0


def test_migration_noop_on_missing_db(tmp_path):
    mig = _load_migration()
    assert mig.migrate(tmp_path / "nope.db", tmp_path / "out.db") == 0
