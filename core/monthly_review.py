"""Monthly review sweep -- surfaces world-event, intention, and session-log
entries for Daniel's review via Telegram.

Daniel responds with keep/archive/discard per entry. This is the
human-in-the-loop pass for data that cannot be auto-pruned (Pass 4
in specs/memory-lifecycle.md).

Called by the scheduler via the /memory/monthly-review gateway endpoint.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from core.archive_store import ArchiveStore, ArchivedEntry

logger = logging.getLogger(__name__)

# 30 days in seconds
REVIEW_INTERVAL_SECONDS = 30 * 86400

# Data classes eligible for monthly review
REVIEW_DATA_CLASSES = ["world-event", "intention", "session-log"]

# Max content length in review messages
MAX_CONTENT_LENGTH = 200


@dataclass
class ReviewEntry:
    """A single entry due for review."""

    element_id: str
    content: str
    data_class: str
    created_at: int
    last_reviewed_at: int | None


def _get_driver():
    """Lazy import to avoid circular dependency and allow mocking."""
    from agents.memory import _get_driver as _mem_get_driver
    return _mem_get_driver()


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Lazy import of the Telegram direct send function."""
    from agents.notify import _telegram_direct as _notify_telegram
    return _notify_telegram(message)


def _get_archive_store() -> ArchiveStore:
    """Return the default ArchiveStore instance. Separate function for mocking."""
    return ArchiveStore()


def query_entries_for_review() -> dict[str, list[ReviewEntry]]:
    """Query Neo4j for entries due for monthly review.

    Returns entries grouped by data_class where:
    - data_class is world-event, intention, or session-log
    - entry is not archived
    - last_reviewed_at is null or older than 30 days

    Returns:
        Dict keyed by data_class, values are lists of ReviewEntry.
    """
    cutoff = int(time.time()) - REVIEW_INTERVAL_SECONDS
    grouped: dict[str, list[ReviewEntry]] = {}

    try:
        driver = _get_driver()
        with driver.session() as session:
            result = session.run(
                """
                MATCH (m:Memory)
                WHERE m.data_class IN $data_classes
                  AND (m.archived IS NULL OR m.archived = false)
                  AND (m.last_reviewed_at IS NULL OR m.last_reviewed_at < $cutoff)
                RETURN m.content AS content,
                       m.data_class AS data_class,
                       m.created_at AS created_at,
                       m.last_reviewed_at AS last_reviewed_at,
                       elementId(m) AS id
                """,
                data_classes=REVIEW_DATA_CLASSES,
                cutoff=cutoff,
            )

            for record in result:
                entry = ReviewEntry(
                    element_id=record["id"],
                    content=record["content"],
                    data_class=record["data_class"],
                    created_at=record["created_at"],
                    last_reviewed_at=record["last_reviewed_at"],
                )
                grouped.setdefault(entry.data_class, []).append(entry)

    except Exception:
        logger.exception("Monthly review query failed")

    return grouped


def _short_id(element_id: str) -> str:
    """Return the full element ID for use in Telegram commands.

    Previously truncated to 12 chars, but Neo4j element IDs (e.g. '4:abc:123')
    must be passed in full for lookups via elementId(). Truncation caused
    keep/archive/discard to always fail with 'Entry not found'.
    """
    return element_id


def _format_date(timestamp: int) -> str:
    """Format a Unix timestamp as a human-readable date."""
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return "unknown"


def build_review_messages(grouped: dict[str, list[ReviewEntry]]) -> dict[str, str]:
    """Build one Telegram message per data class group.

    Args:
        grouped: Dict of data_class -> list of ReviewEntry.

    Returns:
        Dict of data_class -> message string.
    """
    messages: dict[str, str] = {}

    for data_class, entries in grouped.items():
        if not entries:
            continue

        lines = [f"Monthly Review: {data_class}\n"]

        for entry in entries:
            short = _short_id(entry.element_id)
            content_preview = entry.content[:MAX_CONTENT_LENGTH]
            if len(entry.content) > MAX_CONTENT_LENGTH:
                content_preview += "..."
            date_str = _format_date(entry.created_at)

            lines.append(f"{content_preview}")
            lines.append(f"Stored: {date_str}")

            if data_class == "world-event":
                lines.append(
                    f"/keep_{short}  /archive_{short}  /discard_{short}"
                )
            else:
                lines.append(f"/keep_{short}  /discard_{short}")

            lines.append("")  # blank line separator

        messages[data_class] = "\n".join(lines).strip()

    return messages


