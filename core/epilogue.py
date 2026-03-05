"""
Hive Mind -- Session epilogue processor.

Reads Claude transcript JSONL files, triages sessions for signal,
generates topically-grouped digests via Claude, and writes to the
knowledge graph and vector memory after HITL approval.
"""

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite

log = logging.getLogger("hive-mind.epilogue")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSCRIPT_DIR = Path.home() / ".claude" / "projects" / "-usr-src-app"
ARCHIVE_DIR = Path("/usr/src/app/data/transcripts/archive")
MAX_TRANSCRIPT_CHARS = 50000
HITL_DIGEST_TTL = 600  # 10 minutes for user to review digest

# Regex patterns for low-signal utility queries
UTILITY_PATTERNS = [
    r"\bweather\b",
    r"\bwhat time\b",
    r"\bwhat's the time\b",
    r"\bcurrent time\b",
    r"\bquick lookup\b",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TranscriptTurn:
    role: str          # "user" or "assistant"
    content: str       # text content (extracted from JSONL message blocks)
    timestamp: str     # ISO timestamp from JSONL



# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def _extract_text_content(content: str | list) -> str:
    """Extract plain text from a message content field.

    Content can be a plain string or a list of content blocks
    (multimodal format with text, image, etc. blocks).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "".join(texts)
    return ""


def parse_transcript(lines: list[str]) -> list[TranscriptTurn]:
    """Parse JSONL transcript lines into a list of TranscriptTurn objects.

    Only extracts user and assistant message events; skips all other
    event types (queue-operation, result, system, etc.).
    """
    turns: list[TranscriptTurn] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type not in ("user", "assistant"):
            continue

        message = event.get("message", {})
        role = message.get("role", event_type)
        content = _extract_text_content(message.get("content", ""))
        timestamp = event.get("timestamp", "")

        if content:
            turns.append(TranscriptTurn(role=role, content=content, timestamp=timestamp))

    return turns


def read_transcript_file(path: Path) -> list[TranscriptTurn]:
    """Read a JSONL transcript file and return parsed turns.

    Returns an empty list if the file does not exist.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
        return parse_transcript(lines)
    except FileNotFoundError:
        return []


def count_user_turns(turns: list[TranscriptTurn]) -> int:
    """Count the number of user turns in a transcript."""
    return sum(1 for t in turns if t.role == "user")


def triage_session(turns: list[TranscriptTurn]) -> tuple[bool, str]:
    """Decide whether a session should be skipped or processed for epilogue.

    Returns (should_skip, reason). If should_skip is False, reason is empty.
    Skip conditions:
    - Fewer than 3 user turns
    - All user turns match utility patterns (weather, time, quick lookups)
    """
    user_turns = [t for t in turns if t.role == "user"]

    if len(user_turns) < 3:
        return True, "fewer than 3 user turns"

    # Check if all user turns are utility queries
    compiled = [re.compile(p, re.IGNORECASE) for p in UTILITY_PATTERNS]
    all_utility = all(
        any(pat.search(turn.content) for pat in compiled)
        for turn in user_turns
    )
    if all_utility:
        return True, "pure utility session"

    return False, ""


# ---------------------------------------------------------------------------
# Digest generation
# ---------------------------------------------------------------------------

DIGEST_SYSTEM_PROMPT = (
    "You are a session epilogue processor. You will receive a transcript of a "
    "conversation session. Your job is to:\n"
    "1. Extract key entities: people, places, projects, preferences, decisions\n"
    "2. Extract relationships between entities as directed edges\n"
    "3. Write one memory entry per topic — full sentences, self-contained, "
    "exactly as they will be stored in the vector database\n"
    "4. Output a JSON object with this structure:\n"
    '{\n'
    '  "topics": [\n'
    '    "Full self-contained memory entry for topic 1.",\n'
    '    "Full self-contained memory entry for topic 2."\n'
    '  ],\n'
    '  "entities": [\n'
    '    {"name": "...", "type": "person|place|project|preference", '
    '"context": "..."}\n'
    '  ],\n'
    '  "relationships": [\n'
    '    {"from": "NodeA", "edge": "EDGE_TYPE", "to": "NodeB"}\n'
    '  ]\n'
    '}\n\n'
    "Rules:\n"
    "- topics must be complete, standalone memory entries — not headlines or labels\n"
    "- Multi-topic sessions must produce separate topic entries\n"
    "- Include ALL people mentioned with their relationship context\n"
    "- Include architectural decisions with reasoning\n"
    "- Relationship edge types should be uppercase verbs: MANAGES, WORKS_ON, "
    "OWNS, USES, DECIDED, DISCUSSED, etc.\n"
    "- Be concise but complete\n"
    "- Output ONLY valid JSON, no markdown fences"
)


def build_digest_prompt(turns: list[TranscriptTurn]) -> str:
    """Build the prompt to send to a Claude session for digest generation.

    Formats turns into a readable transcript block. Truncates if the
    transcript exceeds MAX_TRANSCRIPT_CHARS.
    """
    transcript_lines = []
    for turn in turns:
        prefix = "User" if turn.role == "user" else "Assistant"
        transcript_lines.append(f"[{prefix}]: {turn.content}")

    transcript_text = "\n\n".join(transcript_lines)

    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        transcript_text = (
            transcript_text[:MAX_TRANSCRIPT_CHARS]
            + "\n\n[TRANSCRIPT TRUNCATED -- exceeded 50000 character limit]"
        )

    return (
        "Here is the session transcript to process:\n\n"
        "---BEGIN TRANSCRIPT---\n"
        f"{transcript_text}\n"
        "---END TRANSCRIPT---\n\n"
        "Produce the JSON digest now."
    )


async def generate_digest(
    gateway_client: Any,
    user_id: int,
    transcript_turns: list[TranscriptTurn],
) -> dict | None:
    """Generate a digest by sending the transcript to a Claude session via the gateway.

    Uses GatewayClient.query() to create a session and get a structured response.
    Returns a dict with keys: digest, topics, entities. Returns None on failure.
    """
    prompt = build_digest_prompt(transcript_turns)
    try:
        response = await gateway_client.query(user_id, "epilogue-digest", prompt)
        # Parse the JSON response
        # Strip any markdown code fences if present
        response = response.strip()
        if response.startswith("```"):
            # Remove code fences
            lines = response.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            response = "\n".join(lines)
        result = json.loads(response)
        return result
    except json.JSONDecodeError as e:
        log.warning("Digest JSON parse failed: %s", e)
        return None
    except Exception as e:
        log.error("Unexpected error generating digest: %s", e)
        return None


# ---------------------------------------------------------------------------
# HITL approval
# ---------------------------------------------------------------------------

ENTITY_TYPE_ICON = {
    "person": "👤",
    "place": "📍",
    "project": "🏗️",
    "preference": "⚙️",
}


def _format_digest_message(digest: dict, session_summary: str = "", ended_at: float = 0.0) -> str:
    """Format a digest dict into a human-readable markdown message for HITL."""
    import datetime
    lines = []

    if session_summary or ended_at:
        ts = datetime.datetime.fromtimestamp(ended_at).strftime("%-I:%M %p") if ended_at else ""
        summary_text = session_summary[:120] + "..." if len(session_summary) > 120 else session_summary
        header = f"📝 **Session epilogue**"
        if ts:
            header += f" — ended {ts}"
        lines.append(header)
        if summary_text:
            lines.append(f"_{summary_text}_")
        lines.append("")

    topics = digest.get("topics", [])
    if topics:
        lines.append("**Memory entries**")
        for topic in topics:
            lines.append(f"• {topic}")
        lines.append("")

    relationships = digest.get("relationships", [])
    entities = digest.get("entities", [])
    if relationships or entities:
        lines.append("**Knowledge graph**")
        for rel in relationships:
            frm = rel.get("from", "?")
            edge = rel.get("edge", "?")
            to = rel.get("to", "?")
            lines.append(f"`{frm}` --[{edge}]--> `{to}`")
        # Nodes with no relationships
        connected = {r.get("from") for r in relationships} | {r.get("to") for r in relationships}
        for ent in entities:
            name = ent.get("name", "?")
            if name not in connected:
                icon = ENTITY_TYPE_ICON.get(ent.get("type", ""), "🔹")
                lines.append(f"{icon} `{name}` (no edges)")

    return "\n".join(lines)


async def request_digest_approval(
    server_url: str,
    digest: dict,
    session_summary: str = "",
    ended_at: float = 0.0,
) -> bool:
    """Request HITL approval for a session digest.

    Sends the formatted digest to the HITL endpoint and waits for approval.
    Returns True if approved, False if denied or on timeout/error.
    """
    message = _format_digest_message(digest, session_summary=session_summary, ended_at=ended_at)
    payload = {
        "action": "session_epilogue",
        "summary": message[:4000],
        "ttl": HITL_DIGEST_TTL,
        "wait": True,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=HITL_DIGEST_TTL + 30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{server_url}/hitl/request", json=payload
            ) as resp:
                data = await resp.json()
                return data.get("approved", False)
    except Exception as e:
        log.error("HITL digest approval request failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Memory write-back
# ---------------------------------------------------------------------------

_ENTITY_TYPE_MAP = {
    "person": "Person",
    "project": "Project",
    "preference": "Preference",
    "system": "System",
    "concept": "Concept",
    "place": "Concept",
}

# Map epilogue entity types to data classes for classification
_ENTITY_DATA_CLASS_MAP = {
    "person": "person",
    "project": "technical-config",
    "preference": "preference",
    "system": "technical-config",
    "concept": "session-log",
    "place": "session-log",
}


async def write_to_memory(digest: dict) -> None:
    """Write digest data directly to knowledge graph and vector memory.

    Calls memory_store and graph_upsert directly — no intermediary Claude session.
    What you approve in HITL is exactly what gets written.
    """
    from agents.memory import memory_store_direct
    from agents.knowledge_graph import graph_upsert_direct

    # Write each topic as a separate vector memory entry
    for topic in digest.get("topics", []):
        try:
            await asyncio.to_thread(
                memory_store_direct,
                content=topic,
                tags="session,epilogue",
                source="session",
                data_class="session-log",
            )
            log.info("Stored memory: %.80s", topic)
        except Exception as e:
            log.error("Failed to write topic to memory: %s", e)

    # Build name→type lookup for relationship resolution
    entities = digest.get("entities", [])
    entity_type_map = {
        ent.get("name", ""): _ENTITY_TYPE_MAP.get(ent.get("type", "").lower(), "Concept")
        for ent in entities
    }

    # Write entities to knowledge graph
    for ent in entities:
        name = ent.get("name", "").strip()
        raw_type = ent.get("type", "").lower()
        entity_type = _ENTITY_TYPE_MAP.get(raw_type, "Concept")
        data_class = _ENTITY_DATA_CLASS_MAP.get(raw_type, "session-log")
        context = ent.get("context", "")
        if not name:
            continue
        try:
            props = json.dumps({"context": context}) if context else "{}"
            await asyncio.to_thread(
                graph_upsert_direct,
                entity_type=entity_type,
                name=name,
                properties=props,
                source="session",
                data_class=data_class,
            )
            log.info("Upserted graph node: %s (%s)", name, entity_type)
        except Exception as e:
            log.error("Failed to upsert graph node %s: %s", name, e)

    # Write relationships
    for rel in digest.get("relationships", []):
        from_name = rel.get("from", "").strip()
        edge = rel.get("edge", "").strip()
        to_name = rel.get("to", "").strip()
        if not (from_name and edge and to_name):
            continue
        # Infer data_class from the source entity
        from_raw_type = next(
            (e.get("type", "").lower() for e in entities if e.get("name") == from_name),
            "",
        )
        rel_data_class = _ENTITY_DATA_CLASS_MAP.get(from_raw_type, "session-log")
        try:
            await asyncio.to_thread(
                graph_upsert_direct,
                entity_type=entity_type_map.get(from_name, "Concept"),
                name=from_name,
                relation=edge,
                target_name=to_name,
                target_type=entity_type_map.get(to_name, "Concept"),
                source="session",
                data_class=rel_data_class,
            )
            log.info("Created relationship: %s -[%s]-> %s", from_name, edge, to_name)
        except Exception as e:
            log.error("Failed to write relationship %s -[%s]-> %s: %s", from_name, edge, to_name, e)


# ---------------------------------------------------------------------------
# Transcript archival
# ---------------------------------------------------------------------------

def archive_transcript(source_path: Path, session_id: str) -> None:
    """Archive a transcript file after processing.

    # TEMPORARY: Phase 1 only -- switch to delete in Phase 2
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / f"{session_id}.jsonl"
    shutil.move(str(source_path), str(dest))
    log.info("Archived transcript %s -> %s", source_path, dest)


# ---------------------------------------------------------------------------
# Main epilogue processor
# ---------------------------------------------------------------------------

async def process_session_epilogue(
    session_id: str,
    db: aiosqlite.Connection,
    server_url: str,
    gateway_client: Any,
    user_id: int,
    force: bool = False,
) -> str:
    """Main epilogue processing orchestrator.

    Steps:
    1. Check epilogue_status -- skip if completed/skipped, allow retry for pending/digest_sent
    2. Set status to "pending"
    3. Look up claude_sid, find transcript path
    4. Read and parse transcript
    5. Triage -- if should_skip, set status to "skipped", return
    6. Generate digest via Claude session
    7. Set status to "digest_sent"
    8. Request HITL approval
    9. On approval: set status to "approved", call write_to_memory, set status to "completed"
    10. On denial: set status to "skipped"
    11. Archive transcript
    12. Return final status

    Returns the final epilogue_status string.
    """
    # Step 1: Check current status (also fetch summary/last_active for HITL header)
    row = await db.execute(
        "SELECT epilogue_status, claude_sid, summary, last_active FROM sessions WHERE id = ?",
        (session_id,),
    )
    result = await row.fetchone()
    if not result:
        log.warning("Session %s not found in DB", session_id)
        return "error"

    current_status = result["epilogue_status"]
    claude_sid = result["claude_sid"]
    session_summary = result["summary"] or ""
    ended_at = result["last_active"] or 0.0

    # Skip completed and skipped sessions (idempotency)
    if current_status in ("completed", "skipped"):
        log.info("Session %s already %s, skipping", session_id, current_status)
        return current_status

    # Allow retry for pending and digest_sent (crashed or timed out)
    # Process normally for NULL

    # Step 2: Set status to "pending"
    await db.execute(
        "UPDATE sessions SET epilogue_status = 'pending' WHERE id = ?",
        (session_id,),
    )
    await db.commit()

    # Step 3: Find transcript path
    if not claude_sid:
        log.info("Session %s has no claude_sid, marking skipped", session_id)
        await db.execute(
            "UPDATE sessions SET epilogue_status = 'skipped' WHERE id = ?",
            (session_id,),
        )
        await db.commit()
        return "skipped"

    transcript_path = TRANSCRIPT_DIR / f"{claude_sid}.jsonl"
    if not transcript_path.exists():
        log.info("Transcript not found for session %s at %s, marking skipped", session_id, transcript_path)
        await db.execute(
            "UPDATE sessions SET epilogue_status = 'skipped' WHERE id = ?",
            (session_id,),
        )
        await db.commit()
        return "skipped"

    # Step 4: Read and parse transcript
    turns = read_transcript_file(transcript_path)

    # Step 5: Triage (bypass on manual /remember)
    should_skip, reason = triage_session(turns)
    if should_skip and not force:
        log.info("Session %s triaged as skip: %s", session_id, reason)
        await db.execute(
            "UPDATE sessions SET epilogue_status = 'skipped' WHERE id = ?",
            (session_id,),
        )
        await db.commit()
        return "skipped"

    # Step 6: Generate digest
    digest = await generate_digest(gateway_client, user_id, turns)
    if not digest:
        log.error("Failed to generate digest for session %s", session_id)
        await db.execute(
            "UPDATE sessions SET epilogue_status = 'skipped' WHERE id = ?",
            (session_id,),
        )
        await db.commit()
        return "skipped"

    # Step 7: Set status to "digest_sent"
    await db.execute(
        "UPDATE sessions SET epilogue_status = 'digest_sent' WHERE id = ?",
        (session_id,),
    )
    await db.commit()

    # Step 8: Request HITL approval
    approved = await request_digest_approval(
        server_url, digest, session_summary=session_summary, ended_at=ended_at
    )

    if approved:
        # Step 9a: Approved
        await db.execute(
            "UPDATE sessions SET epilogue_status = 'approved' WHERE id = ?",
            (session_id,),
        )
        await db.commit()

        await write_to_memory(digest)

        await db.execute(
            "UPDATE sessions SET epilogue_status = 'completed' WHERE id = ?",
            (session_id,),
        )
        await db.commit()
        final_status = "completed"
    else:
        # Step 10: Denied
        await db.execute(
            "UPDATE sessions SET epilogue_status = 'skipped' WHERE id = ?",
            (session_id,),
        )
        await db.commit()
        final_status = "skipped"

    # Step 11: Archive transcript
    # TEMPORARY: Phase 1 only -- switch to delete in Phase 2
    try:
        archive_transcript(transcript_path, session_id)
    except Exception as e:
        log.error("Failed to archive transcript for session %s: %s", session_id, e)

    log.info("Epilogue for session %s completed with status: %s", session_id, final_status)
    return final_status
