"""HTTP client for the hive-comms gateway.

Used by ``bots/telegram_bot.py``. Skill discovery (``bots/skills.py``)
and per-chat asyncio primitives (``bots/bot_utils.py``) used to live
here and were split out in Phase B7.
"""

import json
import logging
import time
from typing import Any, AsyncGenerator

import aiohttp

from hive_surfaces.hive_logging import log_event, log_if_slow

log = logging.getLogger("hive-mind.gateway-client")

# Emitted by query_stream between two content blocks, as its own piece, so
# every caller can concatenate the stream plainly. A caller that supplies the
# separator itself cannot: it sees token deltas and whole blocks on one
# channel with nothing distinguishing them.
BLOCK_SEPARATOR = "\n\n"

# What marks a run of the mind's own reasoning on a chat surface. A surface has
# only text to work with, so the two are told apart by a word rather than by
# styling — which also survives being read aloud. Reasoning is shown at all
# only when the provider sends it as readable text: a thinking block that
# carries none, redacted or encrypted, yields nothing and is never announced.
THINKING_LABEL = "(thinking) "


def _delta_prose(delta: dict) -> tuple[str | None, str]:
    """What a partial event carries for a reader, and which kind it is.

    Prose and reasoning are the two kinds a chat surface shows. Anything else a
    harness streams — a tool call being assembled, a signature, a redacted
    thinking payload with no text in it — answers ``(None, "")``, which is the
    rule stated once rather than per harness: readable text is shown, and what
    is not readable is not announced either.
    """
    kind = delta.get("type")
    if kind == "text_delta":
        return "text", str(delta.get("text") or "")
    if kind == "thinking_delta":
        return "reasoning", str(delta.get("thinking") or "")
    return None, ""


def _block_prose(block: dict) -> tuple[str | None, str]:
    """The same judgement for a whole buffered content block."""
    kind = block.get("type")
    if kind == "text":
        return "text", str(block.get("text") or "")
    if kind == "thinking":
        return "reasoning", str(block.get("thinking") or "")
    return None, ""


