"""Session prompt-block composition — owned by NS.

When ``comms/sessions.py::SessionManager.create_session`` builds a new
session, it calls ``compose_prompt_blocks`` here to assemble the
system-prompt content from per-mind data:

1. ``<soul>`` — from lucent KG, per-mind UUID
2. ``<recent-memory>`` — from lucent vector store, decay-weighted
   contextual entries, per-mind
3. ``<session-memory>`` — from comms's own ``session_memory`` table,
   the latest rotation carry-forward for this ``(mind_id, client_ref)``

Standing rules are no longer composed here — they are injected per-turn
by the mind's UserPromptSubmit hook (~/.claude/hooks/contextual_retrieval.py).
This keeps the standing block hot across the whole session rather than
freezing it at spawn time, and makes the rule set editable mid-session.

The composed string is shipped to the mind's ``mind_server`` via the
``system_prompt_blocks`` field of the ``/sessions`` POST. The mind
passes it straight through to ``claude --append-system-prompt`` — no
mind-side composition.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import aiosqlite
import requests

log = logging.getLogger(__name__)

LUCENT_URL = os.environ.get("LUCENT_URL_HIVE", "http://hive-lucent:8424")
def _auth_headers() -> dict:
    """Read per call, not at import. A credential read once at import needs a
    process restart to pick up a re-mint, which contradicts the promise the
    store makes — a revocation lands on the next request with nothing to
    restart."""
    token = os.environ.get("LUCENT_BEARER_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}

RECENT_MEMORY_CHAR_CAP = 6000    # ~1500 tokens
REQUEST_TIMEOUT = 5              # seconds — never hang spawn


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    if "\n" in head:
        head = head.rsplit("\n", 1)[0]
    return f"{head}\n…"


def _get(path: str, params: dict[str, Any]) -> Any | None:
    try:
        resp = requests.get(
            f"{LUCENT_URL}{path}",
            params=params,
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("lucent %s failed (params=%s)", path, params)
        return None


def _post(path: str, body: dict[str, Any]) -> Any | None:
    """POST a body to lucent. Used where the payload is a name or a prompt.

    A name on a query string lands verbatim in Zeek's http.log, in uvicorn's
    access log and in Loki. The body is logged nowhere.
    """
    try:
        resp = requests.post(
            f"{LUCENT_URL}{path}",
            json=body,
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("lucent %s failed", path)
        return None


def _fetch_soul(mind_id: str, mind_name: str) -> str:
    """Query the KG for the Mind node's soul_values; format as <soul> block."""
    entity_name = mind_name.capitalize()
    body = _post("/graph/query", {"names": [entity_name], "depth": 1})
    results = (body or {}).get("results") or []
    if not results or not results[0].get("found"):
        return ""
    matches = results[0].get("matches") or []
    if not matches:
        return ""
    soul_values = (matches[0].get("properties") or {}).get("soul_values") or []
    if not soul_values:
        return ""
    return "\n".join(["<soul>"] + list(soul_values) + ["</soul>"])


def _fetch_recent_memory(mind_id: str) -> str:
    """Top-20 decay-weighted contextual rows for this mind."""
    rows = _get("/memory/recent-decayed", {"mind_id": mind_id, "limit": 20})
    if not isinstance(rows, list) or not rows:
        return ""
    parts: list[str] = []
    for row in rows:
        cls = row.get("data_class", "?")
        content = (row.get("content") or "").strip().replace("\n", " ")
        score = row.get("score", 0.0)
        parts.append(f"- ({cls}) {content} [score={score:.2f}]")
    body = _truncate("\n".join(parts), RECENT_MEMORY_CHAR_CAP)
    return f"<recent-memory>\n{body}\n</recent-memory>"


