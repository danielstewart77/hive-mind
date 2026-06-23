"""Tests for the inlined empty-turn diagnostic helper in the codex_cli template.

When Codex closes a turn with no `agent_message` item — typically because
the model emitted its tool call in a non-Responses dialect that landed
on the reasoning channel — the template's relay synthesises a diagnostic
assistant frame so the operator sees what the model actually produced
instead of the generic "mind stream closed with no text output"
placeholder. This file pins the helper's behaviour.

The template module reads a `runtime.yaml` next to itself at import time
(and instantiates a FastAPI app), so the helper is loaded by extracting
just its function definition from the source via ast — no module import,
no side effects.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "mind_templates"
    / "codex_cli.py"
)


def _load_helper() -> Callable[[str, str], str]:
    source = TEMPLATE_PATH.read_text()
    tree = ast.parse(source)
    func_node: ast.FunctionDef | None = None
    for node in tree.body:
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "compose_empty_turn_diagnostic"
        ):
            func_node = node
            break
    assert func_node is not None, "compose_empty_turn_diagnostic missing from template"
    module = ast.Module(body=[func_node], type_ignores=[])
    namespace: dict = {}
    exec(compile(module, str(TEMPLATE_PATH), "exec"), namespace)
    return namespace["compose_empty_turn_diagnostic"]


compose_empty_turn_diagnostic = _load_helper()


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
    raw = '[TOOL_CALLS]{"name": "exec_command", "arguments": {"cmd": "ls"}}'
    out = compose_empty_turn_diagnostic(
        last_reasoning_text=raw, last_other_item_type="command_execution"
    )
    assert raw in out
    assert "command_execution" not in out
