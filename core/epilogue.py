"""Session epilogue processor -- extracts memories and knowledge graph entries from completed sessions.

Sub-threshold sessions (short, few entities) auto-write without HITL approval.
Above-threshold sessions generate a digest sent to Daniel via Telegram for HITL approval.
Transcripts are deleted after processing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import EpilogueThresholds

logger = logging.getLogger(__name__)


@dataclass
class SessionMetrics:
    turn_count: int
    duration_minutes: float
    novel_entity_count: int


@dataclass
class EpilogueDigest:
    session_id: str
    summary: str
    memories: list[dict]
    entities: list[dict]
    metrics: SessionMetrics


def exceeds_threshold(metrics: SessionMetrics, thresholds: EpilogueThresholds) -> bool:
    """Check whether session metrics exceed any epilogue threshold.

    Returns True if any metric strictly exceeds the corresponding threshold.
    """
    return (
        metrics.turn_count > thresholds.max_turns
        or metrics.duration_minutes > thresholds.max_duration_minutes
        or metrics.novel_entity_count > thresholds.max_novel_entities
    )


def _extract_text_from_content(content: str | list) -> str:
    """Extract plain text from message content (string or content-block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp string to a datetime."""
    try:
        # Handle trailing Z
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def parse_transcript(path: Path) -> tuple[int, float, str]:
    """Parse a Claude CLI JSONL transcript file.

    Args:
        path: Path to the JSONL transcript file.

    Returns:
        Tuple of (turn_count, duration_minutes, conversation_text).
        turn_count: number of user messages.
        duration_minutes: time between first and last message timestamps.
        conversation_text: concatenated user and assistant text.

    Raises:
        FileNotFoundError: if the path does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Transcript not found: {path}")

    turn_count = 0
    timestamps: list[datetime] = []
    conversation_parts: list[str] = []

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            if entry_type not in ("user", "assistant"):
                continue

            # Parse timestamp
            ts = entry.get("timestamp")
            if ts:
                dt = _parse_timestamp(ts)
                if dt:
                    timestamps.append(dt)

            # Extract message content
            message = entry.get("message", {})
            content = message.get("content", "")
            text = _extract_text_from_content(content)

            if entry_type == "user":
                turn_count += 1
                conversation_parts.append(f"User: {text}")
            elif entry_type == "assistant" and text:
                conversation_parts.append(f"Assistant: {text}")

    duration_minutes = 0.0
    if len(timestamps) >= 2:
        timestamps.sort()
        delta = timestamps[-1] - timestamps[0]
        duration_minutes = delta.total_seconds() / 60.0

    conversation_text = "\n".join(conversation_parts)
    return turn_count, duration_minutes, conversation_text


_TELEGRAM_MAX_LEN = 4000


def format_digest_for_telegram(digest: EpilogueDigest) -> str:
    """Format an epilogue digest for display in a Telegram HITL approval message.

    Truncates to fit within Telegram's message length limit.
    """
    lines = [
        f"Session Epilogue: {digest.session_id[:8]}",
        "",
        f"Summary: {digest.summary}",
        "",
        f"Metrics: {digest.metrics.turn_count} turns, "
        f"{digest.metrics.duration_minutes:.0f} min, "
        f"{digest.metrics.novel_entity_count} novel entities",
        "",
    ]

    lines.append(f"Memories ({len(digest.memories)}):")
    if digest.memories:
        for i, mem in enumerate(digest.memories[:5]):
            content = mem.get("content", "")[:100]
            data_class = mem.get("data_class", "unknown")
            lines.append(f"  {i + 1}. [{data_class}] {content}")
        if len(digest.memories) > 5:
            lines.append(f"  ... and {len(digest.memories) - 5} more")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Entities ({len(digest.entities)}):")
    if digest.entities:
        for i, ent in enumerate(digest.entities[:5]):
            name = ent.get("name", "?")
            etype = ent.get("entity_type", "?")
            lines.append(f"  {i + 1}. {etype}: {name}")
        if len(digest.entities) > 5:
            lines.append(f"  ... and {len(digest.entities) - 5} more")
    else:
        lines.append("  (none)")

    result = "\n".join(lines)
    if len(result) > _TELEGRAM_MAX_LEN:
        result = result[:_TELEGRAM_MAX_LEN - 3] + "..."
    return result


# ---------------------------------------------------------------------------
# Write helpers — lazy imports to avoid import-time side effects
# ---------------------------------------------------------------------------

def _memory_store_direct(**kwargs: Any) -> str:
    """Lazy wrapper around memory_store_direct to avoid import-time Neo4j connections."""
    from tools.stateful.memory import memory_store_direct
    return memory_store_direct(**kwargs)


def _graph_upsert_direct(**kwargs: Any) -> str:
    """Lazy wrapper around graph_upsert_direct to avoid import-time Neo4j connections."""
    from tools.stateful.knowledge_graph import graph_upsert_direct
    return graph_upsert_direct(**kwargs)


def _hitl_request(summary: str) -> bool:
    """Send an HITL approval request to the gateway. Returns True if approved."""
    import os
    import requests

    gateway_url = os.environ.get("GATEWAY_URL", "http://localhost:8420")
    hitl_ttl = 180
    try:
        resp = requests.post(
            f"{gateway_url}/hitl/request",
            json={"action": "epilogue_write", "summary": summary, "ttl": hitl_ttl},
            timeout=hitl_ttl + 5,
        )
        resp.raise_for_status()
        return resp.json().get("approved", False)
    except Exception:
        logger.exception("HITL request failed for epilogue -- denying write by default")
        return False


def auto_write_digest(digest: EpilogueDigest) -> dict:
    """Write all memories and entities from a digest without HITL approval.

    Returns:
        Dict with memories_written, entities_written, and errors counts.
    """
    memories_written = 0
    entities_written = 0
    errors = 0

    for mem in digest.memories:
        try:
            result_str = _memory_store_direct(
                content=mem.get("content", ""),
                data_class=mem.get("data_class", "observation"),
                tags=mem.get("tags", ""),
                source=mem.get("source", "self"),
                agent_id=mem.get("agent_id", "ada"),
            )
            result = json.loads(result_str)
            if "error" in result:
                errors += 1
            else:
                memories_written += 1
        except Exception:
            logger.exception("Failed to write memory: %s", mem.get("content", "")[:80])
            errors += 1

    for ent in digest.entities:
        try:
            result_str = _graph_upsert_direct(
                entity_type=ent.get("entity_type", "Concept"),
                name=ent.get("name", ""),
                data_class=ent.get("data_class", ""),
                properties=ent.get("properties", "{}"),
                relation=ent.get("relation", ""),
                target_name=ent.get("target_name", ""),
                target_type=ent.get("target_type", ""),
                agent_id=ent.get("agent_id", "ada"),
                source=ent.get("source", "self"),
            )
            result = json.loads(result_str)
            if "error" in result:
                errors += 1
            else:
                entities_written += 1
        except Exception:
            logger.exception("Failed to write entity: %s", ent.get("name", "")[:80])
            errors += 1

    return {"memories_written": memories_written, "entities_written": entities_written, "errors": errors}


def hitl_write_digest(digest: EpilogueDigest) -> dict:
    """Send digest for HITL approval, then write if approved.

    Returns:
        Dict with memories_written, entities_written, errors, and optionally skipped.
    """
    summary = format_digest_for_telegram(digest)
    approved = _hitl_request(summary)

    if not approved:
        return {"memories_written": 0, "entities_written": 0, "errors": 0, "skipped": True}

    return auto_write_digest(digest)


# ---------------------------------------------------------------------------
# Session processing
# ---------------------------------------------------------------------------

async def process_session(
    session: dict,
    session_mgr: Any,
    thresholds: EpilogueThresholds,
) -> dict:
    """Process a single session's transcript for epilogue extraction.

    1. Get transcript path; skip if missing
    2. Parse transcript for metrics and conversation text
    3. Build a digest with summary and empty memories/entities (extraction deferred)
    4. Check thresholds: auto-write or HITL
    5. Delete transcript file
    6. Set epilogue_status to 'done' (or 'skipped' on error)

    Returns:
        Dict with processing results.
    """
    session_id = session["id"]

    try:
        transcript_path = await session_mgr.get_transcript_path(session_id)
        if transcript_path is None:
            logger.info("No transcript for session %s -- skipping epilogue", session_id)
            await session_mgr.set_epilogue_status(session_id, "skipped")
            return {"session_id": session_id, "status": "skipped", "reason": "no_transcript"}

        # Parse transcript
        turn_count, duration_minutes, conversation_text = parse_transcript(transcript_path)

        # Build metrics (novel_entity_count=0 for now; real extraction would populate this)
        metrics = SessionMetrics(
            turn_count=turn_count,
            duration_minutes=duration_minutes,
            novel_entity_count=0,
        )

        # Build digest with session summary as the digest summary
        digest = EpilogueDigest(
            session_id=session_id,
            summary=session.get("summary", ""),
            memories=[],
            entities=[],
            metrics=metrics,
        )

        # Route based on threshold
        if exceeds_threshold(metrics, thresholds):
            logger.info(
                "Session %s exceeds threshold (turns=%d, duration=%.0f) -- using HITL",
                session_id, turn_count, duration_minutes,
            )
            result = hitl_write_digest(digest)
            write_mode = "hitl"
        else:
            logger.info(
                "Session %s below threshold (turns=%d, duration=%.0f) -- auto-writing",
                session_id, turn_count, duration_minutes,
            )
            result = auto_write_digest(digest)
            write_mode = "auto"

        # Delete transcript after processing
        transcript_path.unlink(missing_ok=True)

        await session_mgr.set_epilogue_status(session_id, "done")
        return {
            "session_id": session_id,
            "status": "done",
            "write_mode": write_mode,
            **result,
        }

    except Exception:
        logger.exception("Epilogue processing failed for session %s", session_id)
        await session_mgr.set_epilogue_status(session_id, "skipped")
        return {"session_id": session_id, "status": "skipped", "reason": "error"}


async def process_pending_sessions(
    session_mgr: Any,
    thresholds: EpilogueThresholds,
) -> dict:
    """Process all sessions pending epilogue.

    Queries for sessions with status IN ('idle', 'closed') and epilogue_status IS NULL,
    then processes each one.

    Returns:
        Summary dict with processed, auto_written, hitl_sent, skipped, and errors counts.
    """
    pending = await session_mgr.get_sessions_pending_epilogue()

    processed = 0
    auto_written = 0
    hitl_sent = 0
    skipped = 0
    errors = 0

    for session in pending:
        processed += 1
        try:
            result = await process_session(session, session_mgr, thresholds)
            status = result.get("status")
            write_mode = result.get("write_mode")

            if status == "done" and write_mode == "auto":
                auto_written += 1
            elif status == "done" and write_mode == "hitl":
                hitl_sent += 1
            elif status == "skipped":
                skipped += 1
        except Exception:
            logger.exception("Unhandled error processing session %s", session.get("id"))
            errors += 1

    logger.info(
        "Epilogue sweep: processed=%d, auto_written=%d, hitl_sent=%d, skipped=%d, errors=%d",
        processed, auto_written, hitl_sent, skipped, errors,
    )
    return {
        "processed": processed,
        "auto_written": auto_written,
        "hitl_sent": hitl_sent,
        "skipped": skipped,
        "errors": errors,
    }
