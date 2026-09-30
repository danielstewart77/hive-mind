"""The one-time move of names onto the session row, chains and all.

The whole reason this is a function and not a `sqlite3` one-liner: the forward
walk has decisions in it, and a decision nobody can test is a decision nobody
checked.
"""
from __future__ import annotations

import sqlite3

from comms.label_migration import migrate_terminal_labels


def _labels_db(path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE terminal_labels (
               session_id TEXT PRIMARY KEY,
               name TEXT NOT NULL DEFAULT '',
               color TEXT NOT NULL DEFAULT '',
               updated_at INTEGER NOT NULL
           )"""
    )
    conn.executemany(
        "INSERT INTO terminal_labels (session_id, name, color, updated_at) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _sessions_db(path, rows) -> None:
    """`rows` are (id, rotated_from, status, name)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE sessions (
               id TEXT PRIMARY KEY,
               rotated_from TEXT,
               status TEXT NOT NULL,
               name TEXT,
               color TEXT
           )"""
    )
    conn.executemany(
        "INSERT INTO sessions (id, rotated_from, status, name) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _name_of(path, session_id):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT name, color FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    return row


def test_a_stranded_name_lands_on_the_live_conversation_that_continued_it(tmp_path):
    """R4: the walk follows the whole chain, not one hop.

    This is the live shape: "Fittimus Maximus" was given to a session that
    rotated on the 28th, again the next morning, and again that evening, so the
    name sat three rows behind the conversation. Breaks if the walk stops at
    the first successor — the descendant two hops on would keep no name at all.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("a", "Fittimus Maximus", "#3481cc", 100)])
    _sessions_db(sessions, [
        ("a", None, "closed", None),
        ("b", "a", "closed", None),
        ("c", "b", "closed", None),
        ("d", "c", "running", None),
    ])

    report = migrate_terminal_labels(labels, sessions)

    assert report.moved == {"a": "d"}
    assert _name_of(sessions, "d") == ("Fittimus Maximus", "#3481cc")
    assert _name_of(sessions, "a")[0] is None


def test_a_name_whose_conversation_never_rotated_stays_where_it_is(tmp_path):
    """R4: every name survives, including the fifty-eight with no descendant.

    Most names today sit on closed sessions that were simply ended. There is
    nowhere to move them to, and dropping them would lose the name Daniel
    would search for. Breaks if the migration only writes names it can move.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("solo", "Power strip", "", 100)])
    _sessions_db(sessions, [("solo", None, "closed", None)])

    report = migrate_terminal_labels(labels, sessions)

    assert report.kept == ["solo"]
    assert _name_of(sessions, "solo")[0] == "Power strip"


def test_a_chain_that_ends_closed_keeps_its_name_at_the_origin(tmp_path):
    """R4: "the live descendant" has to actually be live.

    Three of the real chains terminate on a closed row. Moving the name onto a
    conversation that is equally unreachable buys nothing and loses the row the
    operator might recognise. Breaks if the walk stops caring about status.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("a", "Security again", "", 100)])
    _sessions_db(sessions, [
        ("a", None, "closed", None),
        ("b", "a", "closed", None),
    ])

    migrate_terminal_labels(labels, sessions)

    assert _name_of(sessions, "a")[0] == "Security again"
    assert _name_of(sessions, "b")[0] is None


def test_a_name_you_have_since_changed_is_not_overwritten_by_its_ancestor(tmp_path):
    """R4: running it twice changes nothing, and neither does running it late.

    This is the failure nobody can see from where they stand: re-run the
    migration after renaming the successor by hand — say because the first
    run's output was inconclusive — and the ancestor's name comes back over
    the top of yours. Breaks if the write stops checking the target.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("a", "Fittimus Maximus", "", 100)])
    _sessions_db(sessions, [
        ("a", None, "closed", None),
        ("b", "a", "running", "Health app"),
    ])

    report = migrate_terminal_labels(labels, sessions)

    assert report.skipped_named == {"a": "b"}
    assert _name_of(sessions, "b")[0] == "Health app"


def test_a_forked_lineage_is_reported_as_forked_rather_than_guessed(tmp_path):
    """R4: two successors means the migration declines to pick one.

    Reachable when `create_session` succeeds inside a rotation and the
    predecessor's retirement then raises: the row stays armed and the next turn
    finalizes a second time. A confident wrong answer about which conversation
    inherited a name is worse than leaving it where a person can place it.
    Breaks if the walk takes the first child it finds.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("a", "Terminal++", "", 100)])
    _sessions_db(sessions, [
        ("a", None, "closed", None),
        ("b", "a", "running", None),
        ("c", "a", "running", None),
    ])

    report = migrate_terminal_labels(labels, sessions)

    assert report.forked == ["a"]
    assert _name_of(sessions, "a")[0] == "Terminal++"
    assert _name_of(sessions, "b")[0] is None
    assert _name_of(sessions, "c")[0] is None


