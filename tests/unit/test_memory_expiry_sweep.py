"""Unit tests for the memory expiry sweep module (core.memory_expiry).

Updated for Lucent (SQLite) backend.
"""

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest


def _make_test_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with Lucent schema."""
    import tools.stateful.lucent as lucent_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lucent_mod._init_schema(conn)
    return conn


def _patch_conn(conn):
    return patch("tools.stateful.lucent._get_connection", return_value=conn)


def _insert_expired_record(
    conn: sqlite3.Connection,
    content: str,
    expires_at: str,
    recurring: bool,
) -> None:
    """Insert a timed-event memory into the test DB."""
    conn.execute(
        """INSERT INTO memories (agent_id, content, data_class, expires_at, recurring, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("ada", content, "timed-event", expires_at, 1 if recurring else 0, 1000),
    )
    conn.commit()


class TestSweepExpiredEvents:
    """Tests for sweep_expired_events in core.memory_expiry."""

    def test_sweep_deletes_expired_non_recurring_events(self) -> None:
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _insert_expired_record(conn, "Meeting at 3pm", past, False)
        _insert_expired_record(conn, "Dentist appointment", past, False)

        from core import memory_expiry

        with _patch_conn(conn), patch.object(memory_expiry, "_telegram_direct") as mock_telegram:
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 2
        assert result["prompted"] == 0
        assert result["errors"] == 0
        mock_telegram.assert_not_called()

    def test_sweep_prompts_for_expired_recurring_events(self) -> None:
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _insert_expired_record(conn, "Mom's birthday dinner", past, True)

        from core import memory_expiry

        with (
            _patch_conn(conn),
            patch.object(memory_expiry, "_telegram_direct", return_value=(True, "sent")) as mock_telegram,
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 0
        assert result["prompted"] == 1
        assert result["errors"] == 0
        mock_telegram.assert_called_once()
        call_msg = mock_telegram.call_args[0][0]
        assert "Mom's birthday dinner" in call_msg

    def test_sweep_mixed_expired_events(self) -> None:
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _insert_expired_record(conn, "Meeting at 3pm", past, False)
        _insert_expired_record(conn, "Dentist appointment", past, False)
        _insert_expired_record(conn, "Mom's birthday dinner", past, True)

        from core import memory_expiry

        with (
            _patch_conn(conn),
            patch.object(memory_expiry, "_telegram_direct", return_value=(True, "sent")),
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 2
        assert result["prompted"] == 1
        assert result["errors"] == 0

    def test_sweep_no_expired_events(self) -> None:
        conn = _make_test_conn()

        from core import memory_expiry

        with _patch_conn(conn), patch.object(memory_expiry, "_telegram_direct"):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 0
        assert result["prompted"] == 0
        assert result["errors"] == 0

    def test_sweep_logs_deletions(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _insert_expired_record(conn, "Meeting at 3pm", past, False)

        from core import memory_expiry

        with (
            caplog.at_level(logging.INFO, logger="core.memory_expiry"),
            _patch_conn(conn),
            patch.object(memory_expiry, "_telegram_direct"),
        ):
            memory_expiry.sweep_expired_events()

        assert any("Meeting at 3pm" in record.message for record in caplog.records)

    def test_sweep_handles_db_error_gracefully(self) -> None:
        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_connection", side_effect=Exception("DB error")),
            patch.object(memory_expiry, "_telegram_direct"),
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 0
        assert result["prompted"] == 0
        assert result["errors"] == 1

    def test_sweep_telegram_failure_does_not_block(self) -> None:
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _insert_expired_record(conn, "Mom's birthday dinner", past, True)

        from core import memory_expiry

        with (
            _patch_conn(conn),
            patch.object(
                memory_expiry, "_telegram_direct",
                side_effect=Exception("Telegram API down"),
            ),
        ):
            result = memory_expiry.sweep_expired_events()

        # Sweep completes despite Telegram failure; recurring entry NOT deleted
        assert result["deleted"] == 0
        assert result["prompted"] == 0
        assert result["errors"] == 1
