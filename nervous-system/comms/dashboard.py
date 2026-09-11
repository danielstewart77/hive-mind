"""The hive-wide live feed a dashboard reads, separate from the session stream.

`SessionManager.stream_session_events` exists for one observer watching one
conversation, and it is the wrong shape for a page showing every mind at
once in three specific ways:

*It is shaped for one reader.* The dashboard shows every mind at once and
wants the whole turn — prose, the tool call, what came back, and the
sub-mind turns underneath — because a conversation inside a ten-minute
build is *doing* something and a column carrying only speech renders it as
nothing at all. Blocks are therefore typed rather than filtered, so a
reader can tell a command from a sentence. The route that serves them is
admin-guarded for exactly this reason: what crosses it is command output
and file contents.

*It drops silently.* A full observer queue discards its oldest entry and the
events carry no ordering, so a fast mind or a slow reader yields prose that
joins two halves of different sentences and reads perfectly. Every block
here carries a sequence, and the buffer reports the earliest it still holds,
so a gap is something a reader can see rather than something it cannot.

*It has no notion of "generating".* Neither does anything else: `status` is
written to 'running' at creation and on a model switch, and nothing ever
writes it back, so a dashboard keyed on it shows a column for every session
that has ever taken a turn. Generating is therefore tracked explicitly, with
a start, an end, and an expiry for the turns whose end never arrives —
a mind killed mid-turn, or this process restarting.

State lives in memory and is a view, not a record. The transcript is the
durable thing; this is what is happening right now.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Optional

#: How long a *framed* conversation stays generating with nothing heard.
#: The chat path brackets every turn — `user` opens it, `result` closes it —
#: so silence in the middle is a tool chain working and the ceiling only has
#: to catch the turns whose end never arrives: a mind killed mid-turn, or
#: this process restarting.
GENERATING_TTL_SECONDS = 900.0

#: How long an *unframed* conversation stays generating. A terminal has no
#: brackets at all — `publish_pty_text` emits bare assistant blocks and never
#: a `result` — so silence is the only end-of-turn signal there is, and it
#: has to be read as one. At the framed ceiling a pane that went quiet this
#: morning would outrank four minds actually producing, because the column
#: selection is oldest-first: stale terminals would hold every slot.
UNFRAMED_TTL_SECONDS = 90.0

#: No assistant text for this long, while still generating, is *quiet* —
#: reported, not removed. Silence inside a long build is not the turn ending,
#: and a column that vanished would say it was.
QUIET_AFTER_SECONDS = 45.0

#: One block's ceiling, in bytes. Before the feed carried tool traffic the
#: largest thing it could hold was a sentence; now a single `tool_result`
#: can be a whole file — measured at 1,267,087 bytes on this hive, and an
#: image renders to hundreds of KB of base64. Four hundred of those per
#: conversation, retained after the conversation ends, is the process.
MAX_BLOCK_BYTES = 120_000

#: Blocks kept per conversation. A page open for days cannot hold every
#: block of every conversation, and the sequence numbers are what make the
#: bound safe to have.
BUFFER_BLOCKS = 400

#: How long a finished conversation's text stays readable, so a tile that
#: reconnects just after a turn ended still renders the end of it.
RETAIN_AFTER_END_SECONDS = 600.0


def _blocks_of(event: dict) -> list[dict]:
    """Everything this event did, one typed block at a time.

    Two shapes arrive. `send_message` yields harness events whose
    `message.content` is a list of blocks; a terminal's tailer yields blocks
    already typed, which take `observe_blocks` instead and never come
    through here.

    A block carries its `kind` rather than being dropped for having one:
    `tool_use` is the command, `tool_result` is what came back, and both are
    the substance of a turn that is working rather than talking. `thinking`
    is carried when it holds anything, which on disk it never does — the
    harness writes the text empty beside its signature — so it costs a
    branch and nothing else.

    An event with no content at all yields nothing. `result` closes a turn
    and says nothing; appending an empty block for it would put a blank row
    in the column on every turn.
    """
    kind = event.get("type")
    if kind not in ("assistant", "user"):
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if content is None:
        # A terminal tailer's whole-block shape: prose directly on the event.
        text = event.get("content")
        if isinstance(text, str) and text.strip():
            return [_block("text", text)]
        return []
    if isinstance(content, str):
        return [_block("text", content)] if content.strip() else []
    if not isinstance(content, list):
        return []

    out: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text") or ""
            if text.strip():
                out.append(_block("text", text))
        elif block_type == "thinking":
            text = block.get("thinking") or ""
            if text.strip():
                out.append(_block("thinking", text))
        elif block_type == "tool_use":
            out.append(
                _block("tool_use", _rendered(block.get("input")), name=block.get("name"))
            )
        elif block_type == "tool_result":
            out.append(_block("tool_result", _rendered(block.get("content"))))
    return out


def _rendered(value) -> str:
    """A tool's input or result as one string, whatever shape it arrived in."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if value is None:
        return ""
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def _capped(text: str) -> tuple[str, bool]:
    """One block's text, trimmed to something the buffer can hold.

    The tail is kept rather than the head: the end of a command's output is
    where the error is. A trim is reported rather than done silently, since
    a reader who cannot tell truncation from a short result reads the wrong
    conclusion off the screen.
    """
    raw = text.encode("utf-8", "replace")
    if len(raw) <= MAX_BLOCK_BYTES:
        return text, False
    return raw[-MAX_BLOCK_BYTES:].decode("utf-8", "replace"), True


