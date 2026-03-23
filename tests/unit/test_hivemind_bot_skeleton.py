"""Tests for clients/hivemind_bot.py — group chat bot skeleton."""

from pathlib import Path


class TestHiveMindBotSkeleton:
    """Verify the HiveMind bot skeleton has required structure."""

    def test_hivemind_bot_file_exists(self):
        path = Path(__file__).resolve().parents[2] / "clients" / "hivemind_bot.py"
        assert path.exists()

    def test_hivemind_bot_imports_discord(self):
        # The conftest autouse fixture mocks discord, so this should work
        from clients.hivemind_bot import HiveMindGroupBot
        assert HiveMindGroupBot is not None

    def test_hivemind_bot_has_group_session_management(self):
        from clients.hivemind_bot import HiveMindGroupBot
        bot = HiveMindGroupBot()
        assert hasattr(bot, "create_group_session")
        assert callable(bot.create_group_session)

    def test_hivemind_bot_has_new_command(self):
        from clients.hivemind_bot import HiveMindGroupBot
        bot = HiveMindGroupBot()
        assert hasattr(bot, "handle_new_command")
        assert callable(bot.handle_new_command)

    def test_hivemind_bot_uses_group_session_endpoint(self):
        path = Path(__file__).resolve().parents[2] / "clients" / "hivemind_bot.py"
        content = path.read_text()
        assert "/group-sessions" in content
