"""Unit tests for knowledge graph write guards (core.kg_guards).

Tests disambiguation logic, orphan node guard, and Telegram notification.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j, agent_tooling, and keyring for importing core.kg_guards."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


# ---------------------------------------------------------------------------
# Disambiguation tests (Step 1)
# ---------------------------------------------------------------------------
class TestCheckDisambiguation:
    """Tests for check_disambiguation in core.kg_guards."""

    def test_check_disambiguation_no_existing_returns_proceed(self) -> None:
        """When graph has no matching nodes, action should be 'proceed'."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.data.return_value = []
        mock_session.run.return_value = mock_result

        from core import kg_guards

        with patch.object(kg_guards, "_get_driver", return_value=mock_driver):
            result = kg_guards.check_disambiguation("NewEntity", "Person", "ada")

        assert result.action == "proceed"
        assert result.existing_nodes == []

    def test_check_disambiguation_exact_match_returns_merge(self) -> None:
        """When exact match (case-insensitive) found, action should be 'merge'."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {"name": "Daniel", "labels": ["Person"], "id": "node-1"}
        ]
        mock_session.run.return_value = mock_result

        from core import kg_guards

        with patch.object(kg_guards, "_get_driver", return_value=mock_driver):
            result = kg_guards.check_disambiguation("Daniel", "Person", "ada")

        assert result.action == "merge"
        assert len(result.existing_nodes) == 1

    def test_check_disambiguation_similar_name_returns_disambiguate(self) -> None:
        """When a similar but not exact match found, action should be 'disambiguate'."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {"name": "Daniel Stewart", "labels": ["Person"], "id": "node-1"}
        ]
        mock_session.run.return_value = mock_result

        from core import kg_guards

        with patch.object(kg_guards, "_get_driver", return_value=mock_driver):
            result = kg_guards.check_disambiguation("Daniel", "Person", "ada")

        assert result.action == "disambiguate"
        assert len(result.existing_nodes) == 1

    def test_check_disambiguation_different_entity_type_still_checks(self) -> None:
        """Disambiguation works across entity types."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {"name": "Hive Mind", "labels": ["System"], "id": "node-1"}
        ]
        mock_session.run.return_value = mock_result

        from core import kg_guards

        with patch.object(kg_guards, "_get_driver", return_value=mock_driver):
            result = kg_guards.check_disambiguation("Hive Mind", "Project", "ada")

        assert result.action == "merge"
        assert len(result.existing_nodes) == 1

    def test_check_disambiguation_case_insensitive_match(self) -> None:
        """Case-insensitive matching: 'daniel' matches 'Daniel'."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {"name": "Daniel", "labels": ["Person"], "id": "node-1"}
        ]
        mock_session.run.return_value = mock_result

        from core import kg_guards

        with patch.object(kg_guards, "_get_driver", return_value=mock_driver):
            result = kg_guards.check_disambiguation("daniel", "Person", "ada")

        assert result.action == "merge"

    def test_check_disambiguation_returns_existing_nodes(self) -> None:
        """existing_nodes list should be populated with matching node data."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {"name": "Daniel", "labels": ["Person"], "id": "node-1"},
            {"name": "Daniel Stewart", "labels": ["Person"], "id": "node-2"},
        ]
        mock_session.run.return_value = mock_result

        from core import kg_guards

        with patch.object(kg_guards, "_get_driver", return_value=mock_driver):
            result = kg_guards.check_disambiguation("Daniel", "Person", "ada")

        assert len(result.existing_nodes) == 2
        assert result.existing_nodes[0]["name"] == "Daniel"
        assert result.existing_nodes[1]["name"] == "Daniel Stewart"

    def test_check_disambiguation_message_includes_node_names(self) -> None:
        """The message string should contain both proposed and existing node names."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.data.return_value = [
            {"name": "Daniel Stewart", "labels": ["Person"], "id": "node-1"}
        ]
        mock_session.run.return_value = mock_result

        from core import kg_guards

        with patch.object(kg_guards, "_get_driver", return_value=mock_driver):
            result = kg_guards.check_disambiguation("Daniel", "Person", "ada")

        assert "Daniel" in result.message
        assert "Daniel Stewart" in result.message


