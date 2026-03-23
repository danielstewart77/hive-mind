"""Unit tests for the memory expiry sweep module (core.memory_expiry)."""

import logging
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j, agent_tooling, and keyring for importing agents.memory / core.memory_expiry."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)


def _make_expired_record(
    content: str,
    expires_at: str,
    recurring: bool,
    element_id: str = "test-id-1",
) -> dict:
    """Create a mock record matching the sweep query result shape."""
    return {
        "content": content,
        "expires_at": expires_at,
        "recurring": recurring,
        "id": element_id,
    }


def _make_mock_driver_with_results(records: list[dict]) -> MagicMock:
    """Create a mock Neo4j driver that returns given records from the query."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    # The first run call is the query; subsequent calls are deletes
    mock_query_result = MagicMock()
    mock_query_result.__iter__ = MagicMock(return_value=iter(records))

    mock_delete_result = MagicMock()

    # First call returns query results, all subsequent calls return delete results
    mock_session.run.side_effect = [mock_query_result] + [mock_delete_result] * len(records)

    return mock_driver


class TestSweepExpiredEvents:
    """Tests for sweep_expired_events in core.memory_expiry."""

    def test_sweep_deletes_expired_non_recurring_events(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        records = [
            _make_expired_record("Meeting at 3pm", past, False, "id-1"),
            _make_expired_record("Dentist appointment", past, False, "id-2"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
            patch.object(memory_expiry, "_telegram_direct") as mock_telegram,
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 2
        assert result["prompted"] == 0
        assert result["errors"] == 0
        mock_telegram.assert_not_called()

    def test_sweep_prompts_for_expired_recurring_events(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        records = [
            _make_expired_record("Mom's birthday dinner", past, True, "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
            patch.object(memory_expiry, "_telegram_direct", return_value=(True, "sent")) as mock_telegram,
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 0
        assert result["prompted"] == 1
        assert result["errors"] == 0
        mock_telegram.assert_called_once()
        # Check the message contains the event content
        call_msg = mock_telegram.call_args[0][0]
        assert "Mom's birthday dinner" in call_msg

    def test_sweep_mixed_expired_events(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        records = [
            _make_expired_record("Meeting at 3pm", past, False, "id-1"),
            _make_expired_record("Dentist appointment", past, False, "id-2"),
            _make_expired_record("Mom's birthday dinner", past, True, "id-3"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
            patch.object(memory_expiry, "_telegram_direct", return_value=(True, "sent")),
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 2
        assert result["prompted"] == 1
        assert result["errors"] == 0

    def test_sweep_no_expired_events(self) -> None:
        mock_driver = _make_mock_driver_with_results([])

        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
            patch.object(memory_expiry, "_telegram_direct"),
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 0
        assert result["prompted"] == 0
        assert result["errors"] == 0

    def test_sweep_logs_deletions(self, caplog: pytest.LogCaptureFixture) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        records = [
            _make_expired_record("Meeting at 3pm", past, False, "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import memory_expiry

        with (
            caplog.at_level(logging.INFO, logger="core.memory_expiry"),
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
            patch.object(memory_expiry, "_telegram_direct"),
        ):
            memory_expiry.sweep_expired_events()

        assert any("Meeting at 3pm" in record.message for record in caplog.records)

    def test_sweep_handles_neo4j_error_gracefully(self) -> None:
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_session.run.side_effect = Exception("Neo4j connection lost")

        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
            patch.object(memory_expiry, "_telegram_direct"),
        ):
            result = memory_expiry.sweep_expired_events()

        assert result["deleted"] == 0
        assert result["prompted"] == 0
        assert result["errors"] == 1

    def test_sweep_telegram_failure_does_not_block(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        records = [
            _make_expired_record("Mom's birthday dinner", past, True, "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import memory_expiry

        with (
            patch.object(memory_expiry, "_get_driver", return_value=mock_driver),
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
