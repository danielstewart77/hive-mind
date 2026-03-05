"""Unit tests for backfill Telegram command handler -- /classify_* commands."""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_neo4j_and_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock neo4j and agent_tooling for import."""
    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_mock_driver() -> MagicMock:
    """Create a mock Neo4j driver."""
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return mock_driver


class TestApplyClassification:
    """Tests for apply_classification function."""

    def test_classify_command_applies_to_memory_node(self) -> None:
        from agents.memory_backfill import apply_classification

        mock_driver = _make_mock_driver()
        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result = apply_classification("4:abc:0", "person")
            assert "applied" in result.lower() or "classified" in result.lower() or "updated" in result.lower()

        mock_session = mock_driver.session.return_value.__enter__.return_value
        assert mock_session.run.called

    def test_classify_command_applies_to_entity_node(self) -> None:
        from agents.memory_backfill import apply_classification

        mock_driver = _make_mock_driver()
        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result = apply_classification("4:def:0", "technical-config")
            assert "updated" in result.lower() or "classified" in result.lower() or "applied" in result.lower()

    def test_classify_command_sends_confirmation(self) -> None:
        from agents.memory_backfill import apply_classification

        mock_driver = _make_mock_driver()
        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result = apply_classification("4:abc:0", "person")
            # Result should contain confirmation text
            assert "person" in result.lower()

    def test_classify_command_invalid_id_sends_error(self) -> None:
        from agents.memory_backfill import apply_classification

        mock_driver = _make_mock_driver()
        mock_session = mock_driver.session.return_value.__enter__.return_value
        mock_session.run.side_effect = Exception("Node not found")

        with patch("agents.memory_backfill._get_driver", return_value=mock_driver):
            result = apply_classification("invalid-id", "person")
            assert "error" in result.lower()


class TestRegisterNewClass:
    """Tests for registering new data classes at runtime."""

    def test_classify_command_with_new_class_adds_to_registry(self) -> None:
        from core.memory_schema import DATA_CLASS_REGISTRY, register_new_class

        # Register a new class
        new_def = register_new_class("shopping-list", tier="reviewable")
        assert "shopping-list" in DATA_CLASS_REGISTRY
        assert new_def.name == "shopping-list"
        assert new_def.tier == "reviewable"

        # Clean up
        if "shopping-list" in DATA_CLASS_REGISTRY:
            del DATA_CLASS_REGISTRY["shopping-list"]


class TestTelegramClassifyHandler:
    """Tests for the cmd_classify handler in the Telegram bot."""

    @pytest.mark.asyncio
    async def test_classify_handler_routes_valid_command(self) -> None:
        """Verify /classify_<id> <class> is parsed and apply_classification is called."""
        from clients.telegram_bot import cmd_classify

        update = MagicMock()
        update.effective_user.id = 123
        update.message.text = "/classify_4:a:0 person"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot._apply_classification_sync", return_value="Classified 4:a:0 as person (tier: durable)") as mock_apply,
        ):
            await cmd_classify(update, context)

        mock_apply.assert_called_once_with("4:a:0", "person")
        update.message.reply_text.assert_called_once()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "person" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_classify_handler_rejects_unauthorized_user(self) -> None:
        """Non-allowed user cannot classify."""
        from clients.telegram_bot import cmd_classify

        update = MagicMock()
        update.effective_user.id = 999
        update.message.text = "/classify_4:a:0 person"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with patch("clients.telegram_bot._is_allowed_user", return_value=False):
            await cmd_classify(update, context)

        # Should reply with "Not authorized."
        update.message.reply_text.assert_called_once_with("Not authorized.")

    @pytest.mark.asyncio
    async def test_classify_handler_reports_parse_failure(self) -> None:
        """Malformed /classify_ command gets an error reply."""
        from clients.telegram_bot import cmd_classify

        update = MagicMock()
        update.effective_user.id = 123
        update.message.text = "/classify_"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with patch("clients.telegram_bot._is_allowed_user", return_value=True):
            await cmd_classify(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "usage" in reply_text.lower() or "invalid" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_classify_handler_reports_apply_error(self) -> None:
        """When apply_classification returns an error, the handler relays it."""
        from clients.telegram_bot import cmd_classify

        update = MagicMock()
        update.effective_user.id = 123
        update.message.text = "/classify_4:a:0 person"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot._apply_classification_sync", return_value="Error: Node not found"),
        ):
            await cmd_classify(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "error" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_classify_handler_passes_new_class_prefix(self) -> None:
        """Verify new:class-name prefix is passed through correctly."""
        from clients.telegram_bot import cmd_classify

        update = MagicMock()
        update.effective_user.id = 123
        update.message.text = "/classify_4:a:0 new:shopping-list"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with (
            patch("clients.telegram_bot._is_allowed_user", return_value=True),
            patch("clients.telegram_bot._apply_classification_sync", return_value="Classified 4:a:0 as shopping-list (tier: reviewable)") as mock_apply,
        ):
            await cmd_classify(update, context)

        mock_apply.assert_called_once_with("4:a:0", "new:shopping-list")
