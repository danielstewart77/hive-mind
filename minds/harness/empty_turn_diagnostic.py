"""Compose a diagnostic message when a Codex turn yields no agent message.

The Codex CLI emits assistant text as `item.completed` events whose item
type is `agent_message`. Some open-weight models drive Codex on Ollama
but emit their tool calls in a dialect Codex does not parse (Mistral
[TOOL_CALLS] prose, Llama 3 <|tool_call_start|> sentinels, etc.). Codex
files those as `agent_reasoning` items, then closes the turn with no
agent_message. The relay would otherwise yield zero assistant text,
and the Telegram bot would surface the generic placeholder
"mind stream closed with no text output". That hides the real failure
mode.

This helper composes a single diagnostic string from whatever was
captured during the turn so the operator can see what the model
actually emitted and decide whether to keep trying the model or rotate.
"""

from __future__ import annotations


def compose_empty_turn_diagnostic(
    last_reasoning_text: str,
    last_other_item_type: str,
) -> str:
    """Build the diagnostic body for a turn that produced no agent_message.

    Inputs are best-effort observations from the relay loop:
      - last_reasoning_text: text from the most recent agent_reasoning item,
        if any. Empty string if none was seen.
      - last_other_item_type: type of the most recent non-agent_message
        item.completed event, if any (e.g. "command_execution"). Empty
        string if none was seen.
    """
    parts = ["Mind produced no agent message this turn."]
    if last_reasoning_text:
        parts.append(
            "The model emitted text on the reasoning channel instead, which "
            "usually means it tried to call a tool in a dialect Codex does "
            "not parse. Raw reasoning text follows:"
        )
        parts.append(last_reasoning_text)
    elif last_other_item_type:
        parts.append(
            f"Last item type Codex received was '{last_other_item_type}'."
        )
    else:
        parts.append(
            "No reasoning or other content was captured. Check the rollout "
            "JSONL under .codex/sessions for details."
        )
    return "\n\n".join(parts)
