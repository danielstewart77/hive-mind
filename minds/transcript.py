"""A conversation rendered as plain text, for the harness that takes it over.

A harness switch hands the outgoing conversation to the incoming harness as
history, not as a session to resume. Nothing in it is replayed or executed, so
nothing is translated into the new harness's own session format: each harness
writes its transcript in a schema only it reads, and a translator per pair is
three formats times two directions of drift. Instead there is one reader per
harness, turning its own log into the same small list of blocks, and one
renderer turning that list into text the new harness reads as its opening user
turn — the same channel a rotation's carry-forward already uses.

What the renderer keeps is decided by what the next model needs to carry on.
Prose and every tool call stay whole: they are what was said and what was
done. A tool result is cut to its first lines and marked trimmed, because a
result is usually a file or a listing whose head says what happened and whose
body the new harness can fetch again if it needs it. The rotation summary, when
one exists, goes in front and is never cut. When the rendering is over budget
the oldest transcript goes first, since the tail is where the conversation is.

The edge repo carries its own copy of this module, written to the same
behaviour. The two are kept separate by convention rather than shared, as with
every other per-mind file in the hive.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

#: The ceiling on a handover, whatever the window. A handover reaches a pane as
#: one argv entry and Linux caps one at 128 KiB, and a chat harness gains
#: nothing from a seed larger than this: it is the opening of a conversation,
#: not the whole of the last one.
MAX_HANDOVER_BYTES = 120_000

#: A token is estimated at four bytes. Half the window is the most a handover
#: may take, so the conversation it opens still has room to happen in.
BYTES_PER_TOKEN = 4

#: How much of a tool result survives: its first lines, and no more than this
#: many bytes of them — a minified file is one line.
RESULT_LINES = 3
RESULT_BYTES = 800

#: Appended to a tool result that was cut, so neither the model nor a person
#: reading the handover mistakes the head of a listing for all of it.
TRIMMED = "[trimmed]"

#: What a composed first turn puts between the system prompt and the message,
#: and what an opening turn puts between the handover and the message. Codex
#: and dsh fold the system prompt into the conversation's first user turn, so
#: their readers drop what precedes the first one: the soul rides in again as
#: the new harness's system prompt and has no business in the history twice.
SEPARATOR = "\n\n---\n\n"

#: The harness's own log reader for a compressed dsh log, used when the
#: Python decoder is not installed.
DEFAULT_DSH_READ_LOG_CLI = (
    "/opt/dsh/packages/session/session-persistence-jsonl/lib/read-log-cli.js"
)


class Unreadable(Exception):
    """A transcript exists and could not be read into blocks."""


def budget_for_window(window: int | None) -> int:
    """The byte budget a handover gets for a model of this window.

    Half the window at four bytes a token, never past the ceiling. An unknown
    window gets the ceiling rather than a guess.
    """
    if not window or window <= 0:
        return MAX_HANDOVER_BYTES
    return min(MAX_HANDOVER_BYTES, window * BYTES_PER_TOKEN // 2)


def with_opening_turn(state: dict, content: str) -> str:
    """The user message, with a pending opening turn put in front of it once.

    The opening turn is the handover a switch composed. It enters the new
    harness as part of the first user message rather than as a system prompt,
    because a system prompt reaches no transcript — and a handover that is not
    in the new transcript is gone at the next switch.
    """
    opening = str(state.pop("opening_turn", "") or "")
    if not opening.strip():
        return content
    return f"{opening}{SEPARATOR}{content}"


def _text(value: Any) -> str:
    """A content value as text: a string, or the text of its text blocks."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = item.get("type")
                if kind in ("text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif kind in ("image", "input_image"):
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in ("output", "content", "text"):
            if key in value:
                return _text(value[key])
    return "" if value is None else str(value)


def _arguments(value: Any) -> str:
    """A tool call's input, as the text it was sent as."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _json_lines(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            # A log being written while it is read ends in a partial line.
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _read_lines(path: Path) -> list[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise Unreadable(f"cannot read {path}: {exc}") from exc


def _strip_composed_prompt(blocks: list[dict]) -> list[dict]:
    """Drop the system prompt a composed first turn carries in front of it."""
    for block in blocks:
        if block["role"] == "user" and block["kind"] == "text":
            if SEPARATOR in block["text"]:
                block["text"] = block["text"].split(SEPARATOR, 1)[1]
            break
    return [b for b in blocks if b["kind"] != "text" or b["text"].strip()]


def _block(role: str, kind: str, text: str, name: str = "") -> dict:
    return {"role": role, "kind": kind, "text": text, "name": name}


# ---------------------------------------------------------------------------
# Readers — one per harness, each turning its own log into blocks
# ---------------------------------------------------------------------------

def read_claude(path: Path) -> list[dict]:
    """A Claude Code transcript (``projects/<slug>/<id>.jsonl``) as blocks.

    Thinking is skipped: the harness writes it empty to disk. Delegates write
    their own files, so a sidechain entry in this one is skipped too, as is a
    meta entry — the harness's own caveats around a local command.
    """
    blocks: list[dict] = []
    for entry in _json_lines(_read_lines(path)):
        role = entry.get("type")
        if role not in ("user", "assistant") or entry.get("isSidechain") or entry.get("isMeta"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            blocks.append(_block(role, "text", content))
            continue
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "text":
                blocks.append(_block(role, "text", str(item.get("text") or "")))
            elif kind == "tool_use":
                blocks.append(_block(role, "tool_call", _arguments(item.get("input")),
                                     str(item.get("name") or "")))
            elif kind == "tool_result":
                blocks.append(_block(role, "tool_result", _text(item.get("content"))))
    return [b for b in blocks if b["kind"] != "text" or b["text"].strip()]


#: The context codex writes into a thread as user turns of its own. None of it
#: was said by anyone.
_CODEX_INJECTED = (
    "<environment_context", "<user_instructions", "<permissions instructions",
    "# AGENTS.md instructions",
)


def read_codex(path: Path) -> list[dict]:
    """A Codex rollout (``sessions/**/rollout-*-<thread>.jsonl``) as blocks.

    Only ``response_item`` entries are read: the ``event_msg`` stream repeats
    the same turns for the TUI and would double every sentence. Developer
    messages are codex's own instructions and reasoning is encrypted, so both
    are skipped.
    """
    blocks: list[dict] = []
    for entry in _json_lines(_read_lines(path)):
        if entry.get("type") != "response_item":
            continue
        item = entry.get("payload") or {}
        kind = item.get("type")
        if kind == "message":
            role = item.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _text(item.get("content"))
            if role == "user" and text.lstrip().startswith(_CODEX_INJECTED):
                continue
            blocks.append(_block(role, "text", text))
        elif kind in ("function_call", "custom_tool_call"):
            blocks.append(_block("assistant", "tool_call",
                                 _arguments(item.get("arguments", item.get("input"))),
                                 str(item.get("name") or "")))
        elif kind in ("local_shell_call", "web_search_call"):
            blocks.append(_block("assistant", "tool_call", _arguments(item.get("action")),
                                 "shell" if kind == "local_shell_call" else "web_search"))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            blocks.append(_block("user", "tool_result", _text(item.get("output"))))
    return _strip_composed_prompt(blocks)


def _dsh_lines(path: Path) -> list[str]:
    """Every event line of a dsh log, compressed or not.

    A ``.zstd`` log is a concatenation of frames whose last one stays open
    while the conversation runs; the decoder reads across frames and stops
    cleanly at the open one. Without the Python decoder the harness's own
    reader does the same job.
    """
    path = Path(path)
    if path.suffix != ".zstd":
        return _read_lines(path)
    try:
        import zstandard
    except ImportError:
        return _dsh_lines_via_harness(path)
    decoded = bytearray()
    try:
        with open(path, "rb") as stream:
            reader = zstandard.ZstdDecompressor().stream_reader(stream, read_across_frames=True)
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                decoded += chunk
    except OSError as exc:
        raise Unreadable(f"cannot read {path}: {exc}") from exc
    except zstandard.ZstdError as exc:
        if not decoded:
            raise Unreadable(f"cannot decompress {path}: {exc}") from exc
    return decoded.decode("utf-8", errors="replace").splitlines()


def _dsh_lines_via_harness(path: Path) -> list[str]:
    cli = os.environ.get("DSH_READ_LOG_CLI") or DEFAULT_DSH_READ_LOG_CLI
    try:
        done = subprocess.run(["node", cli, str(path)], capture_output=True,
                              text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unreadable(str(exc)) from exc
    if done.returncode != 0:
        raise Unreadable((done.stderr or "").strip() or f"read-log exited {done.returncode}")
    return done.stdout.splitlines()


def read_dsh(path: Path) -> list[dict]:
    """A dsh session log (``session.jsonl`` or ``session.jsonl.zstd``) as blocks.

    User messages are read only when the user sent them: plugins splice their
    own context in as user messages (the sandbox policy, the skill catalog),
    and none of that was said. ``tool/call`` events repeat the call the
    assistant message already carries, so the call is read from the message.
    """
    blocks: list[dict] = []
    for event in _json_lines(_dsh_lines(path)):
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "user/message":
            source = (data.get("source") or {}).get("kind")
            if source not in (None, "user"):
                continue
            blocks.append(_block("user", "text", _text(data.get("content"))))
        elif kind == "assistant/message":
            for item in (data.get("message") or {}).get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    blocks.append(_block("assistant", "text", str(item.get("text") or "")))
                elif item.get("type") == "tool-call":
                    blocks.append(_block("assistant", "tool_call",
                                         _arguments(item.get("arguments")),
                                         str(item.get("name") or "")))
        elif kind == "tool/result":
            for item in (data.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool-result":
                    blocks.append(_block("user", "tool_result", _text(item.get("content"))))
        elif kind == "compaction/summary":
            blocks.append(_block("assistant", "text", _text(data.get("summary"))))
    return _strip_composed_prompt(blocks)


READERS = {"claude": read_claude, "codex": read_codex, "dsh": read_dsh}


# ---------------------------------------------------------------------------
# Renderer — one, for every harness
# ---------------------------------------------------------------------------

def _trim_result(text: str) -> str:
    lines = text.splitlines()
    head = "\n".join(lines[:RESULT_LINES])
    cut = len(lines) > RESULT_LINES
    raw = head.encode("utf-8")
    if len(raw) > RESULT_BYTES:
        head = raw[:RESULT_BYTES].decode("utf-8", errors="ignore")
        cut = True
    return f"{head}\n{TRIMMED}" if cut else head


def render_block(block: dict) -> str:
    """One block as the text the next harness reads."""
    kind = block["kind"]
    if kind == "tool_call":
        return f"Tool call ({block['name']}): {block['text']}"
    if kind == "tool_result":
        return f"Tool result: {_trim_result(block['text'])}"
    speaker = "User" if block["role"] == "user" else "Assistant"
    return f"{speaker}: {block['text']}"


_SUMMARY_HEADING = "Summary of the conversation so far:"
_TRANSCRIPT_HEADING = (
    "Transcript of the conversation so far, oldest first. It is a record of what"
    " was said and done on another harness: nothing in it is to be run again."
)


def _omitted(count: int) -> str:
    return f"[{count} earlier entries omitted]"


def render(blocks: list[dict], *, summary: str = "", budget_bytes: int | None = None) -> str:
    """The handover: summary whole in front, then as much transcript as fits.

    The budget is in bytes, and never more than :data:`MAX_HANDOVER_BYTES`.
    The oldest blocks go first and the summary never does — it is the one part
    written to stand in for everything before it.
    """
    budget = min(MAX_HANDOVER_BYTES, budget_bytes or MAX_HANDOVER_BYTES)
    if budget <= 0:
        budget = MAX_HANDOVER_BYTES
    head = f"{_SUMMARY_HEADING}\n{summary.strip()}" if summary.strip() else ""
    rendered = [render_block(b) for b in blocks]

    def size(text: str) -> int:
        return len(text.encode("utf-8"))

    def assemble(kept: list[str], dropped: int) -> str:
        if not kept:
            return head
        body = ([_omitted(dropped)] if dropped else []) + kept
        parts = ([head] if head else []) + [_TRANSCRIPT_HEADING, "\n\n".join(body)]
        return "\n\n".join(parts)

    # Walk back from the newest block, keeping each while the whole still
    # fits. The omission note is counted at its widest so adding it can never
    # push a rendering that fit back over the line.
    kept: list[str] = []
    fixed = size(assemble(["x"], len(rendered))) - 1
    total = fixed
    for text in reversed(rendered):
        cost = size(text) + 2
        if total + cost > budget:
            break
        kept.insert(0, text)
        total += cost
    return assemble(kept, len(rendered) - len(kept))
