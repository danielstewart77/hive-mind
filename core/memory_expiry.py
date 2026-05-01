"""Memory expiry sweep -- deletes expired timed-event entries from Lucent (SQLite).

Non-recurring expired events are deleted unconditionally.
Recurring expired events trigger a Telegram prompt to Daniel.

Called by the scheduler via the /memory/expiry-sweep gateway endpoint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _get_connection():
    """Lazy import to get the Lucent SQLite connection."""
    from nervous_system.lucent_api.lucent import _get_connection as _lucent_get_connection
    return _lucent_get_connection()


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Delegate to shared Telegram utility in core/."""
    from core.notify_utils import telegram_direct
    return telegram_direct(message)


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
        conn = _get_connection()
        rows = conn.execute(
            """
            SELECT id, content, expires_at, recurring
            FROM memories
            WHERE data_class = 'timed-event'
              AND expires_at IS NOT NULL
              AND expires_at < ?
            """,
            (now,),
        ).fetchall()

        for row in rows:
            content = row["content"]
            expires_at = row["expires_at"]
            is_recurring = row["recurring"]
            row_id = row["id"]

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
                    conn.execute(
                        "DELETE FROM memories WHERE id = ?",
                        (row_id,),
                    )
                    conn.commit()
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