def handle_keep(element_id: str) -> dict:
    """Mark an entry as reviewed (keep it active).

    Sets last_reviewed_at to current timestamp.

    Returns:
        Result dict with ok, action keys.
    """
    try:
        driver = _get_driver()
        now_ts = int(time.time())
        with driver.session() as session:
            result = session.run(
                """
                MATCH (m) WHERE elementId(m) = $id
                SET m.last_reviewed_at = $now
                RETURN count(m) AS count
                """,
                id=element_id,
                now=now_ts,
            )
            record = result.single()
            if record and record["count"] > 0:
                logger.info("Kept entry %s (last_reviewed_at=%d)", element_id, now_ts)
                return {"ok": True, "action": "keep"}
            return {"ok": False, "error": f"Entry not found: {element_id}"}
    except Exception as e:
        logger.exception("handle_keep failed for %s", element_id)
        return {"ok": False, "error": str(e)}


def handle_archive(element_id: str) -> dict:
    """Archive a world-event entry.

    Reads the full entry from Neo4j, saves it to the ArchiveStore,
    then marks it as archived in Neo4j.

    Returns:
        Result dict with ok, action keys.
    """
    try:
        driver = _get_driver()
        with driver.session() as session:
            # Read the full entry
            result = session.run(
                """
                MATCH (m) WHERE elementId(m) = $id
                RETURN m.content AS content,
                       m.data_class AS data_class,
                       m.tags AS tags,
                       m.source AS source,
                       m.agent_id AS agent_id,
                       m.created_at AS created_at,
                       properties(m) AS props
                """,
                id=element_id,
            )
            record = result.single()
            if not record:
                return {"ok": False, "error": f"Entry not found: {element_id}"}

            data_class = record["data_class"]
            if data_class != "world-event":
                return {
                    "ok": False,
                    "error": f"Archive only allowed for world-event entries, got: {data_class}",
                }

            # Save to archive store
            archived_entry = ArchivedEntry(
                original_id=element_id,
                content=record["content"],
                data_class=data_class,
                tags=record["tags"] or "",
                source=record["source"] or "",
                agent_id=record["agent_id"] or "ada",
                created_at=record["created_at"] or 0,
                archived_at=datetime.now(timezone.utc).isoformat(),
                original_metadata=dict(record["props"]) if record["props"] else {},
            )

            store = _get_archive_store()
            store.save(archived_entry)

            # Mark as archived in Neo4j
            now_ts = int(time.time())
            session.run(
                """
                MATCH (m) WHERE elementId(m) = $id
                SET m.archived = true, m.last_reviewed_at = $now
                RETURN count(m) AS count
                """,
                id=element_id,
                now=now_ts,
            )

            logger.info("Archived entry %s to store", element_id)
            return {"ok": True, "action": "archive"}

    except Exception as e:
        logger.exception("handle_archive failed for %s", element_id)
        return {"ok": False, "error": str(e)}


def handle_discard(element_id: str) -> dict:
    """Delete an entry from Neo4j.

    Idempotent: does not raise if the entry does not exist.

    Returns:
        Result dict with ok, action keys.
    """
    try:
        driver = _get_driver()
        with driver.session() as session:
            session.run(
                "MATCH (m) WHERE elementId(m) = $id DETACH DELETE m",
                id=element_id,
            )
        logger.info("Discarded entry %s", element_id)
        return {"ok": True, "action": "discard"}
    except Exception as e:
        logger.exception("handle_discard failed for %s", element_id)
        return {"ok": False, "error": str(e)}


def sweep_monthly_review() -> dict:
    """Orchestrate the monthly review sweep.

    Queries entries due for review, builds messages, and sends them
    via Telegram grouped by data class.

    Returns:
        Summary dict with entries_found, messages_sent, errors.
    """
    entries_found = 0
    messages_sent = 0
    errors = 0

    try:
        grouped = query_entries_for_review()
        entries_found = sum(len(v) for v in grouped.values())

        if entries_found == 0:
            logger.info("Monthly review sweep: no entries due for review")
            return {"entries_found": 0, "messages_sent": 0, "errors": 0}

        messages = build_review_messages(grouped)

        for data_class, message in messages.items():
            try:
                _telegram_direct(message)
                messages_sent += 1
                logger.info(
                    "Sent monthly review message for %s (%d entries)",
                    data_class,
                    len(grouped.get(data_class, [])),
                )
            except Exception:
                logger.exception(
                    "Failed to send monthly review message for %s",
                    data_class,
                )
                errors += 1

    except Exception:
        logger.exception("Monthly review sweep failed")
        errors += 1

    logger.info(
        "Monthly review sweep complete: entries_found=%d, messages_sent=%d, errors=%d",
        entries_found,
        messages_sent,
        errors,
    )
    return {
        "entries_found": entries_found,
        "messages_sent": messages_sent,
        "errors": errors,
    }
