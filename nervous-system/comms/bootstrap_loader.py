"""Session prompt-block composition — owned by NS.

When ``comms/sessions.py::SessionManager.create_session`` builds a new
session, it calls ``compose_prompt_blocks`` here to assemble the
system-prompt content from per-mind data:

1. ``<soul>`` — from lucent KG, per-mind UUID
2. ``<recent-memory>`` — from lucent vector store, decay-weighted
   contextual entries, per-mind
3. ``<session-memory>`` — from the ``core.session_memory`` table,
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

import requests
from hive_data import core

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

# Linux caps a *single* ``execve`` argument at ``MAX_ARG_STRLEN`` — 32 pages,
# 131,072 bytes on a 4k-page host — no matter how much room ``ARG_MAX`` leaves
# for the argument list as a whole. The claude harness takes this composed
# string as one ``--append-system-prompt`` argument, so a prompt past that
# ceiling is not merely a long prompt: the spawn dies with
# ``OSError: [Errno 7] Argument list too long`` before the process exists, and
# the mind goes quiet while its service still reports healthy.
#
# This ceiling is therefore **not** a context-window budget and is never sized
# from one. A rotation's budget comes from the model's own threshold and runs
# to hundreds of kilobytes — far past what ``execve`` will carry — which is why
# ``max_bytes`` is *clamped* to this rather than trusted. Composing to a
# context budget alone is what let a 265KB carry-forward reach argv.
#
# Three quarters of the kernel ceiling, leaving the last quarter for the
# per-surface prompt the mind appends after this string, and for the flag.
_MAX_ARG_STRLEN = 32 * os.sysconf("SC_PAGESIZE")
PROMPT_MAX_BYTES = _MAX_ARG_STRLEN * 3 // 4   # 98,304 on a 4k-page host

#: Stand-in for "no ceiling" on a harness that does not pass the prompt on
#: argv. Large enough that nothing trims, finite so the arithmetic below has
#: no ``None`` case to carry.
_UNBOUNDED = 1 << 40


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


async def _fetch_session_memory(*, mind_id: str, client_ref: str | None) -> str:
    """Latest rotation carry-forward row for (mind_id, client_ref)."""
    if not client_ref:
        return ""
    try:
        row = await core.latest_session_memory(mind_id=mind_id, client_ref=client_ref)
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
    max_bytes: int | None = None,
    argv_bound: bool = True,
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

    ``argv_bound`` says whether this conversation's harness carries the prompt
    as an ``execve`` argument. Claude does, so its prompt is additionally held
    to ``PROMPT_MAX_BYTES`` — a caller naming no budget gets that ceiling
    rather than none, and a caller naming a larger one is clamped to it.
    Codex and dsh pass the prompt by stdin, a seed file and a state dict, so
    no kernel ceiling applies and imposing one would cost them a carry-forward
    for a limit that does not exist on their path.

    The ceiling binds every block that can give way, but not the soul, which
    is never trimmed. A soul alone past the ceiling is therefore composed over
    it, logged, and refused by the spawn rather than silently shipped.
    """
    if argv_bound:
        max_bytes = (PROMPT_MAX_BYTES if max_bytes is None
                     else min(max_bytes, PROMPT_MAX_BYTES))
    elif max_bytes is None:
        max_bytes = _UNBOUNDED

    blocks: list[str] = []

    soul = _fetch_soul(mind_id, mind_name)
    if soul:
        blocks.append(soul)

    recent = _fetch_recent_memory(mind_id)
    if recent:
        # Recent memory gives way only once session memory has nothing
        # left to give: it keeps its head, where the strongest rows are.
        fixed = len("\n\n".join(blocks + [""]).encode("utf-8"))
        recent = _keep_head(recent, max_bytes - fixed)
    if recent:
        blocks.append(recent)

    session_memory = await _fetch_session_memory(mind_id=mind_id, client_ref=client_ref)
    if session_memory:
        fixed = len("\n\n".join(blocks + [""]).encode("utf-8"))
        session_memory = _trim_session_memory(session_memory, max_bytes - fixed)
    if session_memory:
        blocks.append(session_memory)

    composed = "\n\n".join(blocks)
    size = len(composed.encode("utf-8"))
    if size > max_bytes:
        # Reachable only when the soul alone is past the ceiling: it is the
        # one block that never gives way, so there is nothing left to trim.
        # Not silently survivable — the spawn that carries this on argv will
        # refuse it, and this is the line that says why.
        log.error(
            "composed prompt is %d bytes, past the %d-byte ceiling, and the "
            "soul alone accounts for %d of it. The soul is never trimmed, so "
            "this cannot be composed to fit; the spawn will refuse it. Shrink "
            "the mind's soul_values.",
            size, max_bytes, len(soul.encode("utf-8")),
        )
    return composed


RECENT_MEMORY_TRIMMED = "\n[less relevant recent memory trimmed to fit the model]\n</recent-memory>"
RECENT_MEMORY_DROPPED = "<recent-memory>\n[recent memory dropped: no room]\n</recent-memory>"
SESSION_MEMORY_DROPPED = "<session-memory>\n[carry-forward dropped: no room]\n</session-memory>"


def _dropped(marker: str, room: int) -> str:
    """A block that would not fit at all, replaced by a mark saying so.

    A block that simply vanishes leaves the mind believing it has the whole
    history. The mark is the smallest honest thing that fits; when even that
    does not, the loss is at least on the record.
    """
    if len(marker.encode("utf-8")) <= room:
        return marker
    log.warning("prompt block dropped with no room even to mark it (room=%d)", room)
    return ""


