"""Integration tests for the memory expiry flow.

Tests the full flow from expired entries to deletion/Telegram prompt,
and from memory_store with validation of expires_at and recurring.
Updated for Lucent (SQLite) backend.
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import numpy as np
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


class TestExpiredNonRecurringDeletion:
    """Integration test: expired non-recurring entries are deleted."""

    def test_expired_non_recurring_entry_is_deleted(self) -> None:
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        conn.execute(
            """INSERT INTO memories (agent_id, content, data_class, expires_at, recurring, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("ada", "Doctor appointment at 2pm", "timed-event", past, 0, 1000),
        )
        conn.commit()

        from core import memory_expiry

        with _patch_conn(conn), patch.object(memory_expiry, "_telegram_direct"):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 1
        assert result["prompted"] == 0
        assert result["errors"] == 0

        # Verify the row is gone
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        assert row[0] == 0


class TestExpiredRecurringTelegramPrompt:
    """Integration test: expired recurring entries trigger Telegram prompt."""

    def test_expired_recurring_entry_triggers_telegram(self) -> None:
        conn = _make_test_conn()
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

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

        assert result["prompted"] == 1
        assert result["deleted"] == 0

        mock_tg.assert_called_once()
        msg = mock_tg.call_args[0][0]
        assert "Mom's birthday dinner" in msg

        # Row should still exist (not deleted)
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        assert row[0] == 1


class TestMemoryStoreExpiresAtValidation:
    """Integration test: memory_store rejects unresolved expires_at."""

    def test_memory_store_rejects_unresolved_expires_at(self) -> None:
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with (
            _patch_conn(conn),
            patch.object(lm, "_embed", return_value=[0.1] * 4096),
        ):
            result_str = lm.memory_store_direct(
                content="Meet at coffee shop",
                data_class="timed-event",
                source="user",
                expires_at="next Friday",
            )
            result = json.loads(result_str)
            assert result["stored"] is False
            assert "resolved absolute ISO datetime" in result.get("error", "")


class TestMemoryStoreRecurringFromContent:
    """Integration test: memory_store sets recurring=True from content keywords."""

    def test_memory_store_sets_recurring_from_content(self) -> None:
        conn = _make_test_conn()
        import tools.stateful.lucent_memory as lm

        with (
            _patch_conn(conn),
            patch.object(lm, "_embed", return_value=[0.1] * 4096),
        ):
            result_str = lm.memory_store_direct(
                content="Mom's birthday dinner at Olive Garden",
                data_class="timed-event",
                source="user",
                expires_at="2026-04-01T18:00:00Z",
            )
            result = json.loads(result_str)
            assert result["stored"] is True

            # Check that recurring=True was stored
            row = conn.execute(
                "SELECT recurring FROM memories WHERE id = ?",
                (result["id"],),
            ).fetchone()
            assert row["recurring"] == 1  # True stored as 1 in SQLite
