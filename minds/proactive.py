"""Proactive (unsolicited) delivery plumbing shared by CLI-Claude mind backends.

In the containerised deployment the mind backend (``minds/<name>/implementation``)
and the Telegram bot (``bots.telegram_bot``) run in **separate** containers, so
they cannot share an in-process ``asyncio.Queue`` the way the single-process
operator minds (Skippy, Mordecai) do.

This module is the cross-container adaptation of that fix:

* :func:`idle_drain` reads the Claude CLI subprocess stdout **only while no
  solicited request is in flight** (guarded by the session's ``stdout_lock``),
  extracts assistant ``text`` blocks from the stream-json events, and appends
  ``{"chat_id", "text"}`` items to an in-memory per-backend buffer.
* :func:`make_proactive_router` exposes ``GET /proactive`` on the backend's
  FastAPI app. The endpoint is bearer-protected and pops-and-returns the
  buffered items as JSON. Each mind's own Telegram bot polls it and posts the
  items to Telegram.

Only the **claude_cli** minds (Ada, Bob) run a long-lived subprocess with a
drainable stdout. The Codex minds (Bilby, Nagatha, Hex) spawn a fresh
subprocess per turn with stdin closed and no idle stream to drain, so they do
not start the drain — their buffer simply stays empty and ``GET /proactive``
returns ``[]``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

log = logging.getLogger("hive-mind.proactive")


def extract_assistant_texts(event: dict) -> list[str]:
    """Return the text of every ``text`` content block in an assistant event.

    Deterministically ignores non-assistant events (system, result,
    stream_event deltas, etc.) and non-text blocks (tool_use, tool_result).
    This mirrors what the solicited stream in ``send_message`` treats as
    displayable assistant output.
    """
    if event.get("type") != "assistant":
        return []
    message = event.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if text:
                texts.append(text)
    return texts


async def idle_drain(session: dict, buffer: list[dict]) -> None:
    """Buffer unsolicited assistant output while no request is live.

    Runs per CLI session for its lifetime. When a request is in flight the
    solicited reader in ``send_message`` owns stdout (it holds ``stdout_lock``),
    so this backs off. When idle it acquires the lock, reads harness
    stream-json lines, and appends assistant ``text`` blocks to ``buffer`` as
    ``{"chat_id": int, "text": str}`` items. The bot polls the ``/proactive``
    endpoint to drain ``buffer`` and deliver each item. Cancelled by the
    session kill path.
    """
    proc = session.get("proc")
    if proc is None or getattr(proc, "stdout", None) is None:
        return

    lock: asyncio.Lock = session["stdout_lock"]
    while True:
        if session.get("in_flight"):
            await asyncio.sleep(0.2)
            continue

        async with lock:
            # Re-check after acquiring: a request may have started while we
            # were waiting for the lock. If so, back off and let it read.
            if session.get("in_flight"):
                await asyncio.sleep(0.2)
                continue
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if not line:  # EOF — subprocess exited
                return
            decoded = line.decode().strip()
            if not decoded:
                continue
            try:
                event = json.loads(decoded)
            except json.JSONDecodeError:
                continue
            chat_id = session.get("chat_id")
            if not chat_id:
                continue
            try:
                chat_id_int = int(chat_id)
            except (TypeError, ValueError):
                continue
            for text in extract_assistant_texts(event):
                buffer.append({"chat_id": chat_id_int, "text": text})


def make_proactive_router(buffer: list[dict], token: str | None) -> APIRouter:
    """Return a FastAPI router exposing ``GET /proactive``.

    The endpoint drains ``buffer`` (pop-all) and returns the items as JSON. It
    is protected with the shared bearer ``token`` — the same token the backend
    already trusts on the internal docker network (``COMMS_BEARER_TOKEN``). When
    ``token`` is falsy the endpoint is unauthenticated (parity with the
    backend's other routes, which are network-trusted).
    """
    router = APIRouter()

    @router.get("/proactive")
    async def proactive(authorization: str | None = Header(default=None)):  # noqa: ANN202
        if token:
            expected = f"Bearer {token}"
            if authorization != expected:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Pop-all: hand the buffered items to the caller and empty the buffer.
        items = buffer[:]
        buffer.clear()
        return items

    return router
