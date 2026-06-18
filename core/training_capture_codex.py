"""Codex CLI rollout → ``TrainingExample``.

The per-harness consumer for the Codex side of the harness fine-tuning
dataset. It reads a Codex CLI rollout JSONL off disk, parses it into the
same normalized turn array the Claude consumer produces, and upserts one
row via :func:`core.training_capture.upsert_example` with
``harness="codex"``.

Like the Claude consumer this module is pure transcript→row logic: it does
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

Normalization mirrors the Claude consumer exactly so both harnesses land in
one schema:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [...]}
    {"role": "tool", "content": "...", "tool_call_id": "..."}

Each ``tool_calls`` entry is ``{"type": <real tool name>, "input": {...},
"id": "..."}`` — the Codex tool name kept verbatim (``exec_command``,
``apply_patch``, ``shell``, an MCP tool, …), with ``arguments`` decoded from
its JSON string into ``input``. Capture is **fully raw**: tool results are
stored as emitted and tool identities are preserved, because the model we
are training is a specialist in driving *this* harness.

The only transforms applied at capture time:

- **Reasoning items are dropped** — encrypted/extended reasoning is pure
  token bloat for harness fidelity and is never graded, so it is not
  stored. This is the Codex analog of dropping Claude thinking blocks.
- **Developer / system messages are skipped from the turn array** — they
  are harness-injected scaffolding (permissions, apps, skills preambles),
  not conversational turns. The real system prompt is captured separately
  from ``session_meta.base_instructions``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.training_capture import (
    HARNESS_CODEX,
    TrainingExample,
    upsert_example,
)

# The training DB lives next to the other state databases in this repo.
# Module-relative so it resolves whether a mind runs it bare-metal or a
# container imports it at a different absolute path. Override with
# ``TRAINING_DB_PATH`` for tests or an alternate location.
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "training_examples.db"

# Message roles that are real conversational turns. ``developer`` / ``system``
# carry harness scaffolding and are excluded from the turn array.
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
        except Exception:
            return {"raw": arguments}
        return decoded if isinstance(decoded, dict) else {"raw": arguments}
    if arguments is None:
        return {}
    return {"raw": str(arguments)}


def _append_tool_call(turns: list[dict], call: dict) -> None:
    """Attach a tool call to the open assistant turn, or open a new one.

    Consecutive ``function_call`` items (an assistant firing several tools
    before any result) accrue onto the same assistant turn. A call that
    follows a ``tool`` turn or a ``user`` turn opens a fresh assistant turn
    with empty content, matching how the model actually stepped.
    """
    if turns and turns[-1]["role"] == "assistant":
        turns[-1].setdefault("tool_calls", []).append(call)
    else:
        turns.append({"role": "assistant", "content": "", "tool_calls": [call]})


def parse_transcript(transcript_path: str | Path) -> list[dict]:
    """Parse a Codex rollout JSONL into the normalized turn array.

    Turns are emitted in rollout order. ``response_item`` lines drive the
    output; ``reasoning`` items and ``developer`` / ``system`` messages are
    dropped; ``event_msg`` / ``session_meta`` / ``turn_context`` lines are
    ignored here (metadata is read separately).
    """
    path = Path(transcript_path)
    turns: list[dict] = []
    try:
        raw = path.read_text()
    except Exception:
        return turns

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
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
            if text:
                turns.append({"role": role, "content": text})
            continue

        if ptype == "function_call":
            _append_tool_call(turns, {
                "type": payload.get("name", ""),
                "input": _parse_arguments(payload.get("arguments")),
                "id": payload.get("call_id", ""),
            })
            continue

        if ptype == "function_call_output":
            turns.append({
                "role": "tool",
                "content": _output_text(payload.get("output")),
                "tool_call_id": payload.get("call_id", ""),
            })
            continue

        # reasoning and any other item types are intentionally dropped.
    return turns


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
    except Exception:
        return {"source_model": None, "harness_version": None, "system_prompt": None}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
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


def build_example(
    transcript_path: str | Path,
    *,
    session_id: str,
    mind_id: str | None = None,
    captured_at: int | None = None,
) -> TrainingExample | None:
    """Build a :class:`TrainingExample` from a rollout, or ``None``.

    Returns ``None`` when the rollout yields no turns (nothing to store).
    """
    turns = parse_transcript(transcript_path)
    if not turns:
        return None
    meta = _session_metadata(transcript_path)
    length_chars = sum(len(t.get("content") or "") for t in turns)
    return TrainingExample.from_turns(
        session_id=session_id,
        harness=HARNESS_CODEX,
        turns=turns,
        mind_id=mind_id,
        source_model=meta["source_model"],
        harness_version=meta["harness_version"],
        captured_at=captured_at if captured_at is not None else int(time.time()),
        system_prompt=meta["system_prompt"],
        length_tokens=length_chars // 4,
    )


def capture_session(
    transcript_path: str | Path,
    *,
    session_id: str,
    mind_id: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    """Parse a rollout and upsert one row. Returns ``True`` if written.

    No-op (returns ``False``) when the rollout has no usable turns.
    """
    example = build_example(transcript_path, session_id=session_id, mind_id=mind_id)
    if example is None:
        return False
    upsert_example(db_path if db_path is not None else default_db_path(), example)
    return True