def test_a_name_for_a_session_that_no_longer_exists_is_not_resurrected(tmp_path):
    """A name with no row to attach to is dropped, never made into a session.

    Inventing a row would put a conversation in every picker with no transcript
    behind it. Breaks if the migration inserts rather than updates.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("gone", "Ghost", "", 100)])
    _sessions_db(sessions, [("real", None, "running", None)])

    report = migrate_terminal_labels(labels, sessions)

    assert report.total == 0
    conn = sqlite3.connect(sessions)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_most_recent_name_in_a_lineage_wins_the_descendant(tmp_path):
    """R4: two names in one chain compete, and the newer one takes it.

    A conversation named at its first tile and renamed three rotations later has
    two rows in the old store, both walking forward to the same live session.
    Breaks if the order is reversed — deterministic and wrong, leaving the live
    conversation wearing the name the operator abandoned.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [
        ("a", "old work", "", 100),
        ("b", "current work", "", 200),
    ])
    _sessions_db(sessions, [
        ("a", None, "closed", None),
        ("b", "a", "closed", None),
        ("c", "b", "running", None),
    ])

    migrate_terminal_labels(labels, sessions)

    assert _name_of(sessions, "c")[0] == "current work"


def test_a_name_cleared_after_the_move_does_not_come_back_on_a_re_run(tmp_path):
    """R5: clearing a name removes it, and stays removed.

    A name moves rather than being copied, so the second run has nothing to
    replay. Breaks if the source row survives the move and the guard is only
    "don't overwrite a target that already has a name" — a cleared name is NULL,
    so that guard would put the old one straight back.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("a", "Fittimus Maximus", "", 100)])
    _sessions_db(sessions, [
        ("a", None, "closed", None),
        ("b", "a", "running", None),
    ])

    migrate_terminal_labels(labels, sessions)
    conn = sqlite3.connect(sessions)
    try:
        conn.execute("UPDATE sessions SET name = NULL WHERE id = 'b'")
        conn.commit()
    finally:
        conn.close()

    migrate_terminal_labels(labels, sessions)

    assert _name_of(sessions, "b")[0] is None


def test_a_chain_that_merely_ends_closed_is_not_reported_as_forked(tmp_path):
    """R4: "nowhere live to move to" and "the lineage splits" are different.

    Every such case on the real data is an ordinary chain three rotations deep
    ending on a conversation somebody closed; the database has never contained a
    fork. Breaks if the two are folded back together, which sends the reader
    hunting a branch that does not exist.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("a", "Security again", "", 100)])
    _sessions_db(sessions, [
        ("a", None, "closed", None),
        ("b", "a", "closed", None),
        ("c", "b", "closed", None),
    ])

    report = migrate_terminal_labels(labels, sessions)

    assert report.forked == []
    assert report.kept == ["a"]


def test_the_migration_adds_its_own_columns_so_it_can_run_before_the_restart(tmp_path):
    """R4: the move must be possible before the gateway is restarted.

    A restarted comms answers with no names until this has run, and the browser
    replaces its local cache with whatever the server says — so a comms-first
    rollout destroys the last copy of every name outside the old table. Breaks if
    the migration starts depending on the columns already existing.
    """
    labels = str(tmp_path / "hive.db")
    sessions = str(tmp_path / "sessions.db")
    _labels_db(labels, [("a", "Fittimus Maximus", "", 100)])
    conn = sqlite3.connect(sessions)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, rotated_from TEXT, status TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO sessions (id, rotated_from, status) VALUES ('a', NULL, 'closed')")
    conn.execute("INSERT INTO sessions (id, rotated_from, status) VALUES ('b', 'a', 'running')")
    conn.commit()
    conn.close()

    report = migrate_terminal_labels(labels, sessions)

    assert report.moved == {"a": "b"}
    assert _name_of(sessions, "b")[0] == "Fittimus Maximus"
