"""Unit tests for the training-turn storage layer.

Covers schema creation, upsert-by-(session_id, turn_index) idempotency,
derivation of has_reasoning / tool_call_count / length_tokens from blocks,
harness validation, ordered retrieval, and that re-capture preserves
curation columns assigned by a later pass.
"""

from __future__ import annotations

import json

import pytest

from core.training_capture import (
    HARNESS_CLAUDE_CODE,
    HARNESS_CODEX,
    TrainingTurn,
    connect,
    count_turns,
    get_turns,
    init_db,
    upsert_turn,
    upsert_turns,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "nested" / "training_turns.db"


def _blocks():
    return [
        {"type": "thinking", "text": "let me think"},
        {"type": "text", "text": "on it"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "t1"},
        {"type": "tool_result", "content": "file.txt", "tool_call_id": "t1"},
        {"type": "tool_use", "name": "Read", "input": {"path": "x"}, "id": "t2"},
    ]


def _turn(session_id="s1", turn_index=0, harness=HARNESS_CLAUDE_CODE, **kwargs):
    return TrainingTurn.from_blocks(
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
        user_content="do the thing",
        assistant_blocks=_blocks(),
        **kwargs,
    )


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
    assert "training_turns" in names


def test_init_db_is_idempotent(db_path):
    init_db(db_path)
    init_db(db_path)  # must not raise
    assert count_turns(db_path) == 0


def test_schema_has_has_reasoning_index(db_path):
    init_db(db_path)
    with connect(db_path) as conn:
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert "idx_training_turns_has_reasoning" in idx


# ---------------------------------------------------------------------------
# from_blocks derivation
# ---------------------------------------------------------------------------

def test_from_blocks_derives_has_reasoning_and_counts():
    turn = _turn()
    assert turn.has_reasoning is True
    assert turn.tool_call_count == 2
    assert turn.length_tokens is not None and turn.length_tokens > 0


def test_from_blocks_no_thinking_has_reasoning_false():
    turn = TrainingTurn.from_blocks(
        session_id="s1",
        turn_index=0,
        harness=HARNESS_CODEX,
        user_content="hi",
        assistant_blocks=[{"type": "text", "text": "hello"}],
    )
    assert turn.has_reasoning is False
    assert turn.tool_call_count == 0


def test_from_blocks_handles_empty_blocks():
    turn = TrainingTurn.from_blocks(
        session_id="s1", turn_index=0, harness=HARNESS_CODEX, user_content="hi"
    )
    assert turn.has_reasoning is False
    assert turn.tool_call_count == 0
    assert turn.assistant_blocks == []


def test_invalid_harness_rejected():
    with pytest.raises(ValueError):
        TrainingTurn(session_id="s1", turn_index=0, harness="bashrc")


# ---------------------------------------------------------------------------
# upsert / get
# ---------------------------------------------------------------------------

def test_upsert_turn_then_get_roundtrips(db_path):
    turn = _turn(
        mind_id="14cb820b",
        source_model="claude-opus-4-7",
        harness_version="1.0.0",
        captured_at=1781649576,
        system_prompt="you are skippy",
    )
    upsert_turn(db_path, turn)

    rows = get_turns(db_path, "s1")
    assert len(rows) == 1
    got = rows[0]
    assert got["session_id"] == "s1"
    assert got["turn_index"] == 0
    assert got["harness"] == HARNESS_CLAUDE_CODE
    assert got["mind_id"] == "14cb820b"
    assert got["source_model"] == "claude-opus-4-7"
    assert got["system_prompt"] == "you are skippy"
    assert got["user_content"] == "do the thing"
    assert got["tool_call_count"] == 2
    assert got["has_reasoning"] is True
    assert got["quality_flag"] == "pending"
    assert got["assistant_blocks"] == _blocks()


def test_get_missing_returns_empty(db_path):
    init_db(db_path)
    assert get_turns(db_path, "nope") == []


def test_upsert_creates_db_if_absent(db_path):
    # No explicit init_db — upsert must stand up the schema itself.
    upsert_turn(db_path, _turn(harness=HARNESS_CODEX))
    assert count_turns(db_path) == 1


def test_upsert_turns_writes_multiple_rows_ordered(db_path):
    turns = [
        _turn(turn_index=2),
        _turn(turn_index=0),
        _turn(turn_index=1),
    ]
    upsert_turns(db_path, turns)
    rows = get_turns(db_path, "s1")
    assert [r["turn_index"] for r in rows] == [0, 1, 2]
    assert count_turns(db_path) == 3


def test_upsert_is_idempotent_by_session_and_index(db_path):
    upsert_turn(db_path, _turn())
    upsert_turn(db_path, _turn())
    assert count_turns(db_path) == 1


def test_upsert_overwrites_capture_columns_and_preserves_id(db_path):
    first = TrainingTurn.from_blocks(
        session_id="s1",
        turn_index=0,
        harness=HARNESS_CLAUDE_CODE,
        user_content="do the thing",
        assistant_blocks=[{"type": "text", "text": "on it"}],
        source_model="claude-sonnet-4-6",
    )
    upsert_turn(db_path, first)
    with connect(db_path) as conn:
        original_id = conn.execute(
            "SELECT id FROM training_turns WHERE session_id='s1' AND turn_index=0"
        ).fetchone()[0]

    grown = _turn(source_model="claude-opus-4-7")
    upsert_turn(db_path, grown)

    rows = get_turns(db_path, "s1")
    assert count_turns(db_path) == 1
    assert rows[0]["tool_call_count"] == 2
    assert rows[0]["source_model"] == "claude-opus-4-7"
    with connect(db_path) as conn:
        new_id = conn.execute(
            "SELECT id FROM training_turns WHERE session_id='s1' AND turn_index=0"
        ).fetchone()[0]
    assert new_id == original_id


def test_recapture_preserves_curation_columns(db_path):
    upsert_turn(db_path, _turn())
    # A later curation pass assigns a verdict.
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE training_turns SET quality_flag='clean', "
            "judge_confidence=0.9 WHERE session_id='s1' AND turn_index=0"
        )
        conn.commit()

    # Re-capture of the same turn must not clobber the verdict.
    upsert_turn(db_path, _turn())
    rows = get_turns(db_path, "s1")
    assert rows[0]["quality_flag"] == "clean"
    assert rows[0]["judge_confidence"] == 0.9


