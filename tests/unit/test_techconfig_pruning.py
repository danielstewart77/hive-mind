"""Unit tests for the techconfig pruning sweep module (core.techconfig_pruning)."""

import logging
import sys
from unittest.mock import MagicMock, patch, call

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j, agent_tooling, and keyring for importing agents.memory / core modules."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_memory_record(
    content: str,
    codebase_ref: str | None,
    element_id: str = "test-id-1",
) -> dict:
    """Create a mock record matching the sweep query result shape."""
    return {
        "content": content,
        "codebase_ref": codebase_ref,
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

    mock_update_result = MagicMock()

    # First call returns query results, subsequent calls are SET operations
    mock_session.run.side_effect = [mock_query_result] + [mock_update_result] * len(records)

    return mock_driver


class TestSweepTechconfigEntries:
    """Tests for sweep_techconfig_entries in core.techconfig_pruning."""

    def test_sweep_queries_technical_config_entries(self) -> None:
        """The Cypher query filters by data_class='technical-config'."""
        records: list[dict] = []
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct"),
        ):
            techconfig_pruning.sweep_techconfig_entries()

        mock_session = mock_driver.session.return_value.__enter__.return_value
        query_call = mock_session.run.call_args_list[0]
        cypher = query_call[0][0]
        assert "technical-config" in cypher
        assert "data_class" in cypher

    def test_sweep_verified_entry_not_modified(self) -> None:
        """Entry verified by heuristic is not changed in Neo4j; verified count incremented."""
        records = [
            _make_memory_record("server.py uses FastAPI", "server.py", "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        mock_verify = MagicMock(return_value=VerificationResult(
            status="verified",
            reason="File exists and keywords found",
            content="server.py uses FastAPI",
            element_id="id-1",
            codebase_ref="server.py",
        ))

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct"),
            patch("core.techconfig_pruning.verify_entry", mock_verify),
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        assert result["verified"] == 1
        assert result["pruned"] == 0
        # Only the query call, no SET call for verified entries
        mock_session = mock_driver.session.return_value.__enter__.return_value
        assert len(mock_session.run.call_args_list) == 1

    def test_sweep_pruned_entry_marked_superseded(self) -> None:
        """Entry that fails verification gets superseded=True set via Cypher."""
        records = [
            _make_memory_record("server.py uses Flask", "server.py", "id-2"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        mock_verify = MagicMock(return_value=VerificationResult(
            status="pruned",
            reason="Keywords not found in file",
            content="server.py uses Flask",
            element_id="id-2",
            codebase_ref="server.py",
        ))

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct"),
            patch("core.techconfig_pruning.verify_entry", mock_verify),
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        assert result["pruned"] == 1
        mock_session = mock_driver.session.return_value.__enter__.return_value
        # Second call should be the SET superseded
        assert len(mock_session.run.call_args_list) >= 2
        set_call = mock_session.run.call_args_list[1]
        assert "superseded" in set_call[0][0]
        assert set_call[1]["id"] == "id-2"

    def test_sweep_flagged_entry_not_modified(self) -> None:
        """Entry that is flagged for review is not changed in Neo4j."""
        records = [
            _make_memory_record("some vague config note", None, "id-3"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        mock_verify = MagicMock(return_value=VerificationResult(
            status="flagged",
            reason="Cannot verify",
            content="some vague config note",
            element_id="id-3",
            codebase_ref=None,
        ))

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct"),
            patch("core.techconfig_pruning.verify_entry", mock_verify),
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        assert result["flagged"] == 1
        assert result["pruned"] == 0
        mock_session = mock_driver.session.return_value.__enter__.return_value
        # Only the query call, no SET call for flagged entries
        assert len(mock_session.run.call_args_list) == 1

    def test_sweep_sends_telegram_summary_with_counts(self) -> None:
        """After sweep, Telegram is called with verified/pruned/flagged counts."""
        records = [
            _make_memory_record("server.py uses FastAPI", "server.py", "id-1"),
            _make_memory_record("server.py uses Flask", "server.py", "id-2"),
            _make_memory_record("some vague note", None, "id-3"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        verify_results = [
            VerificationResult("verified", "OK", records[0]["content"], "id-1", "server.py"),
            VerificationResult("pruned", "Not found", records[1]["content"], "id-2", "server.py"),
            VerificationResult("flagged", "Cannot verify", records[2]["content"], "id-3", None),
        ]
        mock_verify = MagicMock(side_effect=verify_results)

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct") as mock_telegram,
            patch("core.techconfig_pruning.verify_entry", mock_verify),
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        assert result["verified"] == 1
        assert result["pruned"] == 1
        assert result["flagged"] == 1

        # Telegram should have been called at least once for the summary
        assert mock_telegram.call_count >= 1
        summary_msg = mock_telegram.call_args_list[0][0][0]
        assert "1" in summary_msg  # verified count
        assert "pruned" in summary_msg.lower() or "Pruned" in summary_msg

    def test_sweep_sends_flagged_entries_in_review_message(self) -> None:
        """Flagged entries are batched into a Telegram message with their content."""
        records = [
            _make_memory_record("flaggable entry content", None, "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        mock_verify = MagicMock(return_value=VerificationResult(
            status="flagged",
            reason="Cannot verify",
            content="flaggable entry content",
            element_id="id-1",
            codebase_ref=None,
        ))

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct") as mock_telegram,
            patch("core.techconfig_pruning.verify_entry", mock_verify),
        ):
            techconfig_pruning.sweep_techconfig_entries()

        # Should have a second message for flagged entries
        assert mock_telegram.call_count >= 2
        flagged_msg = mock_telegram.call_args_list[1][0][0]
        assert "flaggable entry content" in flagged_msg

    def test_sweep_no_telegram_when_nothing_found(self) -> None:
        """No entries found means no Telegram message sent."""
        records: list[dict] = []
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct") as mock_telegram,
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        mock_telegram.assert_not_called()
        assert result["verified"] == 0
        assert result["pruned"] == 0
        assert result["flagged"] == 0

    def test_sweep_neo4j_error_handled_gracefully(self) -> None:
        """Neo4j failure does not raise; errors count incremented."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_session.run.side_effect = Exception("Neo4j connection lost")

        from core import techconfig_pruning

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct"),
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        assert result["errors"] >= 1

    def test_sweep_telegram_failure_does_not_raise(self) -> None:
        """Telegram failure handled gracefully."""
        records = [
            _make_memory_record("server.py uses FastAPI", "server.py", "id-1"),
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        mock_verify = MagicMock(return_value=VerificationResult(
            status="verified",
            reason="OK",
            content="server.py uses FastAPI",
            element_id="id-1",
            codebase_ref="server.py",
        ))

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(
                techconfig_pruning, "_telegram_direct",
                side_effect=Exception("Telegram API down"),
            ),
            patch("core.techconfig_pruning.verify_entry", mock_verify),
        ):
            # Should not raise
            result = techconfig_pruning.sweep_techconfig_entries()

        # Sweep should still have processed entries
        assert result["verified"] == 1

    def test_sweep_returns_result_dict(self) -> None:
        """Return dict has keys: verified, pruned, flagged, errors."""
        records: list[dict] = []
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct"),
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        assert "verified" in result
        assert "pruned" in result
        assert "flagged" in result
        assert "errors" in result

    def test_sweep_empty_results_returns_zeros(self) -> None:
        """No matching entries returns all-zero counts."""
        records: list[dict] = []
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct"),
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        assert result == {"verified": 0, "pruned": 0, "flagged": 0, "errors": 0}
