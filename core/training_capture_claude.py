"""Claude Code transcript → ``TrainingExample``.

The per-harness consumer for the Claude Code side of the harness
fine-tuning dataset. It reads a Claude Code session JSONL transcript off
disk, parses it into the turn array defined in the spec, and upserts one
row via :func:`core.training_capture.upsert_example`.

This module is pure transcript→row logic. It does not fork, load ``.env``,
or read a Stop payload — that orchestration lives in each mind's own
Stop-hook wrapper (Skippy's lives under ``~/.claude/hooks/``). Keeping the
parse logic here makes it importable and testable against fixture
transcripts, and lets Ada reuse the exact same consumer.

Capture is **fully raw**. Every user turn, assistant turn, tool call, and
tool result is preserved in order, with tool results stored raw and the
real tool names kept verbatim — ``Bash``, ``Skill`` with its actual skill
name, ``mcp__hive-mind-tools__graph_query``, ``Task`` with its actual
``subagent_type``. The model we are training is a specialist in *this*
hive mind, so the concrete tool identities are the most valuable signal in
the data, not noise to be bucketed away. Any anonymization (collapsing
skill / MCP / sub-agent identifiers to category buckets) is an optional
export-time transform over this immutable raw store, applied only if a
more general model is ever wanted — never at capture time.

The only transforms applied at capture time:

- **Thinking blocks are dropped** — extended-reasoning content is pure
  token bloat for harness fidelity and is never graded, so it is not
  stored.
- **Sidechains are skipped** — sub-agent transcripts (``isSidechain``)
  belong to their own session; the parent session keeps only the sub-agent
  tool call and its result.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.training_capture import (
    HARNESS_CLAUDE_CODE,
    TrainingExample,
    upsert_example,
)

# The training DB lives next to the other state databases in this repo.
# Module-relative so it resolves correctly whether Skippy runs it bare-metal
# or Ada's container imports it at a different absolute path. Override with
# ``TRAINING_DB_PATH`` for tests or an alternate location.
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "training_examples.db"


def default_db_path() -> Path:
    """Resolve the training DB path, honoring ``TRAINING_DB_PATH``."""
    override = os.environ.get("TRAINING_DB_PATH")
    return Path(override) if override else _DEFAULT_DB_PATH


def _content_text(content) -> str:
    """Extract assistant/user text from a content value (string or blocks).

    Thinking blocks are skipped. Tool-use / tool-result blocks are handled
    by the caller, not here.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _tool_result_text(content) -> str:
    """Flatten a tool_result block's ``content`` (string or block array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                # text blocks carry ``text``; other block types (e.g. image)
                # carry no plain text and are represented by their type.
                parts.append(c.get("text") or f"[{c.get('type', 'block')}]")
            else:
                parts.append(str(c))
        return "\n".join(p for p in parts if p)
    return ""


def parse_transcript(transcript_path: str | Path) -> list[dict]:
    """Parse a Claude Code JSONL transcript into the raw turn array.

    Each turn is one of::

        {"role": "user", "content": "..."}
        {"role": "assistant", "content": "...", "tool_calls": [...]}
        {"role": "tool", "content": "...", "tool_call_id": "..."}

    Each ``tool_calls`` entry is ``{"type": <raw tool name>, "input": {...},
    "id": "..."}`` — the real tool name, kept verbatim. Turns are emitted in
    transcript order. Sidechain (sub-agent) events and non user/assistant
    events are skipped. Thinking blocks are dropped.
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
        if ev.get("type") not in ("user", "assistant"):
            continue
        if ev.get("isSidechain"):
            continue
        msg = ev.get("message") or {}
        role = msg.get("role") or ev.get("type")
        content = msg.get("content")

        if role == "assistant":
            text = _content_text(content)
            tool_calls = []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        tool_calls.append({
                            "type": c.get("name", ""),
                            "input": c.get("input") or {},
                            "id": c.get("id", ""),
                        })
            if not text and not tool_calls:
                continue
            turn: dict = {"role": "assistant", "content": text}
            if tool_calls:
                turn["tool_calls"] = tool_calls
            turns.append(turn)
            continue

        # role == "user": may carry a plain message, tool results, or both.
        if isinstance(content, str):
            if content:
                turns.append({"role": "user", "content": content})
            continue
        if isinstance(content, list):
            pending_text: list[str] = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "tool_result":
                    if pending_text:
                        turns.append({"role": "user", "content": "\n".join(pending_text)})
                        pending_text = []
                    turns.append({
                        "role": "tool",
                        "content": _tool_result_text(c.get("content")),
                        "tool_call_id": c.get("tool_use_id", ""),
                    })
                elif ctype == "text" and c.get("text"):
                    pending_text.append(c["text"])
            if pending_text:
                turns.append({"role": "user", "content": "\n".join(pending_text)})
    return turns


def _session_metadata(transcript_path: str | Path) -> dict:
    """Pull ``source_model`` and ``harness_version`` from assistant events.

    Uses the last assistant event that carries each field, so a model swap
    mid-session records the model the session ended on.
    """
    path = Path(transcript_path)
    model = None
    version = None
    try:
        raw = path.read_text()
    except Exception:
        return {"source_model": None, "harness_version": None}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("version"):
            version = ev["version"]
        if ev.get("type") == "assistant":
            m = (ev.get("message") or {}).get("model")
            if m:
                model = m
    return {"source_model": model, "harness_version": version}


def build_example(
    transcript_path: str | Path,
    *,
    session_id: str,
    mind_id: str | None = None,
    captured_at: int | None = None,
) -> TrainingExample | None:
    """Build a :class:`TrainingExample` from a transcript, or ``None``.

    Returns ``None`` when the transcript yields no turns (nothing to store).
    """
    turns = parse_transcript(transcript_path)
    if not turns:
        return None
    meta = _session_metadata(transcript_path)
    length_chars = sum(len(t.get("content") or "") for t in turns)
    return TrainingExample.from_turns(
        session_id=session_id,
        harness=HARNESS_CLAUDE_CODE,
        turns=turns,
        mind_id=mind_id,
        source_model=meta["source_model"],
        harness_version=meta["harness_version"],
        captured_at=captured_at if captured_at is not None else int(time.time()),
        system_prompt=None,
        length_tokens=length_chars // 4,
    )


def capture_session(
    transcript_path: str | Path,
    *,
    session_id: str,
    mind_id: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    """Parse a transcript and upsert one row. Returns ``True`` if written.

    No-op (returns ``False``) when the transcript has no usable turns.
    """
    example = build_example(transcript_path, session_id=session_id, mind_id=mind_id)
    if example is None:
        return False
    upsert_example(db_path if db_path is not None else default_db_path(), example)
    return True