def _block(kind: str, text: str, *, name=None, agent=None, trimmed: bool = False) -> dict:
    """One block in the shape every reader of this feed expects."""
    return {
        "kind": kind,
        "text": text,
        "name": name,
        "agent": agent,
        "trimmed": trimmed,
    }


class _Conversation:
    """One session's live view: its text, its ordering, and its liveness."""

    __slots__ = (
        "mind_id",
        "conversation_id",
        "blocks",
        "next_seq",
        "first_available_seq",
        "generating",
        "started_at",
        "last_event_at",
        "last_block_at",
        "awaiting_tool",
        "ended_at",
        "framed",
    )

    def __init__(self, mind_id: str, conversation_id: Optional[str], now: float):
        self.mind_id = mind_id
        self.conversation_id = conversation_id
        self.blocks: deque = deque()
        self.next_seq = 0
        self.first_available_seq = 1
        self.generating = True
        self.started_at = now
        self.last_event_at = now
        #: When this conversation last put *anything* on the feed — a
        #: sentence, a command, a result. Quiet is measured from here, not
        #: from the last thing said: a turn grinding through a build is
        #: working, and a column that vanished would report it as finished.
        self.last_block_at: Optional[float] = None
        #: A tool call has gone out and nothing has come back. The turn is
        #: working, however quiet the transcript is.
        self.awaiting_tool = False
        self.ended_at: Optional[float] = None
        #: Whether this conversation's turns arrive bracketed by `user` and
        #: `result`. False for a pty-hosted conversation, whose prose arrives
        #: as bare assistant blocks with no end-of-turn event at all.
        self.framed = False


