#!/usr/bin/env python3
"""One-time reminder system backed by SQLite.

Standalone stateless tool. Dependencies: dateparser.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Allow importing core modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

_DEFAULT_DB = "/usr/src/app/data/reminders.db"
TZ = ZoneInfo("America/Chicago")


def _conn(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
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


def _parse_when(when_str: str, test_mode: bool = False) -> datetime | None:
    """Parse a when string into a timezone-aware datetime.

    In test mode, uses stdlib strptime with format '%Y-%m-%d %H:%M'.
    Otherwise, uses dateparser for natural language parsing.
    """
    if test_mode:
        try:
            naive = datetime.strptime(when_str, "%Y-%m-%d %H:%M")
            return naive.replace(tzinfo=TZ)
        except ValueError:
            return None
    else:
        from dateparser import parse as dp_parse

        return dp_parse(when_str, settings={"TIMEZONE": "America/Chicago", "RETURN_AS_TIMEZONE_AWARE": True})


def cmd_set(args: argparse.Namespace) -> int:
    try:
        dt = _parse_when(args.when, getattr(args, "test_mode", False))
        if dt is None:
            print(json.dumps({"error": f"Could not parse time: {args.when!r}"}))
            return 1
        fire_at = int(dt.timestamp())
        now = int(datetime.now(TZ).timestamp())
        if fire_at <= now:
            print(json.dumps({"error": "Reminder time is in the past."}))
            return 1
        with _conn(args.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO reminders (message, fire_at, created_at) VALUES (?, ?, ?)",
                (args.message, fire_at, now),
            )
            rid = cur.lastrowid
        print(json.dumps({
            "set": True,
            "id": rid,
            "message": args.message,
            "fire_at": dt.strftime("%Y-%m-%d %H:%M %Z"),
        }))
        return 0
    except ImportError:
        print(json.dumps({"error": "dateparser not installed. Run: pip install dateparser"}))
        return 1
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    try:
        with _conn(args.db_path) as conn:
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
        print(json.dumps({"reminders": reminders, "count": len(reminders)}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_delete(args: argparse.Namespace) -> int:
    try:
        with _conn(args.db_path) as conn:
            conn.execute("DELETE FROM reminders WHERE id = ?", (args.reminder_id,))
        print(json.dumps({"deleted": True, "id": args.reminder_id}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def cmd_due(args: argparse.Namespace) -> int:
    try:
        now = int(datetime.now(TZ).timestamp())
        with _conn(args.db_path) as conn:
            rows = conn.execute(
                "SELECT id, message, fire_at FROM reminders WHERE fire_at <= ?", (now,)
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM reminders WHERE id IN ({placeholders})", ids)
        fired = [
            {
                "id": r[0],
                "message": r[1],
                "fire_at": datetime.fromtimestamp(r[2], TZ).strftime("%Y-%m-%d %H:%M %Z"),
            }
            for r in rows
        ]
        print(json.dumps({"fired": fired, "count": len(fired)}))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Reminder management tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sp = subparsers.add_parser("set", help="Set a reminder")
    sp.add_argument("--message", required=True, help="Reminder message")
    sp.add_argument("--when", required=True, help="When to fire (e.g. '2026-03-01 14:30', 'tomorrow at 9am')")
    sp.add_argument("--test-mode", action="store_true", default=False,
                    help="Use stdlib strptime (format: '%%Y-%%m-%%d %%H:%%M') instead of dateparser")
    sp.add_argument("--db-path", default=_DEFAULT_DB, help="SQLite database path")

    lp = subparsers.add_parser("list", help="List pending reminders")
    lp.add_argument("--db-path", default=_DEFAULT_DB, help="SQLite database path")

    dp = subparsers.add_parser("delete", help="Delete a reminder")
    dp.add_argument("--reminder-id", type=int, required=True, help="Reminder ID")
    dp.add_argument("--db-path", default=_DEFAULT_DB, help="SQLite database path")

    due_p = subparsers.add_parser("due", help="Get and fire due reminders")
    due_p.add_argument("--db-path", default=_DEFAULT_DB, help="SQLite database path")

    args = parser.parse_args()

    commands = {
        "set": cmd_set,
        "list": cmd_list,
        "delete": cmd_delete,
        "due": cmd_due,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