class GatewayClient:
    """HTTP client for the Hive Mind gateway server."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        server_url: str,
        owner_type: str,
        surface_prompt: str | None = None,
        *,
        mind_id: str,
        bearer_token: str | None = None,
        rename_token: str | None = None,
    ):
        self.http = http
        self.server_url = server_url
        self.owner_type = owner_type
        self.surface_prompt = surface_prompt
        self.mind_id = mind_id
        self._bearer_token = bearer_token
        # The one small credential that may change a conversation's name. Held
        # separately from the service bearer on purpose: a copy of this one
        # escaping into a log or a screenshot is worth exactly one renamed
        # conversation, where the service token opens every route on the hive.
        self._rename_token = rename_token

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}

    async def find_active_session(
        self, user_id: int, client_ref: int | str
    ) -> str | None:
        """Look up an active session for this client. Returns the session ID
        if one exists, or ``None`` if there is no active session.

        Unlike :meth:`ensure_session`, this never creates a new session.
        """
        async with self.http.get(
            f"{self.server_url}/sessions",
            params={"client_type": self.owner_type, "client_ref": str(client_ref)},
            headers=self._auth_headers,
        ) as resp:
            data = await resp.json()
            for s in data:
                if s.get("is_active"):
                    return s["id"]
        return None

    async def ensure_session(self, user_id: int, client_ref: int | str) -> str:
        """Get active session for this client, or create one."""
        async with self.http.get(
            f"{self.server_url}/sessions",
            params={"client_type": self.owner_type, "client_ref": str(client_ref)},
            headers=self._auth_headers,
        ) as resp:
            data = await resp.json()
            for s in data:
                if s.get("is_active"):
                    log_event(
                        log, "gateway.session.reused", level=logging.DEBUG,
                        session_id=s["id"], mind_id=self.mind_id,
                        owner_type=self.owner_type, client_ref=str(client_ref),
                    )
                    return s["id"]

        payload: dict = {
            "owner_type": self.owner_type,
            "owner_ref": str(user_id),
            "client_ref": str(client_ref),
            "mind_id": self.mind_id,
        }
        if self.surface_prompt:
            payload["surface_prompt"] = self.surface_prompt
        async with self.http.post(
            f"{self.server_url}/sessions", json=payload, headers=self._auth_headers,
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                log_event(
                    log, "gateway.session.create.failed", level=logging.ERROR,
                    status_code=resp.status, mind_id=self.mind_id,
                    owner_type=self.owner_type, client_ref=str(client_ref),
                )
                raise RuntimeError(f"Gateway session creation failed: HTTP {resp.status}")
            session_id = data["id"]
            log_event(
                log, "gateway.session.created", session_id=session_id,
                mind_id=self.mind_id, owner_type=self.owner_type,
                client_ref=str(client_ref), user_id=user_id,
            )
            return session_id

    async def server_command(
        self, user_id: int, client_ref: int | str, content: str
    ) -> dict:
        """Send a server command and return the JSON response."""
        command = content.split(maxsplit=1)[0] if content else ""
        started = time.monotonic()
        async with self.http.post(
            f"{self.server_url}/command",
            json={
                "content": content,
                "owner_type": self.owner_type,
                "owner_ref": str(user_id),
                "client_ref": str(client_ref),
                "mind_id": self.mind_id,
            },
            headers=self._auth_headers,
        ) as resp:
            result = await resp.json()
            log_event(
                log, "gateway.command.completed" if resp.status < 400 else "gateway.command.failed",
                level=logging.INFO if resp.status < 400 else logging.ERROR,
                command=command, status_code=resp.status, mind_id=self.mind_id,
                owner_type=self.owner_type, client_ref=str(client_ref), user_id=user_id,
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            )
            return result

    async def interrupt_session(self, session_id: str) -> dict:
        """Send an interrupt request for a session. Returns the JSON response."""
        async with self.http.post(
            f"{self.server_url}/sessions/{session_id}/interrupt",
            headers=self._auth_headers,
        ) as resp:
            result = await resp.json()
            # Several lightweight/test transports omit a concrete status;
            # preserving the old return-only behavior is safer than making
            # observability a new failure mode.
            status = resp.status if isinstance(resp.status, int) else 200
            log_event(
                log, "gateway.session.interrupt.completed" if status < 400 else "gateway.session.interrupt.failed",
                level=logging.INFO if status < 400 else logging.ERROR,
                session_id=session_id, mind_id=self.mind_id, status_code=status,
            )
            return result

    async def suspend_session(self, session_id: str) -> dict:
        """Stop a conversation's processes but keep it resumable.

        The same route the terminal tile's suspend button calls, so a
        conversation suspended from a phone and one suspended at the desk
        reach the identical state — there is no Telegram-flavoured suspend.
        """
        async with self.http.post(
            f"{self.server_url}/sessions/{session_id}/suspend",
            headers=self._auth_headers,
        ) as resp:
            result = await resp.json()
            status = resp.status if isinstance(resp.status, int) else 200
            log_event(
                log,
                "gateway.session.suspend.completed" if status < 400 else "gateway.session.suspend.failed",
                level=logging.INFO if status < 400 else logging.ERROR,
                session_id=session_id, mind_id=self.mind_id, status_code=status,
            )
            return result

    async def rename_session(self, session_id: str, body: dict) -> int:
        """Name a conversation on the row that holds its name. Returns the status.

        The status rather than a bool, because the refusals mean different
        things and the operator has to be told which one happened: 409 is a
        conversation that has already rotated away — the button or the rename
        prompt carried an id captured when it was drawn and neither expires —
        and 503 is a gateway with no rename credential configured. Folding
        those into "it did not work" sends the operator looking at the wrong
        thing.

        The body is a partial write, so a rename carries the name alone and the
        colour picked at the tile survives without being read back first.
        """
        headers = {**self._auth_headers}
        if self._rename_token:
            headers["X-Rename-Token"] = self._rename_token
        try:
            async with self.http.put(
                f"{self.server_url}/sessions/{session_id}/name",
                json=body,
                headers=headers,
            ) as resp:
                status = resp.status if isinstance(resp.status, int) else 200
        except Exception as exc:  # noqa: BLE001 — an unreachable gateway is a refusal
            log_event(
                log, "gateway.session.rename.failed", level=logging.WARNING,
                session_id=session_id, mind_id=self.mind_id, error=str(exc),
            )
            return 0
        log_event(
            log,
            "gateway.session.rename.completed" if status < 400 else "gateway.session.rename.failed",
            level=logging.INFO if status < 400 else logging.WARNING,
            session_id=session_id, mind_id=self.mind_id, status_code=status,
        )
        return status

    async def query_stream(
        self, user_id: int, client_ref: int | str, prompt: str,
        images: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Yield assistant text chunks from the gateway SSE response as they arrive.

        Yields each assistant message block as it comes in, enabling callers
        to update a live message progressively rather than waiting for the full
        response.  Falls back to the result event text if no assistant blocks
        were received (e.g. tool-only turns).
        """
        session_id = await self.ensure_session(user_id, client_ref)
        started = time.monotonic()
        log_event(
            log, "gateway.turn.started", session_id=session_id, mind_id=self.mind_id,
            owner_type=self.owner_type, client_ref=str(client_ref), user_id=user_id,
            content_chars=len(prompt), image_count=len(images or []),
        )
        yielded_any = False
        result_fallback = ""

        # SSE streams can be very long-lived (docker builds, long
        # tool calls, etc.), so override the default aiohttp timeouts.
        sse_timeout = aiohttp.ClientTimeout(total=0, sock_read=0)
        payload: dict[str, Any] = {"content": prompt}
        if images:
            payload["images"] = images
        async with self.http.post(
            f"{self.server_url}/sessions/{session_id}/message",
            json=payload,
            timeout=sse_timeout,
            headers=self._auth_headers,
        ) as resp:
            if resp.status != 200:
                error_text = ""
                try:
                    data = await resp.json()
                    if isinstance(data, dict):
                        error_text = str(data.get("error", ""))
                    else:
                        error_text = str(data)
                except Exception:
                    error_text = await resp.text()
                error_text = error_text or f"HTTP {resp.status}"
                log_event(
                    log, "gateway.turn.failed", level=logging.ERROR,
                    session_id=session_id, mind_id=self.mind_id, status_code=resp.status,
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise RuntimeError(
                    f"Gateway message request failed for session {session_id}: {error_text}"
                )
            buf = ""
            # When a mind spawns claude with --include-partial-messages, we
            # receive stream_event events containing per-token text_delta
            # payloads in addition to the buffered `assistant` event at the
            # end of each content block. Prefer the deltas when present and
            # suppress the buffered text to avoid duplication.
            saw_partial_text = False
            # Tracked apart from prose: a turn whose reasoning streamed but
            # whose answer did not still needs the buffered answer, and one
            # whose answer streamed must not get its reasoning twice.
            saw_partial_thinking = False
            # Identity of the content block currently being streamed, so a
            # move to a new block emits the paragraph break the mind meant
            # and a continuation of the same block emits nothing.
            current_block: tuple[int, object] | None = None
            block_epoch = 0
            # Trailing newlines already carried by the text yielded so far, so
            # a block that ends with its own newline does not get a break on
            # top of one.
            tail_newlines = 0

            def separator() -> str:
                need = len(BLOCK_SEPARATOR) - tail_newlines
                return "\n" * need if need > 0 else ""

            async for chunk in resp.content.iter_any():
                buf += chunk.decode()
                while "\n" in buf:
                    raw_line, buf = buf.split("\n", 1)
                    raw_line = raw_line.strip()
                    if not raw_line or not raw_line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(raw_line.removeprefix("data: "))
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if event.get("parent_tool_use_id"):
                        # A delegate's own turn, forwarded by the harness on
                        # the same stream. It is not the mind speaking, and
                        # relaying it puts a sub-agent's prose in the mind's
                        # voice — the terminal speaker already refuses this.
                        continue
                    if etype == "stream_event":
                        # Anthropic-shaped partial event. We care about
                        # text_delta payloads inside content_block_delta.
                        inner = event.get("event", {})
                        inner_type = inner.get("type")
                        if inner_type in (
                            "message_start", "content_block_start",
                            "message_stop", "content_block_stop",
                        ):
                            # A block index is only unique within one message,
                            # so count the frames around a block rather than
                            # trusting the index alone to tell two apart. Both
                            # ends are counted so losing either kind upstream
                            # still leaves two blocks distinguishable.
                            block_epoch += 1
                        elif inner_type == "content_block_delta":
                            delta = inner.get("delta", {})
                            kind, text = _delta_prose(delta)
                            if kind is not None and text:
                                # The kind is part of the block identity, so a
                                # move between reasoning and answer breaks the
                                # paragraph even inside one block index.
                                block = (block_epoch, inner.get("index"), kind)
                                if yielded_any and block != current_block:
                                    gap = separator()
                                    if gap:
                                        yield gap
                                        tail_newlines = len(gap)
                                if kind == "reasoning" and (
                                    current_block is None or current_block[2] != "reasoning"
                                ):
                                    # Once per run of reasoning, not per delta.
                                    yield THINKING_LABEL
                                    yielded_any = True
                                    tail_newlines = 0
                                current_block = block
                                yield text
                                tail_newlines = len(text) - len(text.rstrip("\n"))
                                yielded_any = True
                                if kind == "reasoning":
                                    saw_partial_thinking = True
                                else:
                                    saw_partial_text = True
                    elif etype == "assistant":
                        # Per block rather than per event: a harness that
                        # streamed its answer but buffered its reasoning, or
                        # the reverse, has one of the two still to deliver.
                        for block in event.get("message", {}).get("content", []):
                            kind, text = _block_prose(block)
                            if kind is None or not text:
                                continue
                            if kind == "text" and saw_partial_text:
                                continue
                            if kind == "reasoning" and saw_partial_thinking:
                                continue
                            if yielded_any:
                                gap = separator()
                                if gap:
                                    yield gap
                            if kind == "reasoning":
                                yield THINKING_LABEL
                            yielded_any = True
                            yield text
                            tail_newlines = len(text) - len(text.rstrip("\n"))
                    elif etype == "result":
                        result_fallback = event.get("result", "")

        if not yielded_any and result_fallback:
            yield result_fallback
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        log_event(
            log, "gateway.turn.completed", session_id=session_id, mind_id=self.mind_id,
            elapsed_ms=elapsed_ms,
            yielded_text=yielded_any, used_result_fallback=bool(not yielded_any and result_fallback),
        )
        log_if_slow(
            log, "gateway.turn.slow", elapsed_ms, session_id=session_id,
            mind_id=self.mind_id,
        )

    async def query(self, user_id: int, client_ref: int | str, prompt: str,
                    images: list[dict] | None = None) -> str:
        """Send a query and return the complete response text (non-streaming)."""
        texts: list[str] = []
        async for text in self.query_stream(user_id, client_ref, prompt, images=images):
            texts.append(text)
        # Plain concatenation: query_stream emits its own block separators,
        # and its pieces can split mid-word.
        combined = "".join(texts)
        if not combined:
            raise RuntimeError(
                f"Empty response from gateway for user={user_id} client_ref={client_ref}: "
                "stream produced no text and no result fallback."
            )
        return combined