async def _fetch_session_memory(
    db: aiosqlite.Connection, *, mind_id: str, client_ref: str | None
) -> str:
    """Latest rotation carry-forward row for (mind_id, client_ref)."""
    if not client_ref:
        return ""
    try:
        cursor = await db.execute(
            """
            SELECT body
              FROM session_memory
             WHERE mind_id = ? AND client_ref = ?
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """,
            (mind_id, client_ref),
        )
        row = await cursor.fetchone()
        await cursor.close()
    except Exception:
        log.exception("session_memory read failed (mind_id=%s, client_ref=%s)", mind_id, client_ref)
        return ""
    if not row or not row["body"]:
        return ""
    raw_body = row["body"]
    # Phase B10: rotation_check posts a JSON envelope with a pre-rendered
    # ``carry_forward`` text field (prose summary + recent dialogue +
    # continuation cue). Prefer that field; fall back to the raw JSON for
    # legacy rows (pre-B10 rotations) that don't have it.
    #
    # Late-turn handoff: the envelope may also carry a ``continuation``
    # list — user turns accepted after rotation began whose assistant
    # reply never completed before ``/clear``. Those are surfaced in a
    # distinct ``<pending-continuation>`` block (single source: rendered
    # only here, never duplicated into ``carry_forward``) so the new
    # session knows to answer them as the user's latest input.
    try:
        import json
        envelope = json.loads(raw_body)
        if isinstance(envelope, dict):
            text = envelope.get("carry_forward")
            if isinstance(text, str) and text.strip():
                blocks = [f"<session-memory>\n{text.strip()}\n</session-memory>"]
                cont_block = _render_pending_continuation(envelope.get("continuation"))
                if cont_block:
                    blocks.append(cont_block)
                return "\n\n".join(blocks)
    except (ValueError, TypeError):
        pass
    return f"<session-memory>\n{raw_body}\n</session-memory>"


def _render_pending_continuation(continuation: Any) -> str:
    """Render unanswered late user turns as a ``<pending-continuation>`` block.

    Returns "" when there is nothing pending. Each entry is a turn dict
    with a ``content`` (or legacy ``text``) field; non-user / empty
    entries are skipped.
    """
    if not isinstance(continuation, list) or not continuation:
        return ""
    items: list[str] = []
    for turn in continuation:
        if not isinstance(turn, dict):
            continue
        if turn.get("role") and turn.get("role") != "user":
            continue
        content = (turn.get("content") or turn.get("text") or "").strip()
        if content:
            items.append(f"- {content}")
    if not items:
        return ""
    header = (
        "Input received during the rotation that was not yet answered. "
        "Respond to it as the user's latest message."
    )
    return "\n".join(["<pending-continuation>", header, *items, "</pending-continuation>"])


async def compose_prompt_blocks(
    *,
    mind_id: str,
    mind_name: str,
    client_ref: str | None,
    db: aiosqlite.Connection,
    max_bytes: int | None = None,
) -> str:
    """Build the full system-prompt content for a new session.

    Returns the four data blocks concatenated. Empty blocks are dropped.
    Never raises — every fetch is best-effort and logs on failure. A
    missing component degrades the prompt but never blocks spawn.

    ``max_bytes`` is the room a rotation's successor has for all of it,
    sized from that model's threshold. Only the session memory gives way —
    it is the one block whose size is the conversation's, not the mind's —
    and it keeps its tail, where the latest exchange and anything still
    pending sit.
    """
    blocks: list[str] = []

    soul = _fetch_soul(mind_id, mind_name)
    if soul:
        blocks.append(soul)

    recent = _fetch_recent_memory(mind_id)
    if recent and max_bytes is not None:
        # Recent memory gives way only once session memory has nothing
        # left to give: it keeps its head, where the strongest rows are.
        fixed = len("\n\n".join(blocks + [""]).encode("utf-8"))
        recent = _keep_head(recent, max_bytes - fixed)
    if recent:
        blocks.append(recent)

    session_memory = await _fetch_session_memory(db, mind_id=mind_id, client_ref=client_ref)
    if session_memory and max_bytes is not None:
        fixed = len("\n\n".join(blocks + [""]).encode("utf-8"))
        session_memory = _keep_tail(session_memory, max_bytes - fixed)
    if session_memory:
        blocks.append(session_memory)

    return "\n\n".join(blocks)


RECENT_MEMORY_TRIMMED = "\n[less relevant recent memory trimmed to fit the model]\n</recent-memory>"


def _keep_head(block: str, room: int) -> str:
    """``block`` cut to its first ``room`` bytes, marked, or nothing at all."""
    data = block.encode("utf-8")
    if len(data) <= room:
        return block
    room -= len(RECENT_MEMORY_TRIMMED.encode("utf-8"))
    if room <= 0:
        return ""
    return data[:room].decode("utf-8", errors="ignore") + RECENT_MEMORY_TRIMMED


SESSION_MEMORY_TRIMMED = "<session-memory>\n[earlier carry-forward trimmed to fit the model]\n"


def _keep_tail(block: str, room: int) -> str:
    """``block`` cut to its last ``room`` bytes, marked, or nothing at all."""
    data = block.encode("utf-8")
    if len(data) <= room:
        return block
    room -= len(SESSION_MEMORY_TRIMMED.encode("utf-8"))
    if room <= 0:
        return ""
    tail = data[-room:].decode("utf-8", errors="ignore")
    return SESSION_MEMORY_TRIMMED + tail