def _keep_head(block: str, room: int) -> str:
    """``block`` cut to its first ``room`` bytes, marked."""
    data = block.encode("utf-8")
    if len(data) <= room:
        return block
    room -= len(RECENT_MEMORY_TRIMMED.encode("utf-8"))
    if room <= 0:
        return _dropped(RECENT_MEMORY_DROPPED, room + len(RECENT_MEMORY_TRIMMED.encode("utf-8")))
    return data[:room].decode("utf-8", errors="ignore") + RECENT_MEMORY_TRIMMED


SESSION_MEMORY_OPEN = "<session-memory>"
SESSION_MEMORY_CLOSE = "</session-memory>"
CONTINUATION_OPEN = "<pending-continuation>"
CONTINUATION_CLOSE = "</pending-continuation>"
CONTINUATION_TRIMMED = "\n[earlier part of this pending input trimmed to fit the model]\n"
CONTINUATION_DROPPED = (
    "<pending-continuation>\n[pending input dropped: no room]\n</pending-continuation>"
)
CARRY_FORWARD_TRIMMED = "\n[middle of the carry-forward trimmed to fit the model]\n"

#: How much of the surviving carry-forward is its head rather than its tail.
#: The head holds the summary and the decisions; the tail holds the latest
#: dialogue and the rotation epilogue. Both have to survive, so neither gets
#: all of the room.
_HEAD_SHARE = 0.4


def _split_continuation(block: str) -> tuple[str, str]:
    """``block`` as (session memory, pending continuation).

    ``_fetch_session_memory`` returns the two joined. They are different
    things: the carry-forward is history and can be cut, while the
    continuation is a user turn that was never answered.
    """
    marker = "\n\n" + CONTINUATION_OPEN
    i = block.find(marker)
    if i == -1:
        return block, ""
    return block[:i], block[i + 2:]


def _trim_session_memory(block: str, room: int) -> str:
    """The carry-forward cut to ``room``, keeping both its ends.

    Cutting only the tail-ward end is wrong twice over. The rendered
    carry-forward opens with ``## Summary of the previous session`` and closes
    with the rotation epilogue — the passage that tells the mind "the Summary
    above is your compressed memory ... nothing is missing". Keeping the tail
    alone therefore deletes the summary and keeps the sentence insisting it is
    there. So the head and the tail are both kept and the dialogue between
    them gives way.

    The pending continuation is never cut into. It is a user turn still
    waiting on an answer, and a trim landing inside it was leaving an
    unclosed ``<session-memory>`` wrapped around a stray
    ``</pending-continuation>``, with the line telling the mind to answer it
    cut away — the message was present and unanswerable.
    """
    if len(block.encode("utf-8")) <= room:
        return block

    memory, continuation = _split_continuation(block)
    if continuation:
        # The continuation has first call on the room, being the one thing
        # here that is owed an answer — but not all of it. A 144KB paste
        # during the rotation window otherwise starves the carry-forward to
        # nothing and still lands over the ceiling, which is the whole
        # failure this change exists to prevent.
        continuation = _keep_continuation(continuation, max(0, room // 2))
    cont_bytes = len(continuation.encode("utf-8")) + 2 if continuation else 0
    memory = _keep_head_and_tail(memory, room - cont_bytes)
    if memory and continuation:
        return f"{memory}\n\n{continuation}"
    return memory or continuation


def _keep_continuation(block: str, room: int) -> str:
    """The pending continuation cut to ``room``, keeping its header and tail.

    The header is the line telling the mind to answer this as the user's
    latest message; without it the text reads as more history. The tail is
    the most recent thing the user said. Both survive; the middle of a long
    paste does not.
    """
    if len(block.encode("utf-8")) <= room:
        return block

    open_tag = close_tag = ""
    inner = block
    if block.startswith(CONTINUATION_OPEN) and block.endswith(CONTINUATION_CLOSE):
        open_tag = CONTINUATION_OPEN + "\n"
        close_tag = "\n" + CONTINUATION_CLOSE
        inner = block[len(open_tag):len(block) - len(close_tag)]

    header, _, rest = inner.partition("\n")
    overhead = len(
        (open_tag + header + "\n" + CONTINUATION_TRIMMED + close_tag).encode("utf-8")
    )
    budget = room - overhead
    if budget <= 0:
        return _dropped(CONTINUATION_DROPPED, room)
    tail = rest.encode("utf-8")[-budget:].decode("utf-8", errors="ignore")
    return open_tag + header + "\n" + CONTINUATION_TRIMMED + tail + close_tag


def _keep_head_and_tail(block: str, room: int) -> str:
    """``block`` cut from the middle, marked, keeping its head and its tail."""
    if len(block.encode("utf-8")) <= room:
        return block

    prefix = suffix = ""
    inner = block
    if block.startswith(SESSION_MEMORY_OPEN) and block.endswith(SESSION_MEMORY_CLOSE):
        # Trim inside the tags, never across them: a cut that eats the closing
        # tag produces a block no reader can bound.
        prefix = SESSION_MEMORY_OPEN + "\n"
        suffix = "\n" + SESSION_MEMORY_CLOSE
        inner = block[len(prefix):len(block) - len(suffix)]

    overhead = len((prefix + suffix + CARRY_FORWARD_TRIMMED).encode("utf-8"))
    budget = room - overhead
    if budget <= 0:
        return _dropped(SESSION_MEMORY_DROPPED, room)

    data = inner.encode("utf-8")
    head_room = int(budget * _HEAD_SHARE)
    tail_room = budget - head_room
    head = data[:head_room].decode("utf-8", errors="ignore")
    tail = data[-tail_room:].decode("utf-8", errors="ignore") if tail_room else ""
    return prefix + head + CARRY_FORWARD_TRIMMED + tail + suffix
