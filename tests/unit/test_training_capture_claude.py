"""Unit tests for the Claude Code transcript consumer.

Covers grouping the flat transcript into per-turn rows, the assistant block
sequence (thinking / text / tool_use / tool_result in order), keeping
readable thinking, dropping redacted_thinking, tool results appending to the
current turn (not starting one), sidechain skipping, metadata extraction,
and the end-to-end ``capture_session`` upsert.
"""

from __future__ import annotations

import json

import pytest

from core.training_capture import HARNESS_CLAUDE_CODE, count_turns, get_turns
from core.training_capture_claude import (
    build_turns,
    capture_session,
    _parse_grouped,
)


def _ev(**kw) -> str:
    return json.dumps(kw)


def _assistant(content, *, model="claude-opus-4-8", version="2.1.179", sidechain=False):
    return _ev(
        type="assistant",
        isSidechain=sidechain,
        version=version,
        message={"role": "assistant", "model": model, "content": content},
    )


def _user(content, *, version="2.1.179", sidechain=False):
    return _ev(
        type="user",
        isSidechain=sidechain,
        version=version,
        message={"role": "user", "content": content},
    )


@pytest.fixture
def transcript(tmp_path):
    """A representative session: text, readable thinking, redacted thinking,
    primitive + skill + agent + mcp tool calls, tool results, a sidechain,
    and noise lines. Two human turns."""
    lines = [
        _ev(type="summary", summary="ignored"),
        _user("fix the thing"),
        _assistant([
            {"type": "thinking", "thinking": "readable reasoning, keep me"},
            {"type": "redacted_thinking", "data": "ENCRYPTED-BLOB"},
            {"type": "text", "text": "on it"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]),
        # tool_result is NOT a new turn — appends to the current turn.
        _user([
            {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"},
        ]),
        _assistant([
            {"type": "tool_use", "id": "t2", "name": "Skill",
             "input": {"skill": "hivemind:planka", "args": "list"}},
            {"type": "tool_use", "id": "t3", "name": "mcp__hive-mind-tools__graph_query",
             "input": {"query": "MATCH (n) RETURN n"}},
            {"type": "tool_use", "id": "t4", "name": "Task",
             "input": {"subagent_type": "Explore", "prompt": "look around"}},
        ]),
        # tool_result content as a block array (not a bare string); errored.
        _user([
            {"type": "tool_result", "tool_use_id": "t2",
             "content": [{"type": "text", "text": "card list"}], "is_error": True},
        ]),
        # sidechain (sub-agent) events must be skipped entirely
        _assistant([{"type": "text", "text": "subagent internal chatter"}], sidechain=True),
        _user("subagent internal prompt", sidechain=True),
        _assistant([{"type": "text", "text": "interim"}]),
        # second human turn
        _user("now ship it"),
        _assistant([{"type": "text", "text": "done"}]),
    ]
    p = tmp_path / "session-abc.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# turn grouping
# ---------------------------------------------------------------------------

def test_groups_into_two_human_turns(transcript):
    grouped = _parse_grouped(transcript)
    assert len(grouped) == 2
    assert grouped[0][0] == "fix the thing"
    assert grouped[1][0] == "now ship it"


def test_tool_result_does_not_start_a_new_turn(transcript):
    grouped = _parse_grouped(transcript)
    # All actions before "now ship it" belong to the first turn.
    block_types = [b["type"] for b in grouped[0][1]]
    assert block_types == [
        "thinking",     # readable, kept (redacted dropped)
        "text",         # "on it"
        "tool_use",     # Bash
        "tool_result",  # t1
        "tool_use",     # Skill
        "tool_use",     # mcp
        "tool_use",     # Task
        "tool_result",  # t2
        "text",         # "interim"
    ]


def test_readable_thinking_kept_redacted_dropped(transcript):
    grouped = _parse_grouped(transcript)
    blocks = grouped[0][1]
    thinking = [b for b in blocks if b["type"] == "thinking"]
    assert thinking == [{"type": "thinking", "text": "readable reasoning, keep me"}]
    blob = json.dumps(grouped)
    assert "ENCRYPTED-BLOB" not in blob


def test_sidechain_skipped(transcript):
    grouped = _parse_grouped(transcript)
    blob = json.dumps(grouped)
    assert "subagent internal" not in blob


def test_tool_use_blocks_kept_raw(transcript):
    grouped = _parse_grouped(transcript)
    blocks = grouped[0][1]
    tool_uses = [b for b in blocks if b["type"] == "tool_use"]
    names = [b["name"] for b in tool_uses]
    assert names == ["Bash", "Skill", "mcp__hive-mind-tools__graph_query", "Task"]
    assert tool_uses[0] == {
        "type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "t1"
    }
    assert tool_uses[1]["input"]["skill"] == "hivemind:planka"
    assert tool_uses[3]["input"]["subagent_type"] == "Explore"


def test_tool_result_links_id_and_flattens_blocks(transcript):
    grouped = _parse_grouped(transcript)
    blocks = grouped[0][1]
    results = [b for b in blocks if b["type"] == "tool_result"]
    assert results[0] == {
        "type": "tool_result", "content": "file.txt", "tool_call_id": "t1",
        "is_error": False,
    }
    assert results[1] == {
        "type": "tool_result", "content": "card list", "tool_call_id": "t2",
        "is_error": True,
    }


def test_leading_blocks_attach_to_empty_user_turn(tmp_path):
    """Tool results / assistant blocks before the first human message attach
    to a leading turn with empty user_content."""
    lines = [
        _assistant([{"type": "text", "text": "preamble"}]),
        _user("real prompt"),
        _assistant([{"type": "text", "text": "reply"}]),
    ]
    p = tmp_path / "lead.jsonl"
    p.write_text("\n".join(lines) + "\n")
    grouped = _parse_grouped(p)
    assert grouped[0][0] == ""
    assert grouped[0][1] == [{"type": "text", "text": "preamble"}]
    assert grouped[1][0] == "real prompt"


def test_missing_file_returns_empty(tmp_path):
    assert _parse_grouped(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# build_turns
# ---------------------------------------------------------------------------

def test_build_turns_metadata_and_indices(transcript):
    turns = build_turns(transcript, session_id="abc", mind_id="mind-uuid")
    assert len(turns) == 2
    assert [t.turn_index for t in turns] == [0, 1]
    first = turns[0]
    assert first.session_id == "abc"
    assert first.mind_id == "mind-uuid"
    assert first.harness == HARNESS_CLAUDE_CODE
    assert first.source_model == "claude-opus-4-8"
    assert first.harness_version == "2.1.179"
    assert first.has_reasoning is True
    assert first.tool_call_count == 4  # Bash + Skill + mcp + Task
    assert first.captured_at is not None
    # second turn carries no reasoning
    assert turns[1].has_reasoning is False
    assert turns[1].tool_call_count == 0


def test_build_turns_empty_transcript_returns_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert build_turns(p, session_id="x") == []


# ---------------------------------------------------------------------------
# capture_session
# ---------------------------------------------------------------------------

def test_capture_session_upserts(transcript, tmp_path):
    db = tmp_path / "data" / "training_turns.db"
    assert capture_session(transcript, session_id="abc", mind_id="m", db_path=db) is True
    assert count_turns(db, harness=HARNESS_CLAUDE_CODE) == 2
    rows = get_turns(db, "abc")
    assert rows[0]["tool_call_count"] == 4
    assert rows[0]["has_reasoning"] is True
    assert rows[1]["user_content"] == "now ship it"


def test_capture_session_idempotent(transcript, tmp_path):
    db = tmp_path / "training_turns.db"
    capture_session(transcript, session_id="abc", db_path=db)
    capture_session(transcript, session_id="abc", db_path=db)
    assert count_turns(db) == 2


def test_capture_session_noop_on_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("\n")
    db = tmp_path / "training_turns.db"
    # A no-op capture writes nothing — not even an empty DB file.
    assert capture_session(p, session_id="x", db_path=db) is False
    assert not db.exists()
