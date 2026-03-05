"""Integration tests for the techconfig pruning sweep flow.

Tests the full flow from queried memory entries through verification
to superseded marking and Telegram notification.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j and agent_tooling for importing agents.memory / core modules."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_mock_driver_with_results(records: list[dict]) -> MagicMock:
    """Create a mock Neo4j driver that returns given records from the sweep query."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

    mock_query_result = MagicMock()
    mock_query_result.__iter__ = MagicMock(return_value=iter(records))
    mock_update_result = MagicMock()

    mock_session.run.side_effect = [mock_query_result] + [mock_update_result] * len(records)

    return mock_driver


class TestSweepVerifiesAccurateEntry:
    """Integration test: accurate entry is verified and NOT marked superseded."""

    def test_sweep_verifies_accurate_entry_end_to_end(self) -> None:
        """Entry with codebase_ref='server.py' and content referencing an existing function
        is verified (not marked superseded)."""
        records = [
            {
                "content": "server.py uses FastAPI as the gateway framework",
                "codebase_ref": "server.py",
                "id": "elem-accurate-1",
            },
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        mock_verify = MagicMock(return_value=VerificationResult(
            status="verified",
            reason="File server.py exists and keywords found",
            content=records[0]["content"],
            element_id="elem-accurate-1",
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
        assert result["flagged"] == 0

        # Verify no SET superseded call was made (only query call)
        mock_session = mock_driver.session.return_value.__enter__.return_value
        assert len(mock_session.run.call_args_list) == 1
        query_cypher = mock_session.run.call_args_list[0][0][0]
        assert "technical-config" in query_cypher


class TestSweepPrunesInaccurateEntry:
    """Integration test: inaccurate entry IS marked superseded."""

    def test_sweep_prunes_inaccurate_entry_end_to_end(self) -> None:
        """Entry with codebase_ref='server.py' and content referencing a non-existent
        function is pruned (marked superseded=True)."""
        records = [
            {
                "content": "server.py uses Flask for routing",
                "codebase_ref": "server.py",
                "id": "elem-inaccurate-1",
            },
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        mock_verify = MagicMock(return_value=VerificationResult(
            status="pruned",
            reason="File server.py exists but keywords not found",
            content=records[0]["content"],
            element_id="elem-inaccurate-1",
            codebase_ref="server.py",
        ))

        with (
            patch.object(techconfig_pruning, "_get_driver", return_value=mock_driver),
            patch.object(techconfig_pruning, "_telegram_direct"),
            patch("core.techconfig_pruning.verify_entry", mock_verify),
        ):
            result = techconfig_pruning.sweep_techconfig_entries()

        assert result["pruned"] == 1
        assert result["verified"] == 0

        # Verify SET superseded call was made
        mock_session = mock_driver.session.return_value.__enter__.return_value
        assert len(mock_session.run.call_args_list) == 2
        set_call = mock_session.run.call_args_list[1]
        assert "superseded" in set_call[0][0]
        assert set_call[1]["id"] == "elem-inaccurate-1"


class TestSweepFlagsUnresolvableEntry:
    """Integration test: unresolvable entry is flagged (not modified, not pruned)."""

    def test_sweep_flags_unresolvable_entry_end_to_end(self) -> None:
        """Entry with no codebase_ref and ambiguous content is flagged."""
        records = [
            {
                "content": "Some vague configuration note about the system",
                "codebase_ref": None,
                "id": "elem-vague-1",
            },
        ]
        mock_driver = _make_mock_driver_with_results(records)

        from core import techconfig_pruning
        from core.techconfig_verifier import VerificationResult

        mock_verify = MagicMock(return_value=VerificationResult(
            status="flagged",
            reason="Cannot verify: no file reference and keywords not found",
            content=records[0]["content"],
            element_id="elem-vague-1",
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
        assert result["verified"] == 0

        # Verify no SET call was made
        mock_session = mock_driver.session.return_value.__enter__.return_value
        assert len(mock_session.run.call_args_list) == 1


class TestSweepSendsSummaryTelegram:
    """Integration test: Telegram summary sent with correct counts."""

    def test_sweep_sends_summary_telegram(self) -> None:
        """Sweep with mixed results sends Telegram with correct summary counts."""
        records = [
            {"content": "server.py uses FastAPI", "codebase_ref": "server.py", "id": "id-1"},
            {"content": "server.py uses Flask", "codebase_ref": "server.py", "id": "id-2"},
            {"content": "vague note", "codebase_ref": None, "id": "id-3"},
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

        # First telegram call: summary
        assert mock_telegram.call_count >= 1
        summary_msg = mock_telegram.call_args_list[0][0][0]
        assert "Verified: 1" in summary_msg
        assert "Pruned: 1" in summary_msg
        assert "Flagged: 1" in summary_msg

        # Second telegram call: flagged entries
        assert mock_telegram.call_count >= 2
        flagged_msg = mock_telegram.call_args_list[1][0][0]
        assert "vague note" in flagged_msg
