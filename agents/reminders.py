"""One-time reminder system.

Stores reminders in a SQLite table. The check-reminders skill fires any that
are due and deletes them automatically.

MCP tools:
  set_reminder(message, when) — schedule a one-time reminder
  list_reminders()            — list all pending reminders
  delete_reminder(id)         — cancel a reminder
"""

import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from agent_tooling import tool

DB_PATH = os.getenv("REMINDERS_DB", "/usr/src/app/data/reminders.db")
TZ = ZoneInfo("America/Chicago")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            fire_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )"""
    )
    conn.commit()
    return conn


@tool(tags=["utility"])
def set_reminder(message: str, when: str) -> str:
    """Schedule a one-time reminder to be delivered via Telegram.

    Args:
        message: What to remind about (plain text).
        when: Natural date/time string in US Central time, e.g.
              "2026-03-01 14:30", "tomorrow at 9am", "in 2 hours".
              ISO format (YYYY-MM-DD HH:MM) is most reliable.

    Returns:
        Confirmation with the reminder ID and scheduled time.
    """
    try:
        from dateparser import parse as dp_parse
        dt = dp_parse(when, settings={"TIMEZONE": "America/Chicago", "RETURN_AS_TIMEZONE_AWARE": True})
        if dt is None:
            return json.dumps({"error": f"Could not parse time: {when!r}"})
        fire_at = int(dt.timestamp())
        now = int(datetime.now(TZ).timestamp())
        if fire_at <= now:
            return json.dumps({"error": "Reminder time is in the past."})
        with _conn() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (message, fire_at, created_at) VALUES (?, ?, ?)",
                (message, fire_at, now),
            )
            rid = cur.lastrowid
        return json.dumps({
            "set": True,
            "id": rid,
            "message": message,
            "fire_at": dt.strftime("%Y-%m-%d %H:%M %Z"),
        })
    except ImportError:
        return json.dumps({"error": "dateparser not installed. Run: pip install dateparser"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool(tags=["utility"])
def list_reminders() -> str:
    """List all pending reminders.

    Returns:
        JSON array of pending reminders with id, message, and scheduled time.
    """
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, message, fire_at FROM reminders ORDER BY fire_at"
            ).fetchall()
        reminders = [
            {
                "id": r[0],
                "message": r[1],
                "fire_at": datetime.fromtimestamp(r[2], TZ).strftime("%Y-%m-%d %H:%M %Z"),
            }
            for r in rows
        ]
        return json.dumps({"reminders": reminders, "count": len(reminders)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool(tags=["utility"])
def delete_reminder(reminder_id: int) -> str:
    """Cancel a pending reminder by ID.

    Args:
        reminder_id: The reminder ID from list_reminders or set_reminder.

    Returns:
        Confirmation or error.
    """
    try:
        with _conn() as conn:
            conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return json.dumps({"deleted": True, "id": reminder_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool(tags=["utility"])
def get_due_reminders() -> str:
    """Return all reminders that are due now or overdue, and delete them.

    Called by the check-reminders skill every 15 minutes. Self-cleaning.

    Returns:
        JSON array of fired reminders (empty if none due).
    """
    try:
        now = int(datetime.now(TZ).timestamp())
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, message, fire_at FROM reminders WHERE fire_at <= ?", (now,)
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                conn.execute(f"DELETE FROM reminders WHERE id IN ({','.join('?' * len(ids))})", ids)
        fired = [
            {
                "id": r[0],
                "message": r[1],
                "fire_at": datetime.fromtimestamp(r[2], TZ).strftime("%Y-%m-%d %H:%M %Z"),
            }
            for r in rows
        ]
        return json.dumps({"fired": fired, "count": len(fired)})
    except Exception as e:
        return json.dumps({"error": str(e)})
