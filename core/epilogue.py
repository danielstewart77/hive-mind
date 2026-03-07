"""
Hive Mind — Session epilogue utilities.

Provides transcript reading and the system prompt for the automated
memory pipeline. Orchestration lives in SessionManager._run_memory_for_owner.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("hive-mind.epilogue")

TRANSCRIPT_DIR = Path.home() / ".claude" / "projects" / "-usr-src-app"
MAX_TRANSCRIPT_CHARS = 50000

MEMORY_MANAGER_PROMPT = (
    "You are processing a completed session transcript for memory storage.\n\n"
    "When you receive a transcript, invoke the memory-manager pipeline with "
    "trigger type 'automated'. Follow these steps exactly:\n"
    "1. Create a temp directory: /tmp/memory-{use current timestamp}/\n"
    "2. Run parse-memory agent — pass: trigger=automated, the full transcript, temp path\n"
    "3. On PASS: run classify-memory agent — pass: temp path\n"
    "4. On PASS: run route-memory agent — pass: temp path\n"
    "5. On PASS (not ALL_DISCARDED): run save-memory agent — pass: temp path\n"
    "6. Delete the temp directory\n\n"
    "Proceed autonomously. When classify-memory needs a new data class, it will "
    "prompt Daniel via Telegram — that is expected and correct."
)


@dataclass
class TranscriptTurn:
    role: str       # "user" or "assistant"
    content: str
    timestamp: str


def _extract_text_content(content: str | list) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def read_transcript(path: Path) -> list[TranscriptTurn]:
    """Read and parse a JSONL transcript file into TranscriptTurn objects.

    Returns an empty list if the file does not exist or has no message turns.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    turns = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") not in ("user", "assistant"):
            continue
        message = event.get("message", {})
        role = message.get("role", event.get("type"))
        content = _extract_text_content(message.get("content", ""))
        timestamp = event.get("timestamp", "")
        if content:
            turns.append(TranscriptTurn(role=role, content=content, timestamp=timestamp))
    return turns


def format_transcript(turns: list[TranscriptTurn]) -> str:
    """Format transcript turns into a readable string for the memory pipeline."""
    lines = []
    for turn in turns:
        prefix = "User" if turn.role == "user" else "Assistant"
        lines.append(f"[{prefix}]: {turn.content}")
    text = "\n\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS] + "\n\n[TRANSCRIPT TRUNCATED]"
    return text
