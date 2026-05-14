"""Session epilogue processor -- extracts memories and knowledge graph entries from completed sessions.

All sessions auto-write by default. Exception triggers (anomalous conditions) send
informational HITL notifications after writes have completed. Transcripts are deleted
after processing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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


@dataclass
class EpilogueException:
    trigger: str  # "high_novel_entities" | "high_error_rate" | "conflicting_entity"
    detail: str   # Human-readable explanation


def check_exceptions(
    digest: EpilogueDigest,
    write_errors: int = 0,
    total_writes: int = 0,
) -> list[EpilogueException]:
    """Check whether a digest triggers any exception conditions.

    Returns a list of EpilogueException instances (empty if no exceptions).
    """
    exceptions: list[EpilogueException] = []

    if digest.metrics.novel_entity_count > 10:
        exceptions.append(EpilogueException(
            trigger="high_novel_entities",
            detail=f"{digest.metrics.novel_entity_count} novel entities found",
        ))

    if total_writes > 0 and write_errors / total_writes > 0.5:
        exceptions.append(EpilogueException(
            trigger="high_error_rate",
            detail=f"{write_errors}/{total_writes} writes failed",
        ))

    return exceptions


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


# ---------------------------------------------------------------------------
# Write helpers — HTTP calls to the shared hive_nervous_system container
# ---------------------------------------------------------------------------

def _memory_store_direct(**kwargs: Any) -> dict:
    from core.lucent_client import memory_store
    return memory_store(**kwargs)


def _graph_upsert_direct(**kwargs: Any) -> dict:
    from core.lucent_client import graph_upsert_direct
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
        mind_id = mem.get("mind_id")
        if not mind_id:
            logger.error("Skipping memory with missing mind_id: %s", mem.get("content", "")[:80])
            errors += 1
            continue
        try:
            result = _memory_store_direct(
                content=mem.get("content", ""),
                data_class=mem.get("data_class", "observation"),
                tags=mem.get("tags", ""),
                source=mem.get("source", "self"),
                mind_id=mind_id,
            )
            if "error" in result or result.get("stored") is False:
                errors += 1
            else:
                memories_written += 1
        except Exception:
            logger.exception("Failed to write memory: %s", mem.get("content", "")[:80])
            errors += 1

    for ent in digest.entities:
        mind_id = ent.get("mind_id")
        if not mind_id:
            logger.error("Skipping entity with missing mind_id: %s", ent.get("name", "")[:80])
            errors += 1
            continue
        try:
            result = _graph_upsert_direct(
                entity_type=ent.get("entity_type", "Concept"),
                name=ent.get("name", ""),
                data_class=ent.get("data_class", ""),
                properties=ent.get("properties", "{}"),
                relation=ent.get("relation", ""),
                target_name=ent.get("target_name", ""),
                target_type=ent.get("target_type", ""),
                mind_id=mind_id,
                source=ent.get("source", "self"),
            )
            if "error" in result:
                errors += 1
            else:
                entities_written += 1
        except Exception:
            logger.exception("Failed to write entity: %s", ent.get("name", "")[:80])
            errors += 1

    return {"memories_written": memories_written, "entities_written": entities_written, "errors": errors}


def format_exception_notification(
    session_id: str,
    exceptions: list[EpilogueException],
) -> str:
    """Format exception details into an informational HITL notification message."""
    lines = [
        f"Epilogue Exception: {session_id[:8]}",
        "",
        f"{len(exceptions)} exception(s) detected after auto-write:",
        "",
    ]
    for exc in exceptions:
        lines.append(f"  - {exc.trigger}: {exc.detail}")

    result = "\n".join(lines)
    if len(result) > _TELEGRAM_MAX_LEN:
        result = result[:_TELEGRAM_MAX_LEN - 3] + "..."
    return result


def _notify_exception(
    session_id: str,
    exceptions: list[EpilogueException],
) -> None:
    """Send exception details via HITL notification (fire-and-forget).

    Catches all exceptions so that notification failures never propagate.
    """
    try:
        message = format_exception_notification(session_id, exceptions)
        _hitl_request(message)
    except Exception:
        logger.exception(
            "Failed to send exception notification for session %s", session_id
        )


# ---------------------------------------------------------------------------
# Session processing
# ---------------------------------------------------------------------------

async def process_session(
    session: dict,
    session_mgr: Any,
) -> dict:
    """Process a single session's transcript for epilogue extraction.

    1. Get transcript path; skip if missing
    2. Parse transcript for metrics and conversation text
    3. Build a digest with summary and empty memories/entities (extraction deferred)
    4. Always auto-write
    5. Check for exception conditions; notify if any
    6. Delete transcript file
    7. Set epilogue_status to 'done' (or 'skipped' on error)

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

        # Always auto-write
        logger.info(
            "Session %s auto-writing (turns=%d, duration=%.0f)",
            session_id, turn_count, duration_minutes,
        )
        result = auto_write_digest(digest)
        write_mode = "auto"

        # Check for exception conditions after auto-write
        total_writes = result["memories_written"] + result["entities_written"] + result["errors"]
        exceptions = check_exceptions(digest, write_errors=result["errors"], total_writes=total_writes)

        if exceptions:
            trigger_names = ", ".join(e.trigger for e in exceptions)
            logger.warning(
                "Epilogue exceptions for session %s: %s",
                session_id, trigger_names,
            )
            _notify_exception(session_id, exceptions)

        # Delete transcript after processing
        transcript_path.unlink(missing_ok=True)

        await session_mgr.set_epilogue_status(session_id, "done")
        output: dict[str, Any] = {
            "session_id": session_id,
            "status": "done",
            "write_mode": write_mode,
            **result,
        }
        if exceptions:
            output["exceptions"] = [{"trigger": e.trigger, "detail": e.detail} for e in exceptions]
        return output

    except Exception:
        logger.exception("Epilogue processing failed for session %s", session_id)
        await session_mgr.set_epilogue_status(session_id, "skipped")
        return {"session_id": session_id, "status": "skipped", "reason": "error"}


async def process_pending_sessions(
    session_mgr: Any,
) -> dict:
    """Process all sessions pending epilogue.

    Queries for sessions with status IN ('idle', 'closed') and epilogue_status IS NULL,
    then processes each one.

    Returns:
        Summary dict with processed, auto_written, skipped, errors, and exceptions counts.
    """
    pending = await session_mgr.get_sessions_pending_epilogue()

    processed = 0
    auto_written = 0
    skipped = 0
    errors = 0
    exception_count = 0

    for session in pending:
        processed += 1
        try:
            result = await process_session(session, session_mgr)
            status = result.get("status")

            if status == "done":
                auto_written += 1
                if result.get("exceptions"):
                    exception_count += 1
            elif status == "skipped":
                skipped += 1
        except Exception:
            logger.exception("Unhandled error processing session %s", session.get("id"))
            errors += 1

    logger.info(
        "Epilogue sweep: processed=%d, auto_written=%d, skipped=%d, errors=%d, exceptions=%d",
        processed, auto_written, skipped, errors, exception_count,
    )
    return {
        "processed": processed,
        "auto_written": auto_written,
        "skipped": skipped,
        "errors": errors,
        "exceptions": exception_count,
    }
