"""Unit tests for the per-harness skill detectors.

Both detectors are pure (transcript path in, ``set[str]`` of bare skill names
out). They reuse the already-tested transcript parsers
(``core.training_capture_claude._parse_grouped`` /
``core.training_capture_codex._parse_grouped``) so they unit-test against
fixture transcripts without a DB or a fork.

The Claude fixture builders (``_assistant`` / ``_user`` / ``_ev``) and the
Codex builders (``_line`` / ``_item`` / ``_message`` / ``_call``) mirror the
shapes in ``test_training_capture_claude.py`` / ``test_training_capture_codex.py``.
"""

from __future__ import annotations

import json

from core.skill_telemetry_detect import detect_claude_skills, detect_codex_skills


# ---------------------------------------------------------------------------
# Claude fixture builders (mirror test_training_capture_claude.py)
# ---------------------------------------------------------------------------

def _ev(**kw) -> str:
    return json.dumps(kw)


def _assistant(content, *, version="2.1.179", sidechain=False) -> str:
    return _ev(
        type="assistant",
        isSidechain=sidechain,
        version=version,
        message={"role": "assistant", "model": "claude-opus-4-8", "content": content},
    )


def _user(content, *, sidechain=False) -> str:
    return _ev(
        type="user",
        isSidechain=sidechain,
        message={"role": "user", "content": content},
    )


def _write_claude(tmp_path, lines) -> str:
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


# ---------------------------------------------------------------------------
# Codex fixture builders (mirror test_training_capture_codex.py)
# ---------------------------------------------------------------------------

def _line(type_, payload) -> str:
    return json.dumps({"type": type_, "payload": payload})


def _item(payload) -> str:
    return _line("response_item", payload)


def _cmessage(role, text) -> str:
    block_type = "output_text" if role == "assistant" else "input_text"
    return _item({"type": "message", "role": role,
                  "content": [{"type": block_type, "text": text}]})


def _call(name, args, call_id) -> str:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return _item({"type": "function_call", "name": name,
                  "arguments": arguments, "call_id": call_id})


def _output(call_id, output) -> str:
    return _item({"type": "function_call_output", "call_id": call_id,
                  "output": output})


def _write_codex(tmp_path, lines) -> str:
    p = tmp_path / "rollout.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


# ---------------------------------------------------------------------------
# Claude detector
# ---------------------------------------------------------------------------

def test_claude_detector_finds_exactly_two_skills(tmp_path):
    lines = [
        _user("do some things"),
        _assistant([
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "ls"}},
            {"type": "tool_use", "id": "t2", "name": "Skill",
             "input": {"skill": "hivemind:planka", "args": "list"}},
            {"type": "tool_use", "id": "t3", "name": "Skill",
             "input": {"skill": "sitrep"}},
        ]),
    ]
    path = _write_claude(tmp_path, lines)
    assert detect_claude_skills(path) == {"planka", "sitrep"}


def test_claude_detector_finds_slash_command(tmp_path):
    lines = [
        _user("/morning-briefing please run it"),
        _assistant([{"type": "text", "text": "on it"}]),
    ]
    path = _write_claude(tmp_path, lines)
    assert "morning-briefing" in detect_claude_skills(path)


def test_claude_detector_dedupes_repeated_skill(tmp_path):
    lines = [
        _user("/sitrep"),
        _assistant([
            {"type": "tool_use", "id": "t1", "name": "Skill",
             "input": {"skill": "sitrep"}},
            {"type": "tool_use", "id": "t2", "name": "Skill",
             "input": {"skill": "hivemind:sitrep"}},
        ]),
    ]
    path = _write_claude(tmp_path, lines)
    assert detect_claude_skills(path) == {"sitrep"}


def test_claude_detector_empty_transcript_returns_empty(tmp_path):
    lines = [
        _user("just chatting, no skills"),
        _assistant([
            {"type": "text", "text": "sure"},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "echo hi"}},
        ]),
    ]
    path = _write_claude(tmp_path, lines)
    assert detect_claude_skills(path) == set()


# ---------------------------------------------------------------------------
# Codex detector
# ---------------------------------------------------------------------------

def test_codex_detector_finds_skills_from_exec_command(tmp_path):
    lines = [
        _cmessage("user", "build the feature"),
        _call("exec_command",
              {"command": ["cat", "/usr/src/app/.codex/skills/spark-to-bloom/SKILL.md"]},
              "c1"),
        _call("exec_command",
              {"command": ["cat", "/usr/src/app/.codex/skills/git/SKILL.md"]},
              "c2"),
    ]
    path = _write_codex(tmp_path, lines)
    assert detect_codex_skills(path) == {"spark-to-bloom", "git"}


def test_codex_detector_ignores_non_skill_commands(tmp_path):
    lines = [
        _cmessage("user", "check the host file"),
        _call("exec_command", {"command": ["cat", "/etc/hosts"]}, "c1"),
        _output("c1", "127.0.0.1 localhost"),
    ]
    path = _write_codex(tmp_path, lines)
    assert detect_codex_skills(path) == set()
