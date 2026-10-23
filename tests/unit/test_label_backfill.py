"""The three behaviours the label backfill is trusted for.

Each test builds two throwaway SQLite stores, calls `backfill`, and asserts
on the row it actually stored. The code under test is imported, never
reimplemented here.
"""

import sqlite3

import pytest

from unified_db.label_backfill import backfill


def _labels_store(path, rows):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE terminal_labels ("
        " session_id TEXT PRIMARY KEY,"
        " name TEXT NOT NULL DEFAULT '',"
        " color TEXT NOT NULL DEFAULT '',"
        " updated_at INTEGER NOT NULL)"
    )
    con.executemany(
        "INSERT INTO terminal_labels (session_id, name, color, updated_at) "
        "VALUES (?, ?, ?, 0)", rows)
    con.commit()
    con.close()


def _sessions_store(path, rows):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT, color TEXT, status TEXT)")
    con.executemany(
        "INSERT INTO sessions (id, name, color, status) VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def _stored(path, sid):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT name, color FROM sessions WHERE id = ?", (sid,)).fetchone()
    finally:
        con.close()


def test_kept_label_lands_on_its_own_session_row(tmp_path):
    """A label on a closed, unnamed session is written onto that session row."""
    labels = tmp_path / "hive.db"
    sessions = tmp_path / "sessions.db"
    _labels_store(labels, [("sess-closed", "power strip", "#4ee8fc")])
    _sessions_store(sessions, [("sess-closed", "", "", "closed")])

    report = backfill(str(labels), str(sessions))

    assert _stored(sessions, "sess-closed") == ("power strip", "#4ee8fc")
    assert report.placed == {"sess-closed": ("power strip", "#4ee8fc")}
    assert report.can_drop is True


def test_a_single_conflict_stops_every_write(tmp_path):
    """One target row already named means the whole run writes nothing."""
    labels = tmp_path / "hive.db"
    sessions = tmp_path / "sessions.db"
    _labels_store(labels, [
        ("sess-free", "power strip", "#4ee8fc"),
        ("sess-taken", "old label", "#111111"),
    ])
    _sessions_store(sessions, [
        ("sess-free", "", "", "closed"),
        ("sess-taken", "a name somebody set", "#999999", "running"),
    ])

    report = backfill(str(labels), str(sessions))

    # The free row is untouched even though nothing was wrong with it.
    assert _stored(sessions, "sess-free") == ("", "")
    assert _stored(sessions, "sess-taken") == ("a name somebody set", "#999999")
    assert report.placed == {}
    assert "sess-taken" in report.conflicts
    assert report.can_drop is False


def test_a_label_with_no_session_row_is_unplaced_and_blocks_the_drop(tmp_path):
    """A label whose session is gone is reported, written nowhere, and keeps the table."""
    labels = tmp_path / "hive.db"
    sessions = tmp_path / "sessions.db"
    _labels_store(labels, [
        ("sess-gone", "orphan", "#222222"),
        ("sess-free", "power strip", "#4ee8fc"),
    ])
    _sessions_store(sessions, [("sess-free", "", "", "closed")])

    report = backfill(str(labels), str(sessions))

    assert report.unplaced == ["sess-gone"]
    assert _stored(sessions, "sess-gone") is None
    # The placeable label still lands; an orphan blocks the drop, not the move.
    assert _stored(sessions, "sess-free") == ("power strip", "#4ee8fc")
    assert report.placed == {"sess-free": ("power strip", "#4ee8fc")}
    assert report.can_drop is False
