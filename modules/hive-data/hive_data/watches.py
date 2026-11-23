"""Persistent watches between stable gateway session IDs."""

from __future__ import annotations

from typing import Any

from .core import _fetch, _fetch_one
from .schema import select_list
from .unit import Unit

Row = dict[str, Any]


async def upsert_watch(watched_session_id: str, sender_session_id: str,
                       task: str, *, unit: Unit | None = None) -> Row:
    """Create a watch or replace the task for the same session pair."""
    row = await _fetch_one(
        unit,
        'INSERT INTO core.session_watches '
        '(watched_session_id, sender_session_id, task) VALUES ($1, $2, $3) '
        'ON CONFLICT (watched_session_id, sender_session_id) '
        'DO UPDATE SET task = EXCLUDED.task '
        f'RETURNING {select_list("session_watches")}',
        watched_session_id, sender_session_id, task)
    assert row is not None
    return row


async def list_watches(watched_session_id: str, *,
                       unit: Unit | None = None) -> list[Row]:
    """List the watches for a session, ordered by sender session ID."""
    return await _fetch(
        unit, f'SELECT {select_list("session_watches")} '
        'FROM core.session_watches WHERE watched_session_id = $1 '
        'ORDER BY sender_session_id', watched_session_id)
