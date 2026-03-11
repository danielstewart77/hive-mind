"""Integration tests for the memory expiry flow.

Tests the full flow from expired entries to deletion/Telegram prompt,
and from memory_store with validation of expires_at and recurring.
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j and agent_tooling for importing agents.memory / core.memory_expiry."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)


def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    mock_result = MagicMock()
    mock_result.single.return_value = {"id": "test-id-123"}
    mock_session.run.return_value = mock_result
    return mock_driver


def _make_mock_driver_with_expired_records(records: list[dict]) -> MagicMock:
    """Create a mock Neo4j driver that returns expired records from the sweep query."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    mock_query_result = MagicMock()
    mock_query_result.__iter__ = MagicMock(return_value=iter(records))
    mock_delete_result = MagicMock()

    mock_session.run.side_effect = [mock_query_result] + [mock_delete_result] * len(records)
    return mock_driver


class TestExpiredNonRecurringDeletion:
    """Integration test: expired non-recurring entries are deleted."""

    def test_expired_non_recurring_entry_is_deleted(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        records = [
            {
                "content": "Doctor appointment at 2pm",
                "expires_at": past,
                "recurring": False,
                "id": "element-id-1",
            },
        ]
        mock_driver = _make_mock_driver_with_expired_records(records)

        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
            patch.object(memory_expiry, "_telegram_direct"),
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 1
        assert result["prompted"] == 0
        assert result["errors"] == 0

        # Verify the delete Cypher was called with the correct element ID
        mock_session = mock_driver.session.return_value.__enter__.return_value
        # Second call should be the delete
        delete_call = mock_session.run.call_args_list[1]
        assert "DETACH DELETE" in delete_call[0][0]
        assert delete_call[1]["id"] == "element-id-1"


class TestExpiredRecurringTelegramPrompt:
    """Integration test: expired recurring entries trigger Telegram prompt."""

    def test_expired_recurring_entry_triggers_telegram(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        records = [
            {
                "content": "Mom's birthday dinner",
                "expires_at": past,
                "recurring": True,
                "id": "element-id-2",
            },
        ]
        mock_driver = _make_mock_driver_with_expired_records(records)

        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
            patch.object(memory_expiry, "_telegram_direct", return_value=(True, "sent")) as mock_tg,
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["prompted"] == 1
        assert result["deleted"] == 0

        # Verify Telegram message contains event content
        mock_tg.assert_called_once()
        msg = mock_tg.call_args[0][0]
        assert "Mom's birthday dinner" in msg

        # Verify the node was NOT deleted (no delete Cypher after the query)
        mock_session = mock_driver.session.return_value.__enter__.return_value
        # Only one call: the query. No second call for delete.
        assert len(mock_session.run.call_args_list) == 1


class TestMemoryStoreExpiresAtValidation:
    """Integration test: memory_store rejects unresolved expires_at."""

    def test_memory_store_rejects_unresolved_expires_at(self) -> None:
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
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
        mock_driver = _make_mock_driver()
        import tools.stateful.memory as mem_mod

        with (
            patch.object(mem_mod, "_get_driver", return_value=mock_driver),
            patch.object(mem_mod, "_embed", return_value=[0.1] * 4096),
            patch.object(mem_mod, "_index_created", True),
        ):
            result_str = mem_mod.memory_store_direct(
                content="Mom's birthday dinner at Olive Garden",
                data_class="timed-event",
                source="user",
                expires_at="2026-04-01T18:00:00Z",
            )
            result = json.loads(result_str)
            assert result["stored"] is True

            # Check that recurring=True was passed to Neo4j
            mock_session = mock_driver.session.return_value.__enter__.return_value
            call_args = mock_session.run.call_args
            params = call_args[1]
            assert params["recurring"] is True
