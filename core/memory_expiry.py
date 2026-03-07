"""Memory expiry sweep -- deletes expired timed-event entries from Neo4j.

Non-recurring expired events are deleted unconditionally.
Recurring expired events trigger a Telegram prompt to Daniel.

Called by the scheduler via the /memory/expiry-sweep gateway endpoint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _get_driver():
    """Lazy import to avoid circular dependency and allow mocking."""
    from agents.memory import _get_driver as _mem_get_driver
    return _mem_get_driver()


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Lazy import of the Telegram direct send function."""
    from agents.notify import _telegram_direct as _notify_telegram
    return _notify_telegram(message)


def sweep_expired_events() -> dict:
    """Query Neo4j for expired timed-event entries and process them.

    - Non-recurring entries: deleted unconditionally.
    - Recurring entries: Telegram prompt sent to Daniel; entry NOT deleted.

    Returns:
        Summary dict with keys: deleted, prompted, errors.
    """
    deleted = 0
    prompted = 0
    errors = 0
    now = datetime.now(timezone.utc).isoformat()

    try:
        driver = _get_driver()
        with driver.session() as session:
            result = session.run(
                """
                MATCH (m:Memory)
                WHERE m.data_class = 'timed-event'
                  AND m.expires_at IS NOT NULL
                  AND m.expires_at < $now
                RETURN m.content AS content,
                       m.expires_at AS expires_at,
                       m.recurring AS recurring,
                       elementId(m) AS id
                """,
                now=now,
            )

            for record in result:
                content = record["content"]
                expires_at = record["expires_at"]
                is_recurring = record["recurring"]
                element_id = record["id"]

                if is_recurring:
                    # Send Telegram prompt for recurring events
                    try:
                        msg = (
                            f"Recurring event expired:\n\n"
                            f"{content}\n\n"
                            f"Expired at: {expires_at}\n\n"
                            f"Should I keep this for the next occurrence or delete it?"
                        )
                        _telegram_direct(msg)
                        prompted += 1
                        logger.info(
                            "Prompted for recurring expired event: %s (expires_at=%s)",
                            content,
                            expires_at,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to send Telegram prompt for recurring event: %s",
                            content,
                        )
                        errors += 1
                else:
                    # Delete non-recurring expired events
                    try:
                        session.run(
                            "MATCH (m) WHERE elementId(m) = $id DETACH DELETE m",
                            id=element_id,
                        )
                        deleted += 1
                        logger.info(
                            "Deleted expired non-recurring event: %s (expires_at=%s)",
                            content,
                            expires_at,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to delete expired event: %s",
                            content,
                        )
                        errors += 1

    except Exception:
        logger.exception("Memory expiry sweep failed")
        errors += 1

    logger.info(
        "Memory expiry sweep complete: deleted=%d, prompted=%d, errors=%d",
        deleted,
        prompted,
        errors,
    )
    return {"deleted": deleted, "prompted": prompted, "errors": errors}
