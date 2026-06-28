"""Codex CLI rollout → per-turn ``TrainingTurn`` rows.

The per-harness consumer for the Codex side of the harness fine-tuning
dataset. It reads a Codex CLI rollout JSONL off disk, groups it into the
same per-turn rows the Claude consumer produces, and upserts one row per
turn via :func:`core.training_capture.upsert_turns` with ``harness="codex"``.

Like the Claude consumer this module is pure transcript→rows logic: it does
not fork, load ``.env``, or read a Stop payload. That orchestration lives
in each Codex mind's own Stop-hook wrapper (Mordecai's lives under
``.codex/hooks/``). Keeping the parse logic here makes it importable and
testable against fixture rollouts, and lets every Codex mind reuse one
consumer.

Rollout shape
-------------
A Codex rollout is JSONL where each line is ``{"type": ..., "payload":
..., "timestamp": ...}``. The line ``type`` is one of:

- ``session_meta`` — one per file; carries the session ``id``, the harness
  ``cli_version``, and ``base_instructions.text`` (the system prompt).
- ``turn_context`` — carries the ``model`` for the turn; the last one wins.
- ``event_msg`` — UI-stream events (token counts, agent_message, etc.).
  These mirror the response items and are skipped to avoid double-counting.
- ``response_item`` — the canonical conversation items, mirroring the
  OpenAI Responses API. ``payload.type`` is one of ``message`` (role
  ``user`` / ``assistant`` / ``developer``; content is ``input_text`` /
  ``output_text`` blocks), ``function_call`` (a tool call: ``name``,
  ``arguments`` JSON string, ``call_id``), ``function_call_output`` (a tool
  result: ``call_id``, ``output``), or ``reasoning`` (dropped).

Grain mirrors the Claude consumer: a turn spans from one human (user)
message to the next. A ``user`` message opens a new turn; ``assistant``
text, ``function_call``, and ``function_call_output`` items append to the
current turn's ``assistant_blocks`` in order.

Each ``tool_use`` block is ``{"type": "tool_use", "name": <real tool name>,
"input": {...}, "id": "..."}`` — the Codex tool name kept verbatim
(``exec_command``, ``apply_patch``, ``shell``, an MCP tool, …), with
``arguments`` decoded from its JSON string into ``input``. Capture is
**fully raw**: tool results are stored as emitted and tool identities are
preserved, because the model we are training is a specialist in driving
*this* harness.

The transforms applied at capture time:

- **Reasoning items are dropped** — Codex exposes no readable reasoning, so
  Codex rows always carry ``has_reasoning = 0``.
- **Developer / system messages are skipped from the turn body** — they are
  harness-injected scaffolding. The real system prompt is captured
  separately from ``session_meta.base_instructions``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.training_capture import (
    HARNESS_CODEX,
    TrainingTurn,
    upsert_turns,
)

# The training DB lives next to the other state databases in this repo.
# Module-relative so it resolves whether a mind runs it bare-metal or a
# container imports it at a different absolute path. Override with
# ``TRAINING_DB_PATH`` for tests or an alternate location.
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "training_turns.db"

# Message roles that are real conversational turns. ``developer`` / ``system``
# carry harness scaffolding and are excluded from the turn body.
_TURN_ROLES = frozenset({"user", "assistant"})


def default_db_path() -> Path:
    """Resolve the training DB path, honoring ``TRAINING_DB_PATH``."""
    override = os.environ.get("TRAINING_DB_PATH")
    return Path(override) if override else _DEFAULT_DB_PATH


def _content_text(content) -> str:
    """Extract message text from a Codex content value (string or blocks).

    Codex message content is a list of ``{"type": "input_text" |
    "output_text", "text": ...}`` blocks. A bare string is tolerated.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict)
            and c.get("type") in ("input_text", "output_text", "text")
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _output_text(output) -> str:
    """Flatten a ``function_call_output`` payload's ``output`` field.

    Codex emits the tool result either as a bare string or as a dict that
    wraps the text under ``output`` (sometimes alongside ``metadata``). A
    dict with no plain ``output`` string is preserved as compact JSON so no
    signal is lost.
    """
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        inner = output.get("output")
        if isinstance(inner, str):
            return inner
        return json.dumps(output, ensure_ascii=False)
    if output is None:
        return ""
    return str(output)


def _output_is_error(output) -> bool:
    """Best-effort error verdict for a ``function_call_output``.

    Codex wraps the tool result as a dict that may carry ``metadata.exit_code``
    or a ``success`` flag. A non-zero exit or ``success: false`` is an error;
    anything else (including a bare string) is treated as success so the flag
    only tightens success-gating when the signal is actually present."""
    if isinstance(output, dict):
        meta = output.get("metadata")
        if isinstance(meta, dict):
            ec = meta.get("exit_code")
            if isinstance(ec, int):
                return ec != 0
        if output.get("success") is False:
            return True
    return False


