"""Unit tests for the Claude Code transcript consumer.

Covers transcript parsing into the normalized turn array, tool-call
normalization (skill / agent / MCP anonymization, primitives kept),
thinking-block dropping, sidechain skipping, metadata extraction, and the
end-to-end ``capture_session`` upsert.
"""

from __future__ import annotations

import json

import pytest

from core.training_capture import HARNESS_CLAUDE_CODE, count_examples, get_example
from core.training_capture_claude import (
    AGENT_TYPE_PLACEHOLDER,
    MCP_TOOL_TYPE,
    SKILL_NAME_PLACEHOLDER,
    build_example,
    capture_session,
    normalize_tool_call,
    parse_transcript,
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
    """A representative session: text, thinking, primitive + skill + agent +
    mcp tool calls, a tool result, a sidechain event, and noise lines."""
    lines = [
        _ev(type="summary", summary="ignored"),
        _user("fix the thing"),
        _assistant([
            {"type": "thinking", "thinking": "secret reasoning, must be dropped"},
            {"type": "text", "text": "on it"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]),
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
        # tool_result content as a block array (not a bare string)
        _user([
            {"type": "tool_result", "tool_use_id": "t2",
             "content": [{"type": "text", "text": "card list"}]},
        ]),
        # sidechain (sub-agent) turns must be skipped entirely
        _assistant([{"type": "text", "text": "subagent internal chatter"}], sidechain=True),
        _user("subagent internal prompt", sidechain=True),
        _assistant([{"type": "text", "text": "done"}]),
    ]
    p = tmp_path / "session-abc.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# normalize_tool_call
# ---------------------------------------------------------------------------

def test_primitive_kept_verbatim():
    assert normalize_tool_call("Bash", {"command": "ls"}) == {
        "type": "Bash", "input": {"command": "ls"}
    }


def test_skill_name_anonymized_structure_kept():
    out = normalize_tool_call("Skill", {"skill": "hivemind:planka", "args": "x"})
    assert out["type"] == "Skill"
    assert out["input"]["skill"] == SKILL_NAME_PLACEHOLDER
    assert out["input"]["args"] == "x"


def test_agent_subagent_type_anonymized():
    out = normalize_tool_call("Agent", {"subagent_type": "Explore", "prompt": "p"})
    assert out["type"] == "Agent"
    assert out["input"]["subagent_type"] == AGENT_TYPE_PLACEHOLDER
    assert out["input"]["prompt"] == "p"


def test_task_normalizes_to_agent_bucket():
    assert normalize_tool_call("Task", {"subagent_type": "Plan"})["type"] == "Agent"


def test_mcp_tool_bucketed():
    out = normalize_tool_call("mcp__hive-mind-tools__graph_query", {"query": "q"})
    assert out["type"] == MCP_TOOL_TYPE
    assert out["input"] == {"query": "q"}


def test_non_dict_input_tolerated():
    assert normalize_tool_call("Bash", None) == {"type": "Bash", "input": {}}


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------

def test_parse_roles_in_order(transcript):
    turns = parse_transcript(transcript)
    roles = [t["role"] for t in turns]
    assert roles == [
        "user",       # "fix the thing"
        "assistant",  # "on it" + Bash
        "tool",       # t1 result
        "assistant",  # Skill + mcp + Task
        "tool",       # t2 result
        "assistant",  # "done"
    ]


def test_thinking_blocks_dropped(transcript):
    turns = parse_transcript(transcript)
    assert all("secret reasoning" not in (t.get("content") or "") for t in turns)
    assert turns[1]["content"] == "on it"


def test_sidechain_skipped(transcript):
    turns = parse_transcript(transcript)
    blob = json.dumps(turns)
    assert "subagent internal" not in blob


def test_tool_calls_normalized_in_turn(transcript):
    turns = parse_transcript(transcript)
    skill_turn = turns[3]
    types = [c["type"] for c in skill_turn["tool_calls"]]
    assert types == ["Skill", MCP_TOOL_TYPE, "Agent"]
    assert skill_turn["tool_calls"][0]["input"]["skill"] == SKILL_NAME_PLACEHOLDER
    assert skill_turn["tool_calls"][2]["input"]["subagent_type"] == AGENT_TYPE_PLACEHOLDER


def test_tool_result_links_id_and_flattens_blocks(transcript):
    turns = parse_transcript(transcript)
    assert turns[2] == {"role": "tool", "content": "file.txt", "tool_call_id": "t1"}
    assert turns[4]["role"] == "tool"
    assert turns[4]["content"] == "card list"
    assert turns[4]["tool_call_id"] == "t2"


def test_missing_file_returns_empty(tmp_path):
    assert parse_transcript(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# build_example
# ---------------------------------------------------------------------------

def test_build_example_counts_and_metadata(transcript):
    ex = build_example(transcript, session_id="abc", mind_id="mind-uuid")
    assert ex is not None
    assert ex.session_id == "abc"
    assert ex.mind_id == "mind-uuid"
    assert ex.harness == HARNESS_CLAUDE_CODE
    assert ex.source_model == "claude-opus-4-8"
    assert ex.harness_version == "2.1.179"
    assert ex.turn_count == 6
    assert ex.tool_call_count == 4  # Bash + Skill + mcp + Task
    assert ex.captured_at is not None


def test_build_example_empty_transcript_returns_none(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert build_example(p, session_id="x") is None


# ---------------------------------------------------------------------------
# capture_session
# ---------------------------------------------------------------------------

def test_capture_session_upserts(transcript, tmp_path):
    db = tmp_path / "data" / "training_examples.db"
    assert capture_session(transcript, session_id="abc", mind_id="m", db_path=db) is True
    assert count_examples(db, harness=HARNESS_CLAUDE_CODE) == 1
    row = get_example(db, "abc")
    assert row["tool_call_count"] == 4
    assert len(row["turns"]) == 6


def test_capture_session_idempotent(transcript, tmp_path):
    db = tmp_path / "training_examples.db"
    capture_session(transcript, session_id="abc", db_path=db)
    capture_session(transcript, session_id="abc", db_path=db)
    assert count_examples(db) == 1


def test_capture_session_noop_on_empty(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("\n")
    db = tmp_path / "training_examples.db"
    # A no-op capture writes nothing — not even an empty DB file.
    assert capture_session(p, session_id="x", db_path=db) is False
    assert not db.exists()