class LiveFeed:
    """Every conversation currently producing, and what it has said.

    Deliberately synchronous and lock-guarded rather than async: every
    caller is already inside an event loop and each operation is a few
    dict writes, so an await here would buy nothing and add ordering
    hazards to the one structure whose ordering is the point.
    """

    def __init__(
        self,
        *,
        generating_ttl: float = GENERATING_TTL_SECONDS,
        unframed_ttl: float = UNFRAMED_TTL_SECONDS,
        quiet_after: float = QUIET_AFTER_SECONDS,
        buffer_blocks: int = BUFFER_BLOCKS,
        retain_after_end: float = RETAIN_AFTER_END_SECONDS,
    ):
        self._generating_ttl = generating_ttl
        self._unframed_ttl = unframed_ttl
        self._quiet_after = quiet_after
        self._buffer_blocks = buffer_blocks
        self._retain_after_end = retain_after_end
        self._lock = threading.Lock()
        self._conversations: dict[str, _Conversation] = {}

    # -- writes --------------------------------------------------------

    def begin(
        self,
        session_id: str,
        *,
        mind_id: str = "",
        conversation_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        """This conversation is producing as of now.

        A *different* conversation id under the same session is a rotation:
        a terminal rotation keeps the session row and swaps the harness
        conversation beneath it, so the previous conversation's words must
        not stay on screen under a context count that has just reset. The
        same id is simply the next turn, and clearing there would blank the
        column on every user message.
        """
        moment = time.time() if now is None else now
        with self._lock:
            existing = self._conversations.get(session_id)
            rotated = (
                existing is not None
                and conversation_id is not None
                and existing.conversation_id is not None
                and existing.conversation_id != conversation_id
            )
            if existing is None or rotated:
                self._conversations[session_id] = _Conversation(
                    mind_id, conversation_id, moment
                )
                return
            if mind_id:
                existing.mind_id = mind_id
            if conversation_id is not None:
                existing.conversation_id = conversation_id
            existing.generating = True
            existing.ended_at = None
            existing.last_event_at = moment

    def observe(
        self, session_id: str, event: dict[str, Any], now: Optional[float] = None
    ) -> None:
        """Take everything this event did, typed.

        An event that produced no block still counts as life — `result`
        closes a turn, `user` opens one — so it pushes the expiry back
        without appending anything.
        """
        self.observe_blocks(session_id, _blocks_of(event), now=now)

    def observe_blocks(
        self, session_id: str, blocks: list[dict], now: Optional[float] = None
    ) -> None:
        """Append blocks a caller has already typed.

        The terminal's path. A pty publishes no harness events — its
        keystrokes are raw bytes — so the mind tails the harness transcript
        and posts what it finds already shaped, rather than synthesising
        events here for `observe` to take apart again.

        A block for a conversation the feed never opened is dropped.
        Opening one here would put a column on screen for a session comms
        has no record of, which is a worse answer than a missing column.
        """
        moment = time.time() if now is None else now
        with self._lock:
            conversation = self._conversations.get(session_id)
            if conversation is None:
                return
            conversation.last_event_at = moment
            for block in blocks:
                kind = block.get("kind") or "text"
                text, cut = _capped(block.get("text") or "")
                conversation.next_seq += 1
                conversation.blocks.append(
                    {
                        "seq": conversation.next_seq,
                        "kind": kind,
                        "text": text,
                        "name": block.get("name"),
                        "agent": block.get("agent"),
                        "trimmed": bool(block.get("trimmed")) or cut,
                        "at": moment,
                    }
                )
                conversation.last_block_at = moment
                # A tool call with nothing back yet is the one silence a
                # turn cannot help: the harness writes nothing between
                # issuing it and its result, and a terminal has no `result`
                # event to frame the turn. Without this an unframed
                # conversation expires at 90s in the middle of a ten-minute
                # build and the console drops the column.
                if kind == "tool_use":
                    conversation.awaiting_tool = True
                elif kind == "tool_result":
                    conversation.awaiting_tool = False
                while len(conversation.blocks) > self._buffer_blocks:
                    dropped = conversation.blocks.popleft()
                    conversation.first_available_seq = dropped["seq"] + 1

    def frame(self, session_id: str) -> None:
        """This conversation's turns are bracketed — it will say when it ends.

        Set by the chat path, which publishes `user` and `result` around
        every turn. Its absence is what marks a pty conversation, whose
        liveness can only be inferred from silence.
        """
        with self._lock:
            conversation = self._conversations.get(session_id)
            if conversation is not None:
                conversation.framed = True

    def end(self, session_id: str, now: Optional[float] = None) -> None:
        """This conversation has stopped producing."""
        moment = time.time() if now is None else now
        with self._lock:
            conversation = self._conversations.get(session_id)
            if conversation is None:
                return
            conversation.generating = False
            conversation.ended_at = moment

    def forget(self, session_id: str) -> None:
        """Drop a conversation entirely — it was closed, not merely quiet."""
        with self._lock:
            self._conversations.pop(session_id, None)

    def sweep(self, now: Optional[float] = None) -> int:
        """Drop conversations that are neither live nor recent.

        Without this the process accumulates a buffer for every session it
        has ever seen, which on a hive running for weeks is the whole
        history in memory.
        """
        moment = time.time() if now is None else now
        removed = 0
        with self._lock:
            for session_id, conversation in list(self._conversations.items()):
                if self._generating(conversation, moment):
                    continue
                last = conversation.ended_at or conversation.last_event_at
                if moment - last > self._retain_after_end:
                    del self._conversations[session_id]
                    removed += 1
        return removed

    # -- reads ---------------------------------------------------------

    def _generating(self, conversation: _Conversation, now: float) -> bool:
        """Whether this conversation is still producing.

        Two ceilings, because the two paths give different evidence. A chat
        turn says when it ends, so silence inside one is the turn working and
        the ceiling is generous. A terminal turn never says — there is no
        `result` on the pty path — so silence is the only signal available
        and has to be read as the end, or a pane quiet since breakfast holds
        a column all day ahead of minds that are actually talking.
        """
        if not conversation.generating:
            return False
        # An unanswered tool call earns the generous ceiling whichever path
        # this conversation arrived on. A pane that died mid-call then holds
        # its column until the framed ceiling, which is the right way round:
        # a stale column is a nuisance, and a column that vanishes while the
        # work is running is the thing this page exists to prevent.
        framed = conversation.framed or conversation.awaiting_tool
        ceiling = self._generating_ttl if framed else self._unframed_ttl
        return (now - conversation.last_event_at) <= ceiling

    def since(self, session_id: str, seq: int) -> list[dict]:
        """Every block after `seq`, oldest first.

        A reader holding a `seq` below `first_available_seq` has missed a
        run; `state` reports that boundary so the gap can be drawn.
        """
        with self._lock:
            conversation = self._conversations.get(session_id)
            if conversation is None:
                return []
            return [dict(block) for block in conversation.blocks if block["seq"] > seq]

    def state(self, session_id: str, now: Optional[float] = None) -> dict:
        """This conversation's liveness, ordering boundary and timings."""
        moment = time.time() if now is None else now
        with self._lock:
            conversation = self._conversations.get(session_id)
            if conversation is None:
                return {
                    "session_id": session_id,
                    "generating": False,
                    "quiet": False,
                    "known": False,
                }
            generating = self._generating(conversation, moment)
            reference = conversation.last_block_at or conversation.started_at
            return {
                "session_id": session_id,
                "known": True,
                "mind_id": conversation.mind_id,
                "conversation_id": conversation.conversation_id,
                "generating": generating,
                # Quiet only means something while generating: a finished
                # conversation is not "quiet", it is done, and reporting it
                # as quiet would put it back in front of the operator as
                # something to worry about.
                "quiet": generating and (moment - reference) > self._quiet_after,
                "latest_seq": conversation.next_seq,
                "first_available_seq": conversation.first_available_seq,
                "started_at": conversation.started_at,
                "last_block_at": conversation.last_block_at,
            }

    def live(self, now: Optional[float] = None) -> list[dict]:
        """Every conversation still generating, oldest first.

        The ordering is the start time and it is stable on purpose: the
        console shows a bounded number of columns, and a set that reshuffled
        on each poll would move a conversation out of view mid-sentence.
        """
        moment = time.time() if now is None else now
        with self._lock:
            live = [
                session_id
                for session_id, conversation in self._conversations.items()
                if self._generating(conversation, moment)
            ]
            live.sort(key=lambda sid: self._conversations[sid].started_at)
        return [self.state(session_id, now=moment) for session_id in live]
