"""Unit tests for the orphan node sweep module (core.orphan_sweep)."""

import logging
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j, agent_tooling, and keyring for importing core.orphan_sweep."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_orphan_record(
    name: str,
    labels: list[str],
    created_at: float,
    element_id: str = "test-id-1",
) -> dict:
    """Create a mock record matching the orphan sweep query result shape."""
    return {
        "name": name,
        "labels": labels,
        "created_at": created_at,
        "id": element_id,
    }


def _make_mock_driver_with_results(records: list[dict]) -> MagicMock:
    """Create a mock Neo4j driver that returns given records from the query."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    mock_query_result = MagicMock()
    mock_query_result.__iter__ = MagicMock(return_value=iter(records))
    mock_session.run.return_value = mock_query_result

    return mock_driver


class TestSweepOrphanNodes:
    """Tests for sweep_orphan_nodes in core.orphan_sweep."""

    def test_sweep_finds_orphan_nodes_without_edges(self) -> None:
        """Nodes with zero edges should be returned."""
        stale_time = time.time() - 3600  # 1 hour ago
        records = [
            _make_orphan_record("Orphan1", ["Person"], stale_time, "id-1"),
            _make_orphan_record("Orphan2", ["Concept"], stale_time, "id-2"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import orphan_sweep

        with (
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(orphan_sweep, "_telegram_direct", return_value=(True, "sent")),
        ):
            result = orphan_sweep.sweep_orphan_nodes()

        assert result["orphans_found"] == 2

    def test_sweep_respects_grace_period(self) -> None:
        """Nodes created within 30 minutes should not be flagged."""
        # The Cypher query itself handles the grace period via the cutoff param.
        # We test that sweep returns empty for "recent" nodes by returning
        # no records from the query (the Cypher filters them out).
        mock_driver = _make_mock_driver_with_results([])

        from core import orphan_sweep

        with (
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(orphan_sweep, "_telegram_direct"),
        ):
            result = orphan_sweep.sweep_orphan_nodes()

        assert result["orphans_found"] == 0

    def test_sweep_flags_stale_orphans_past_grace_period(self) -> None:
        """Nodes older than 30 minutes with zero edges should be flagged."""
        stale_time = time.time() - 3600
        records = [
            _make_orphan_record("StaleNode", ["Person"], stale_time, "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import orphan_sweep

        with (
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(orphan_sweep, "_telegram_direct", return_value=(True, "sent")),
        ):
            result = orphan_sweep.sweep_orphan_nodes()

        assert result["orphans_found"] == 1
        assert result["notified"] is True

    def test_sweep_sends_batch_telegram_message(self) -> None:
        """A single batched Telegram message should be sent listing all orphans."""
        stale_time = time.time() - 3600
        records = [
            _make_orphan_record("Orphan1", ["Person"], stale_time, "id-1"),
            _make_orphan_record("Orphan2", ["Concept"], stale_time, "id-2"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import orphan_sweep

        with (
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(orphan_sweep, "_telegram_direct", return_value=(True, "sent")) as mock_tg,
        ):
            orphan_sweep.sweep_orphan_nodes()

        mock_tg.assert_called_once()
        call_msg = mock_tg.call_args[0][0]
        assert "Orphan1" in call_msg
        assert "Orphan2" in call_msg

    def test_sweep_does_not_auto_delete(self) -> None:
        """No DELETE Cypher should be executed."""
        stale_time = time.time() - 3600
        records = [
            _make_orphan_record("Orphan1", ["Person"], stale_time, "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import orphan_sweep

        with (
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(orphan_sweep, "_telegram_direct", return_value=(True, "sent")),
        ):
            orphan_sweep.sweep_orphan_nodes()

        mock_session = mock_driver.session.return_value.__enter__.return_value
        # Only one call -- the query; no delete calls
        assert mock_session.run.call_count == 1
        query = mock_session.run.call_args[0][0]
        assert "DELETE" not in query.upper()

    def test_sweep_empty_results_no_telegram(self) -> None:
        """No Telegram message when no orphans found."""
        mock_driver = _make_mock_driver_with_results([])

        from core import orphan_sweep

        with (
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(orphan_sweep, "_telegram_direct") as mock_tg,
        ):
            result = orphan_sweep.sweep_orphan_nodes()

        assert result["orphans_found"] == 0
        assert result["notified"] is False
        mock_tg.assert_not_called()

    def test_sweep_telegram_failure_does_not_raise(self) -> None:
        """Graceful handling of Telegram errors."""
        stale_time = time.time() - 3600
        records = [
            _make_orphan_record("Orphan1", ["Person"], stale_time, "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import orphan_sweep

        with (
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(
                orphan_sweep, "_telegram_direct",
                side_effect=Exception("Telegram API down"),
            ),
        ):
            result = orphan_sweep.sweep_orphan_nodes()

        assert result["orphans_found"] == 1
        assert result["notified"] is False
        assert result["errors"] == 1

    def test_sweep_returns_result_dict(self) -> None:
        """Return dict should have keys: orphans_found, notified, errors."""
        mock_driver = _make_mock_driver_with_results([])

        from core import orphan_sweep

        with (
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(orphan_sweep, "_telegram_direct"),
        ):
            result = orphan_sweep.sweep_orphan_nodes()

        assert "orphans_found" in result
        assert "notified" in result
        assert "errors" in result

    def test_sweep_logs_orphan_details(self, caplog: pytest.LogCaptureFixture) -> None:
        """Orphan node names should be logged."""
        stale_time = time.time() - 3600
        records = [
            _make_orphan_record("OrphanLogged", ["Person"], stale_time, "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import orphan_sweep

        with (
            caplog.at_level(logging.INFO, logger="core.orphan_sweep"),
            patch.object(orphan_sweep, "_get_driver", return_value=mock_driver),
            patch.object(orphan_sweep, "_telegram_direct", return_value=(True, "sent")),
        ):
            orphan_sweep.sweep_orphan_nodes()

        assert any("OrphanLogged" in record.message for record in caplog.records)
