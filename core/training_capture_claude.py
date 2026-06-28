"""Claude Code transcript → per-turn ``TrainingTurn`` rows.

The per-harness consumer for the Claude Code side of the harness
fine-tuning dataset. It reads a Claude Code session JSONL transcript off
disk, groups it into per-turn rows defined by the data contract, and
upserts one row per turn via :func:`core.training_capture.upsert_turns`.

This module is pure transcript→rows logic. It does not fork, load ``.env``,
or read a Stop payload — that orchestration lives in each mind's own
Stop-hook wrapper (Skippy's lives under ``~/.claude/hooks/``). Keeping the
parse logic here makes it importable and testable against fixture
transcripts, and lets Ada reuse the exact same consumer.

A **turn** spans from one human (user) message to the next. A user event
bearing human text opens a new turn; a user event bearing ``tool_result``
blocks is *not* a new turn — its results append to the current turn's
``assistant_blocks``. The block array preserves the assistant's exact
ordering of ``thinking`` / ``text`` / ``tool_use`` / ``tool_result``.

Capture is **fully raw**. Tool names are kept verbatim — ``Bash``,
``Skill`` with its actual skill name, ``mcp__hive-mind-tools__graph_query``,
``Task`` with its actual ``subagent_type``. The model we are training is a
specialist in *this* hive mind, so the concrete tool identities are the most
valuable signal in the data. Any anonymization is an optional export-time
transform over this immutable raw store, never applied at capture time.

The transforms applied at capture time:

- **Readable thinking is kept** — plaintext extended-thinking blocks are
  captured as ``thinking`` blocks in their real positions.
- **Encrypted reasoning is dropped** — ``redacted_thinking`` blocks carry
  no readable signal and are never stored.
- **Sidechains are skipped** — sub-agent transcripts (``isSidechain``)
  belong to their own session; the parent keeps only the sub-agent tool
  call and its result.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.training_capture import (
    HARNESS_CLAUDE_CODE,
    TrainingTurn,
    upsert_turns,
)

# The training DB lives next to the other state databases in this repo.
# Module-relative so it resolves correctly whether Skippy runs it bare-metal
# or Ada's container imports it at a different absolute path. Override with
# ``TRAINING_DB_PATH`` for tests or an alternate location.
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "training_turns.db"


def default_db_path() -> Path:
    """Resolve the training DB path, honoring ``TRAINING_DB_PATH``."""
    override = os.environ.get("TRAINING_DB_PATH")
    return Path(override) if override else _DEFAULT_DB_PATH


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


class _TurnAccumulator:
    """Groups transcript events into per-turn ``(user_content, blocks)``.

    A new turn opens only on a genuine human user message. Assistant blocks
    and tool results append to the current turn. Blocks that arrive before
    the first human message attach to a leading turn with empty
    ``user_content``.
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
    """Group a Claude Code JSONL transcript into per-turn rows.

    Returns a list of ``(user_content, assistant_blocks)`` tuples in
    transcript order. Sidechain events and non user/assistant events are
    skipped. ``redacted_thinking`` blocks are dropped; readable ``thinking``
    blocks are kept in position.
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
        if ev.get("type") not in ("user", "assistant"):
            continue
        if ev.get("isSidechain"):
            continue
        msg = ev.get("message") or {}
        role = msg.get("role") or ev.get("type")
        content = msg.get("content")

        if role == "assistant":
            if isinstance(content, str):
                if content:
                    acc.add_block({"type": "text", "text": content})
                continue
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ctype = c.get("type")
                    if ctype == "thinking":
                        text = c.get("thinking") or ""
                        if text:
                            acc.add_block({"type": "thinking", "text": text})
                    elif ctype == "redacted_thinking":
                        continue  # encrypted; no readable signal
                    elif ctype == "text":
                        if c.get("text"):
                            acc.add_block({"type": "text", "text": c["text"]})
                    elif ctype == "tool_use":
                        acc.add_block({
                            "type": "tool_use",
                            "name": c.get("name", ""),
                            "input": c.get("input") or {},
                            "id": c.get("id", ""),
                        })
            continue

        # role == "user": human text opens a new turn; tool_result blocks
        # append to the current turn.
        if isinstance(content, str):
            if content:
                acc.start_turn(content)
            continue
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "tool_result":
                    acc.add_block({
                        "type": "tool_result",
                        "content": _tool_result_text(c.get("content")),
                        "tool_call_id": c.get("tool_use_id", ""),
                        "is_error": bool(c.get("is_error")),
                    })
                elif ctype == "text" and c.get("text"):
                    acc.start_turn(c["text"])
    return acc.turns


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
    except OSError:
        return {"source_model": None, "harness_version": None}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("version"):
            version = ev["version"]
        if ev.get("type") == "assistant":
            m = (ev.get("message") or {}).get("model")
            if m:
                model = m
    return {"source_model": model, "harness_version": version}


def build_turns(
    transcript_path: str | Path,
    *,
    session_id: str,
    mind_id: str | None = None,
    captured_at: int | None = None,
) -> list[TrainingTurn]:
    """Build the list of :class:`TrainingTurn` rows from a transcript.

    Returns an empty list when the transcript yields no turns (nothing to
    store). The ``system_prompt`` (Claude transcripts carry none here) is
    denormalized onto every row of the session.
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
            harness=HARNESS_CLAUDE_CODE,
            user_content=user_content,
            assistant_blocks=blocks,
            mind_id=mind_id,
            source_model=meta["source_model"],
            harness_version=meta["harness_version"],
            captured_at=stamp,
            system_prompt=None,
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
    """Parse a transcript and upsert its turn rows. ``True`` if any written.

    No-op (returns ``False``) when the transcript has no usable turns.
    """
    turns = build_turns(transcript_path, session_id=session_id, mind_id=mind_id)
    if not turns:
        return False
    upsert_turns(db_path if db_path is not None else default_db_path(), turns)
    return True