def _parse_arguments(arguments) -> dict:
    """Decode a ``function_call`` ``arguments`` value into an input dict.

    Codex stores call arguments as a JSON string. A non-decodable or
    non-object value is preserved verbatim under ``raw`` so the call is
    never dropped.
    """
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError:
            return {"raw": arguments}
        return decoded if isinstance(decoded, dict) else {"raw": arguments}
    if arguments is None:
        return {}
    return {"raw": str(arguments)}


class _TurnAccumulator:
    """Groups rollout items into per-turn ``(user_content, blocks)``.

    A new turn opens only on a ``user`` message. Assistant text, tool calls,
    and tool results append to the current turn. Items that arrive before the
    first user message attach to a leading turn with empty ``user_content``.
    """

    def __init__(self) -> None:
        self._turns: list[tuple[str, list[dict]]] = []

    def _ensure_current(self) -> list[dict]:
        if not self._turns:
            self._turns.append(("", []))
        return self._turns[-1][1]

    def start_turn(self, user_content: str) -> None:
        self._turns.append((user_content, []))

    def add_block(self, block: dict) -> None:
        self._ensure_current().append(block)

    @property
    def turns(self) -> list[tuple[str, list[dict]]]:
        return self._turns


def _parse_grouped(transcript_path: str | Path) -> list[tuple[str, list[dict]]]:
    """Group a Codex rollout JSONL into per-turn rows.

    Returns a list of ``(user_content, assistant_blocks)`` tuples in rollout
    order. ``reasoning`` items and ``developer`` / ``system`` messages are
    dropped; ``event_msg`` / ``session_meta`` / ``turn_context`` lines are
    ignored here (metadata is read separately).
    """
    path = Path(transcript_path)
    acc = _TurnAccumulator()
    try:
        raw = path.read_text()
    except OSError:
        return []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "response_item":
            continue
        payload = ev.get("payload") or {}
        ptype = payload.get("type")

        if ptype == "message":
            role = payload.get("role")
            if role not in _TURN_ROLES:
                continue
            text = _content_text(payload.get("content"))
            if role == "user":
                if text:
                    acc.start_turn(text)
            else:  # assistant
                if text:
                    acc.add_block({"type": "text", "text": text})
            continue

        if ptype == "function_call":
            acc.add_block({
                "type": "tool_use",
                "name": payload.get("name", ""),
                "input": _parse_arguments(payload.get("arguments")),
                "id": payload.get("call_id", ""),
            })
            continue

        if ptype == "function_call_output":
            acc.add_block({
                "type": "tool_result",
                "content": _output_text(payload.get("output")),
                "tool_call_id": payload.get("call_id", ""),
                "is_error": _output_is_error(payload.get("output")),
            })
            continue

        # reasoning and any other item types are intentionally dropped.
    return acc.turns


def _session_metadata(transcript_path: str | Path) -> dict:
    """Pull ``source_model``, ``harness_version``, ``system_prompt``.

    ``source_model`` is the last ``turn_context.model`` (the model the
    session ended on). ``harness_version`` and ``system_prompt`` come from
    the single ``session_meta`` line.
    """
    path = Path(transcript_path)
    model = None
    version = None
    system_prompt = None
    try:
        raw = path.read_text()
    except OSError:
        return {"source_model": None, "harness_version": None, "system_prompt": None}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        payload = ev.get("payload") or {}
        if etype == "turn_context":
            m = payload.get("model")
            if m:
                model = m
        elif etype == "session_meta":
            if payload.get("cli_version"):
                version = payload["cli_version"]
            instr = payload.get("base_instructions")
            if isinstance(instr, dict):
                system_prompt = instr.get("text")
            elif isinstance(instr, str):
                system_prompt = instr
    return {
        "source_model": model,
        "harness_version": version,
        "system_prompt": system_prompt,
    }


def build_turns(
    transcript_path: str | Path,
    *,
    session_id: str,
    mind_id: str | None = None,
    captured_at: int | None = None,
) -> list[TrainingTurn]:
    """Build the list of :class:`TrainingTurn` rows from a rollout.

    Returns an empty list when the rollout yields no turns (nothing to
    store). The ``system_prompt`` from ``session_meta`` is denormalized onto
    every row of the session.
    """
    grouped = _parse_grouped(transcript_path)
    if not grouped:
        return []
    meta = _session_metadata(transcript_path)
    stamp = captured_at if captured_at is not None else int(time.time())
    return [
        TrainingTurn.from_blocks(
            session_id=session_id,
            turn_index=idx,
            harness=HARNESS_CODEX,
            user_content=user_content,
            assistant_blocks=blocks,
            mind_id=mind_id,
            source_model=meta["source_model"],
            harness_version=meta["harness_version"],
            captured_at=stamp,
            system_prompt=meta["system_prompt"],
        )
        for idx, (user_content, blocks) in enumerate(grouped)
    ]


def capture_session(
    transcript_path: str | Path,
    *,
    session_id: str,
    mind_id: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    """Parse a rollout and upsert its turn rows. ``True`` if any written.

    No-op (returns ``False``) when the rollout has no usable turns.
    """
    turns = build_turns(transcript_path, session_id=session_id, mind_id=mind_id)
    if not turns:
        return False
    upsert_turns(db_path if db_path is not None else default_db_path(), turns)
    return True