def test_distinct_sessions_share_turn_index(db_path):
    upsert_turn(db_path, _turn(session_id="a", turn_index=0))
    upsert_turn(db_path, _turn(session_id="b", turn_index=0))
    assert count_turns(db_path) == 2


# ---------------------------------------------------------------------------
# count filtering / storage encoding
# ---------------------------------------------------------------------------

def test_count_filters_by_harness(db_path):
    upsert_turn(db_path, _turn(session_id="a", harness=HARNESS_CLAUDE_CODE))
    upsert_turn(db_path, _turn(session_id="b", harness=HARNESS_CLAUDE_CODE))
    upsert_turn(db_path, _turn(session_id="c", harness=HARNESS_CODEX))
    assert count_turns(db_path) == 3
    assert count_turns(db_path, harness=HARNESS_CLAUDE_CODE) == 2
    assert count_turns(db_path, harness=HARNESS_CODEX) == 1


def test_blocks_stored_as_json_text_and_has_reasoning_as_int(db_path):
    upsert_turn(db_path, _turn(harness=HARNESS_CODEX))
    with connect(db_path) as conn:
        raw, hr = conn.execute(
            "SELECT assistant_blocks, has_reasoning FROM training_turns "
            "WHERE session_id='s1'"
        ).fetchone()
    assert isinstance(raw, str)
    assert json.loads(raw) == _blocks()
    assert hr == 1
