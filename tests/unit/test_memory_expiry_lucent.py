"""Unit tests for memory_expiry after Lucent migration -- uses real in-memory SQLite."""

import sqlite3
from datetime import datetime, timedelta, timezone
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


class TestSweepExpiredEventsLucent:
    """Tests for sweep_expired_events using SQLite backend."""

    def test_sweep_deletes_non_recurring_expired(self):
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        conn.execute(
            """INSERT INTO memories (agent_id, content, data_class, expires_at, recurring, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("ada", "Meeting at 3pm", "timed-event", past, 0, 1000),
        )
        conn.commit()

        from core import memory_expiry

        with _patch_conn(conn), patch.object(memory_expiry, "_telegram_direct"):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 1
        assert result["prompted"] == 0
        assert result["errors"] == 0

        # Verify row is gone
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        assert row[0] == 0

    def test_sweep_prompts_recurring_expired(self):
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        conn.execute(
            """INSERT INTO memories (agent_id, content, data_class, expires_at, recurring, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("ada", "Mom's birthday dinner", "timed-event", past, 1, 1000),
        )
        conn.commit()

        from core import memory_expiry

        with (
            _patch_conn(conn),
            patch.object(memory_expiry, "_telegram_direct", return_value=(True, "sent")) as mock_tg,
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 0
        assert result["prompted"] == 1
        mock_tg.assert_called_once()
        assert "Mom's birthday dinner" in mock_tg.call_args[0][0]

        # Row should still exist
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        assert row[0] == 1

    def test_sweep_no_expired_entries(self):
        conn = _make_test_conn()
        from core import memory_expiry

        with _patch_conn(conn), patch.object(memory_expiry, "_telegram_direct"):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 0
        assert result["prompted"] == 0
        assert result["errors"] == 0
