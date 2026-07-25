"""Tests for the codex harness's empty-turn diagnostic helper.

When Codex closes a turn with no `agent_message` item — typically because
the model emitted its tool call in a non-Responses dialect that landed
on the reasoning channel — the harness relay synthesises a diagnostic
assistant frame so the operator sees what the model actually produced
instead of the generic "mind stream closed with no text output"
placeholder. This file pins that helper's behaviour.
"""

from __future__ import annotations

from minds.harness.empty_turn_diagnostic import compose_empty_turn_diagnostic


def test_reasoning_text_is_surfaced_verbatim() -> None:
    raw = "<|tool_call_start|>[exec_command(cmd='ls -la')]<|tool_call_end|>"
    out = compose_empty_turn_diagnostic(
        last_reasoning_text=raw, last_other_item_type=""
    )
    assert "no agent message" in out.lower()
    assert "reasoning channel" in out
    assert raw in out


def test_other_item_type_is_named_when_no_reasoning() -> None:
    out = compose_empty_turn_diagnostic(
        last_reasoning_text="", last_other_item_type="command_execution"
    )
    assert "no agent message" in out.lower()
    assert "command_execution" in out
    assert "reasoning channel" not in out


def test_minimal_diagnostic_when_nothing_captured() -> None:
    out = compose_empty_turn_diagnostic(
        last_reasoning_text="", last_other_item_type=""
    )
    assert "no agent message" in out.lower()
    assert "rollout" in out.lower()
    assert "reasoning channel" not in out


def test_reasoning_takes_precedence_over_other_item_type() -> None:
    raw = "[TOOL_CALLS]{\"name\": \"exec_command\", \"arguments\": {\"cmd\": \"ls\"}}"
    out = compose_empty_turn_diagnostic(
        last_reasoning_text=raw, last_other_item_type="command_execution"
    )
    assert raw in out
    assert "command_execution" not in out
