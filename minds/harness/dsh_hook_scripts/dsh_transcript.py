#!/usr/bin/env python3
"""Read this mind's own dsh session log, in the shape its hooks expect.

The hooks read a Claude Code transcript: newline-delimited JSON, one entry
per turn, `message.usage` in snake_case. dsh writes neither of those things. The log is a
concatenated Zstandard container whose final frame is left unterminated
while the conversation runs, and its events are the harness's own schema.

So the schema difference is confined here, at the boundary, rather than
taught to five hooks separately — each of which would then carry two readers
and a branch, and drift.

Two rules this module exists to keep:

* The decompression is the harness's own. The container has no `zstd` binary
  and no Python decoder, a one-shot decompress of the whole file returns the
  header frame alone, and the frame magic occurs inside compressed payloads
  so the file cannot be split on it. The harness ships a reader that knows
  all three facts; this shells to it rather than reimplementing it.
* Absence is reported as absence. A hook that cannot read the conversation
  must say so, because every caller here has a plausible wrong answer
  available to it and no way to tell it apart from a right one.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

# The harness's own log reader, which owns the frame container's mechanics.
DEFAULT_READ_LOG_CLI = "/opt/dsh/packages/session/session-persistence-jsonl/lib/read-log-cli.js"

# A read of a multi-megabyte log through a node process. Generous, because
# the alternative to a slow answer here is a hook that measures nothing.
READ_TIMEOUT_SECONDS = 30


class UnreadableLog(RuntimeError):
    """The log exists but could not be read into events."""


def read_log_cli() -> str:
    return os.environ.get("DSH_READ_LOG_CLI") or DEFAULT_READ_LOG_CLI


def read_log_lines(path: Path) -> list[str]:
    """Every event line in one session log.

    A `.zstd` artifact goes through the harness's reader; a plain one is read
    directly, so a mind whose persistence is uncompressed needs no node.
    """
    path = Path(path)
    if path.suffix != ".zstd":
        try:
            return path.read_text(errors="replace").splitlines()
        except OSError as exc:
            raise UnreadableLog(str(exc)) from exc
    try:
        done = subprocess.run(
            ["node", read_log_cli(), str(path)],
            capture_output=True, text=True, timeout=READ_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnreadableLog(str(exc)) from exc
    if done.returncode != 0:
        raise UnreadableLog((done.stderr or "").strip() or f"read-log exited {done.returncode}")
    return done.stdout.splitlines()


def _events(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def session_header(lines: list[str]) -> dict:
    """The log's opening header, which carries the session's own identity."""
    for event in _events(lines):
        if event.get("type") == "session":
            return event
    return {}


def is_delegate_log(path: Path) -> bool:
    """Whether this log belongs to a delegate rather than to the mind.

    A delegate gets its own session, and every hook fires for its turns as if
    they were the mind's own — with the mind's identity in the environment. So
    the mind's memory fills with work it did not do unless the reader asks.
    The harness marks the child's header, the same three fields Claude Code
    uses, so this is one question with one answer rather than a guess from
    the turn's content.
    """
    try:
        header = session_header(read_log_lines(path))
    except UnreadableLog:
        # Unreadable is not "hers". Harvesting a log nobody can identify is
        # how a delegate's work is attributed to her by default.
        return True
    return header.get("origin") == "subagent" or bool(header.get("parentSession"))


# dsh reports uncached input, cache reads and cache writes as three
# disjoint numbers. Mapped onto the names the hooks read, so the existing sum
# is the real context rather than a tenth of it.
_USAGE_MAP = {
    "inputTokens": "input_tokens",
    "cacheReadTokens": "cache_read_input_tokens",
    "cacheWriteTokens": "cache_creation_input_tokens",
    "outputTokens": "output_tokens",
}

# Content blocks, named as the hooks' reader names them. `reasoning` becomes
# `thinking` so the voice and digest readers skip it for the reason they
# already skip it, rather than by failing to recognise it.
_BLOCK_MAP = {
    "text": "text",
    "reasoning": "thinking",
    "tool-call": "tool_use",
    "tool-result": "tool_result",
    "image": "image",
}


def _blocks(content: object) -> list[dict]:
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = _BLOCK_MAP.get(block.get("type") or "")
        if kind is None:
            continue
        if kind == "text":
            out.append({"type": "text", "text": block.get("text") or ""})
        elif kind == "thinking":
            out.append({"type": "thinking", "thinking": block.get("text") or ""})
        elif kind == "image":
            # Kept as a marker, not as bytes: what a reader needs is that the
            # conversation holds a picture, because a text-only model it is
            # switched to cannot read it.
            out.append({"type": "image"})
        elif kind == "tool_use":
            out.append({
                "type": "tool_use",
                "id": block.get("id") or "",
                "name": block.get("name") or "",
                "input": block.get("arguments"),
            })
        else:
            out.append({
                "type": "tool_result",
                "tool_use_id": block.get("toolCallId") or "",
                "content": block.get("content"),
            })
    return out


def translate(lines: list[str]) -> list[dict]:
    """The log's events as the entries the hooks read.

    Only the event kinds a hook asks about are translated. The rest — step
    boundaries, inbox splices, chunk deltas — carry nothing a hook reads, and
    a chunk in particular would double every sentence, since the harness
    writes both the deltas and the assembled message.
    """
    header = session_header(lines)
    sidechain = header.get("origin") == "subagent" or bool(header.get("parentSession"))
    entries: list[dict] = []
    for event in _events(lines):
        kind = event.get("type")
        data = event.get("data") or {}
        common = {"isSidechain": sidechain, "sessionId": header.get("id") or ""}
        # When the event was written, in epoch seconds: dsh stamps each one in
        # milliseconds, and a reader asking when the conversation began needs it.
        if isinstance(event.get("time"), (int, float)):
            common["timestamp"] = event["time"] / 1000
        if kind == "user/message":
            content = data.get("content")
            if content is None:
                content = (data.get("message") or {}).get("content")
            entries.append({**common, "type": "user",
                            "message": {"role": "user", "content": _blocks(content)}})
        elif kind == "assistant/message":
            message = data.get("message") or {}
            usage = data.get("usage") or {}
            entries.append({**common, "type": "assistant", "message": {
                "role": "assistant",
                "content": _blocks(message.get("content")),
                "model": ((message.get("source") or {}).get("model")) or "",
                "usage": {dest: int(usage.get(src) or 0)
                          for src, dest in _USAGE_MAP.items() if usage.get(src) is not None},
            }})
        elif kind == "tool/result":
            # A tool's output is most of what fills a context, and it arrives
            # as its own event rather than inside a user message. Placed where
            # Claude Code puts it — a user entry holding `tool_result` blocks —
            # so a reader estimating what a turn added counts it, and the
            # text-only readers pass over it as they already do.
            message = data.get("message") or {}
            entries.append({**common, "type": "user",
                            "message": {"role": "user",
                                        "content": _blocks(message.get("content"))}})
        elif kind == "turn/start":
            # Where this turn begins. A count from before it is an older
            # turn's, and an older count is not a measurement of this one.
            entries.append({**common, "type": "turn_start"})
        elif kind == "context/measured":
            # The harness's own figure for the conversation as of this turn:
            # the provider's count when there was one, its own estimate when
            # there was not, and "none" when it had neither.
            entries.append({**common, "type": "context", "context": {
                "model": data.get("model") or "",
                "tokens": data.get("tokens"),
                "kind": data.get("kind") or "",
                "window": data.get("window"),
            }})
        elif kind == "compaction/summary":
            # The floor on the live context. The hooks already stop at this
            # marker; without it they read the pre-compaction figure, which
            # is a conversation that no longer exists.
            entries.append({**common, "type": "assistant", "isCompactSummary": True,
                            "message": {"role": "assistant",
                                        "content": _blocks(data.get("summary"))}})
    return entries


def materialize(path: Path) -> Path:
    """Her log as a file in the hooks' own format, and its path.

    The hooks take a path and seek in it. Handing them a translated file
    keeps that true without giving each one a second reader.
    """
    entries = translate(read_log_lines(path))
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", prefix="dsh-transcript-", delete=False, encoding="utf-8")
    with handle as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return Path(handle.name)


def resolve(event: dict) -> Path | None:
    """The conversation named by a hook payload, in the hooks' own format.

    Every hook that reads the conversation starts here, so each one has a
    single reader and none of them carries a branch on the harness it is
    running under. ``None`` means there is nothing readable to work from —
    the first turn of a conversation whose log does not exist yet, or a log
    this process cannot decode.
    """
    raw = (event or {}).get("transcript_path") or ""
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    if path.suffix != ".zstd":
        return path
    try:
        return materialize(path)
    except UnreadableLog:
        return None


def main() -> None:
    """Print the resolved transcript path for a payload on stdin.

    The bash hooks need the same answer the Python ones get, and a second
    implementation of it in `jq` is a second thing to keep in step.
    """
    import sys
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        raise SystemExit(1)
    resolved = resolve(event if isinstance(event, dict) else {})
    if resolved is None:
        raise SystemExit(1)
    print(resolved)


if __name__ == "__main__":
    main()
