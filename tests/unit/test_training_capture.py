"""Unit tests for the training-example storage layer.

Covers schema creation, upsert-by-session_id idempotency, count derivation
from turns, harness validation, and that re-capture preserves curation
columns assigned by a later pass.
"""

from __future__ import annotations

import json

import pytest

from core.training_capture import (
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    TrainingExample,
    connect,
    count_examples,
    get_example,
    init_db,
    upsert_example,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "nested" / "training_examples.db"


def _turns():
    return [
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content": "on it",
            "tool_calls": [
                {"type": "Bash", "input": {"command": "ls"}},
                {"type": "Read", "input": {"path": "x"}},
            ],
        },
        {"role": "tool", "content": "result", "tool_call_id": "t1"},
    ]


# ---------------------------------------------------------------------------
# schema / init
# ---------------------------------------------------------------------------

def test_init_db_creates_table_and_parent_dir(db_path):
    assert not db_path.parent.exists()
    init_db(db_path)
    assert db_path.exists()
    with connect(db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "training_examples" in names


def test_init_db_is_idempotent(db_path):
    init_db(db_path)
    init_db(db_path)  # must not raise
    assert count_examples(db_path) == 0


# ---------------------------------------------------------------------------
# from_turns derivation
# ---------------------------------------------------------------------------

def test_from_turns_derives_counts():
    ex = TrainingExample.from_turns(
        session_id="s1", harness=HARNESS_CLAUDE_CODE, turns=_turns()
    )
    assert ex.turn_count == 3
    assert ex.tool_call_count == 2


def test_from_turns_handles_missing_tool_calls():
    turns = [{"role": "user", "content": "hi"}]
    ex = TrainingExample.from_turns(
        session_id="s1", harness=HARNESS_CODEX, turns=turns
    )
    assert ex.turn_count == 1
    assert ex.tool_call_count == 0


def test_invalid_harness_rejected():
    with pytest.raises(ValueError):
        TrainingExample(session_id="s1", harness="bashrc")


# ---------------------------------------------------------------------------
# upsert / get
# ---------------------------------------------------------------------------

def test_upsert_then_get_roundtrips(db_path):
    ex = TrainingExample.from_turns(
        session_id="s1",
        harness=HARNESS_CLAUDE_CODE,
        turns=_turns(),
        mind_id="14cb820b",
        source_model="claude-opus-4-7",
        harness_version="1.0.0",
        captured_at=1781649576,
        system_prompt="you are skippy",
        length_tokens=42,
    )
    upsert_example(db_path, ex)

    got = get_example(db_path, "s1")
    assert got is not None
    assert got["session_id"] == "s1"
    assert got["harness"] == HARNESS_CLAUDE_CODE
    assert got["mind_id"] == "14cb820b"
    assert got["source_model"] == "claude-opus-4-7"
    assert got["turn_count"] == 3
    assert got["tool_call_count"] == 2
    assert got["length_tokens"] == 42
    assert got["quality_flag"] == "pending"
    assert got["turns"] == _turns()


def test_get_missing_returns_none(db_path):
    init_db(db_path)
    assert get_example(db_path, "nope") is None


def test_upsert_creates_db_if_absent(db_path):
    # No explicit init_db — upsert must stand up the schema itself.
    ex = TrainingExample.from_turns(
        session_id="s1", harness=HARNESS_CODEX, turns=_turns()
    )
    upsert_example(db_path, ex)
    assert count_examples(db_path) == 1


def test_upsert_is_idempotent_by_session_id(db_path):
    ex = TrainingExample.from_turns(
        session_id="s1", harness=HARNESS_CLAUDE_CODE, turns=_turns()
    )
    upsert_example(db_path, ex)
    upsert_example(db_path, ex)
    assert count_examples(db_path) == 1


def test_upsert_overwrites_capture_columns_and_preserves_id(db_path):
    first = TrainingExample.from_turns(
        session_id="s1",
        harness=HARNESS_CLAUDE_CODE,
        turns=_turns()[:1],
        source_model="claude-sonnet-4-6",
    )
    upsert_example(db_path, first)
    with connect(db_path) as conn:
        original_id = conn.execute(
            "SELECT id FROM training_examples WHERE session_id='s1'"
        ).fetchone()[0]

    grown = TrainingExample.from_turns(
        session_id="s1",
        harness=HARNESS_CLAUDE_CODE,
        turns=_turns(),
        source_model="claude-opus-4-7",
    )
    upsert_example(db_path, grown)

    got = get_example(db_path, "s1")
    assert count_examples(db_path) == 1
    assert got["turn_count"] == 3
    assert got["tool_call_count"] == 2
    assert got["source_model"] == "claude-opus-4-7"
    with connect(db_path) as conn:
        new_id = conn.execute(
            "SELECT id FROM training_examples WHERE session_id='s1'"
        ).fetchone()[0]
    assert new_id == original_id


def test_recapture_preserves_curation_columns(db_path):
    ex = TrainingExample.from_turns(
        session_id="s1", harness=HARNESS_CLAUDE_CODE, turns=_turns()
    )
    upsert_example(db_path, ex)
    # A later curation pass assigns a verdict.
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE training_examples SET quality_flag='clean', "
            "judge_confidence=0.9 WHERE session_id='s1'"
        )
        conn.commit()

    # Re-capture of the same session must not clobber the verdict.
    upsert_example(db_path, ex)
    got = get_example(db_path, "s1")
    assert got["quality_flag"] == "clean"
    assert got["judge_confidence"] == 0.9


# ---------------------------------------------------------------------------
# count filtering
# ---------------------------------------------------------------------------

def test_count_filters_by_harness(db_path):
    upsert_example(
        db_path,
        TrainingExample.from_turns(
            session_id="a", harness=HARNESS_CLAUDE_CODE, turns=_turns()
        ),
    )
    upsert_example(
        db_path,
        TrainingExample.from_turns(
            session_id="b", harness=HARNESS_CLAUDE_CODE, turns=_turns()
        ),
    )
    upsert_example(
        db_path,
        TrainingExample.from_turns(
            session_id="c", harness=HARNESS_CODEX, turns=_turns()
        ),
    )
    assert count_examples(db_path) == 3
    assert count_examples(db_path, harness=HARNESS_CLAUDE_CODE) == 2
    assert count_examples(db_path, harness=HARNESS_CODEX) == 1


def test_turns_stored_as_json_text(db_path):
    upsert_example(
        db_path,
        TrainingExample.from_turns(
            session_id="s1", harness=HARNESS_CODEX, turns=_turns()
        ),
    )
    with connect(db_path) as conn:
        raw = conn.execute(
            "SELECT turns FROM training_examples WHERE session_id='s1'"
        ).fetchone()[0]
    assert isinstance(raw, str)
    assert json.loads(raw) == _turns()
