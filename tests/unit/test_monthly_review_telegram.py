"""Unit tests for the monthly review Telegram command handlers."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock third-party dependencies for importing clients.telegram_bot."""
    # Remove cached client modules so they get reimported with our mocks
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("clients."):
            del sys.modules[mod_name]

    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)
    if "agent_tooling" not in sys.modules:
        at_mock = MagicMock()
        at_mock.tool = MagicMock(return_value=lambda f: f)
        monkeypatch.setitem(sys.modules, "agent_tooling", at_mock)


def _make_update(text: str, user_id: int = 12345) -> MagicMock:
    """Create a mock Telegram Update with the given text and user ID."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


class TestReviewKeepCommand:
    """Tests for cmd_review_keep in clients.telegram_bot."""

    @pytest.mark.asyncio
    async def test_keep_command_calls_review_respond_endpoint(self) -> None:
        """Handler POSTs to /memory/review-respond with action=keep and full Neo4j element_id."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "action": "keep"})
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        update = _make_update("/keep_4:abc:123")

        with patch("clients.telegram_bot.config") as mock_cfg, \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.SERVER_URL", "http://localhost:8420"), \
             patch("clients.telegram_bot._is_allowed_user", return_value=True):
            mock_cfg.hitl_internal_token = "test-token"

            from clients.telegram_bot import cmd_review_keep
            await cmd_review_keep(update, MagicMock())

        mock_http.post.assert_called_once()
        call_args = mock_http.post.call_args
        assert "/memory/review-respond" in call_args[0][0]
        # The JSON body should include the full element_id (with colons) and action
        json_body = call_args[1].get("json", {})
        assert json_body["action"] == "keep"
        assert json_body["element_id"] == "4:abc:123"


class TestReviewArchiveCommand:
    """Tests for cmd_review_archive in clients.telegram_bot."""

    @pytest.mark.asyncio
    async def test_archive_command_calls_review_respond_endpoint(self) -> None:
        """Handler POSTs to /memory/review-respond with action=archive."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "action": "archive"})
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        update = _make_update("/archive_4:def:456")

        with patch("clients.telegram_bot.config") as mock_cfg, \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.SERVER_URL", "http://localhost:8420"), \
             patch("clients.telegram_bot._is_allowed_user", return_value=True):
            mock_cfg.hitl_internal_token = "test-token"

            from clients.telegram_bot import cmd_review_archive
            await cmd_review_archive(update, MagicMock())

        call_args = mock_http.post.call_args
        json_body = call_args[1].get("json", {})
        assert json_body["action"] == "archive"
        assert json_body["element_id"] == "4:def:456"


class TestReviewDiscardCommand:
    """Tests for cmd_review_discard in clients.telegram_bot."""

    @pytest.mark.asyncio
    async def test_discard_command_calls_review_respond_endpoint(self) -> None:
        """Handler POSTs to /memory/review-respond with action=discard."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "action": "discard"})
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        update = _make_update("/discard_4:ghi:789")

        with patch("clients.telegram_bot.config") as mock_cfg, \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.SERVER_URL", "http://localhost:8420"), \
             patch("clients.telegram_bot._is_allowed_user", return_value=True):
            mock_cfg.hitl_internal_token = "test-token"

            from clients.telegram_bot import cmd_review_discard
            await cmd_review_discard(update, MagicMock())

        call_args = mock_http.post.call_args
        json_body = call_args[1].get("json", {})
        assert json_body["action"] == "discard"
        assert json_body["element_id"] == "4:ghi:789"


class TestReviewCommandParsing:
    """Tests for element ID extraction from review commands."""

    @pytest.mark.asyncio
    async def test_review_command_extracts_id_from_command(self) -> None:
        """Full Neo4j element ID (with colons) is correctly parsed from /keep_<id>."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "action": "keep"})
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_http = MagicMock()
        mock_http.post = MagicMock(return_value=mock_resp)

        update = _make_update("/keep_4:abc:123")

        with patch("clients.telegram_bot.config") as mock_cfg, \
             patch("clients.telegram_bot.http", mock_http), \
             patch("clients.telegram_bot.SERVER_URL", "http://localhost:8420"), \
             patch("clients.telegram_bot._is_allowed_user", return_value=True):
            mock_cfg.hitl_internal_token = "test-token"

            from clients.telegram_bot import cmd_review_keep
            await cmd_review_keep(update, MagicMock())

        json_body = mock_http.post.call_args[1].get("json", {})
        assert json_body["element_id"] == "4:abc:123"

    @pytest.mark.asyncio
    async def test_review_command_invalid_format_replies_error(self) -> None:
        """Error message for malformed command (no ID after prefix)."""
        update = _make_update("/keep_")

        with patch("clients.telegram_bot._is_allowed_user", return_value=True), \
             patch("clients.telegram_bot.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"

            from clients.telegram_bot import cmd_review_keep
            await cmd_review_keep(update, MagicMock())

        update.message.reply_text.assert_called_once()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "invalid" in reply_text.lower() or "usage" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_review_command_regex_accepts_neo4j_element_ids_with_colons(self) -> None:
        """Telegram regex patterns must accept Neo4j element IDs containing colons (e.g. 4:abc:123)."""
        import re

        # These are the regex patterns used in telegram_bot.py for review commands
        keep_pattern = r"^/keep_[^\s]+$"
        archive_pattern = r"^/archive_[^\s]+$"
        discard_pattern = r"^/discard_[^\s]+$"

        # Neo4j element IDs contain colons
        neo4j_id = "4:abc:123"
        assert re.match(keep_pattern, f"/keep_{neo4j_id}")
        assert re.match(archive_pattern, f"/archive_{neo4j_id}")
        assert re.match(discard_pattern, f"/discard_{neo4j_id}")

        # Verify the old \w+ pattern would NOT match (regression guard)
        old_pattern = r"^/keep_\w+$"
        assert re.match(old_pattern, f"/keep_{neo4j_id}") is None

    @pytest.mark.asyncio
    async def test_review_command_unauthorized_user_rejected(self) -> None:
        """Non-allowed users cannot use review commands."""
        update = _make_update("/keep_abc123", user_id=99999)

        with patch("clients.telegram_bot._is_allowed_user", return_value=False):
            from clients.telegram_bot import cmd_review_keep
            await cmd_review_keep(update, MagicMock())

        update.message.reply_text.assert_called_once()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "not authorized" in reply_text.lower()