# ---------------------------------------------------------------------------
# Orphan guard tests (Step 2)
# ---------------------------------------------------------------------------
class TestCheckOrphanGuard:
    """Tests for check_orphan_guard in core.kg_guards."""

    def test_orphan_guard_rejects_no_relation_no_target(self) -> None:
        """Should reject when both relation and target_name are empty."""
        from core import kg_guards

        allowed, msg = kg_guards.check_orphan_guard("", "")
        assert allowed is False
        assert msg != ""

    def test_orphan_guard_allows_with_relation_and_target(self) -> None:
        """Should allow when both relation and target_name are provided."""
        from core import kg_guards

        allowed, msg = kg_guards.check_orphan_guard("MANAGES", "Hive Mind")
        assert allowed is True
        assert msg == ""

    def test_orphan_guard_rejects_relation_without_target(self) -> None:
        """Should reject when relation is set but target_name is empty."""
        from core import kg_guards

        allowed, msg = kg_guards.check_orphan_guard("MANAGES", "")
        assert allowed is False

    def test_orphan_guard_error_message_matches_spec(self) -> None:
        """Error message should contain the spec-defined text."""
        from core import kg_guards

        allowed, msg = kg_guards.check_orphan_guard("", "")
        assert "Cannot create a node without at least one edge" in msg

    def test_orphan_guard_grace_period_allows_orphan(self) -> None:
        """When grace_period=True, orphan writes should be allowed."""
        from core import kg_guards

        allowed, msg = kg_guards.check_orphan_guard("", "", grace_period=True)
        assert allowed is True
        assert msg == ""

    def test_orphan_guard_grace_period_default_false(self) -> None:
        """Default behavior should reject orphans (grace_period defaults to False)."""
        from core import kg_guards

        allowed, msg = kg_guards.check_orphan_guard("", "")
        assert allowed is False


# ---------------------------------------------------------------------------
# Telegram disambiguation notification tests (Step 3)
# ---------------------------------------------------------------------------
class TestSendDisambiguationMessage:
    """Tests for send_disambiguation_message in core.kg_guards."""

    def test_send_disambiguation_message_calls_telegram(self) -> None:
        """Should call _telegram_direct with the formatted message."""
        from core import kg_guards

        with patch.object(
            kg_guards, "_telegram_direct", return_value=(True, "sent")
        ) as mock_tg:
            result = kg_guards.send_disambiguation_message(
                "Daniel", [{"name": "Daniel Stewart", "labels": ["Person"]}]
            )

        assert result is True
        mock_tg.assert_called_once()

    def test_send_disambiguation_message_includes_proposed_name(self) -> None:
        """Message should contain the proposed node name."""
        from core import kg_guards

        with patch.object(
            kg_guards, "_telegram_direct", return_value=(True, "sent")
        ) as mock_tg:
            kg_guards.send_disambiguation_message(
                "Daniel", [{"name": "Dan", "labels": ["Person"]}]
            )

        call_msg = mock_tg.call_args[0][0]
        assert "Daniel" in call_msg

    def test_send_disambiguation_message_includes_existing_nodes(self) -> None:
        """Message should list existing matching node names."""
        from core import kg_guards

        with patch.object(
            kg_guards, "_telegram_direct", return_value=(True, "sent")
        ) as mock_tg:
            kg_guards.send_disambiguation_message(
                "Daniel",
                [
                    {"name": "Daniel Stewart", "labels": ["Person"]},
                    {"name": "Dan", "labels": ["Person"]},
                ],
            )

        call_msg = mock_tg.call_args[0][0]
        assert "Daniel Stewart" in call_msg
        assert "Dan" in call_msg

    def test_send_disambiguation_message_includes_choice_options(self) -> None:
        """Message should contain choice text for the user."""
        from core import kg_guards

        with patch.object(
            kg_guards, "_telegram_direct", return_value=(True, "sent")
        ) as mock_tg:
            kg_guards.send_disambiguation_message(
                "Daniel", [{"name": "Dan", "labels": ["Person"]}]
            )

        call_msg = mock_tg.call_args[0][0]
        # Should contain some variation of yes/no/new/skip/merge
        assert any(
            word in call_msg.lower()
            for word in ["yes", "merge", "new", "skip"]
        )

    def test_send_disambiguation_message_handles_telegram_failure(self) -> None:
        """Should return False when Telegram fails, not raise."""
        from core import kg_guards

        with patch.object(
            kg_guards,
            "_telegram_direct",
            side_effect=Exception("Telegram API down"),
        ):
            result = kg_guards.send_disambiguation_message(
                "Daniel", [{"name": "Dan", "labels": ["Person"]}]
            )

        assert result is False
