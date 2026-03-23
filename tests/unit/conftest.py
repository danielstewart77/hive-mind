"""Shared test fixtures for unit tests.

Provides mock modules for third-party dependencies that are not installed
in the test environment (telegram, discord, etc.).
"""

import sys
import types
from unittest.mock import MagicMock

import pytest


def _create_mock_module(name: str, submodules: dict | None = None) -> MagicMock:
    """Create a mock module with optional submodules."""
    mod = MagicMock(spec=types.ModuleType)
    mod.__name__ = name
    if submodules:
        for sub_name, sub_mock in submodules.items():
            setattr(mod, sub_name, sub_mock)
    return mod


@pytest.fixture(autouse=True)
def _mock_third_party_modules(monkeypatch):
    """Mock telegram and discord modules for all unit tests.

    This fixture ensures that tests can import clients/telegram_bot.py and
    clients/discord_bot.py without having python-telegram-bot or discord.py
    installed in the test environment.
    """
    # Remove any previously cached imports of the client modules so they
    # get re-imported with our mocked deps each time.
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("clients."):
            del sys.modules[mod_name]

    # --- Telegram mocks ---
    telegram_mock = _create_mock_module("telegram")
    telegram_mock.Update = MagicMock()

    telegram_ext_mock = _create_mock_module("telegram.ext")
    telegram_ext_mock.ApplicationBuilder = MagicMock()
    telegram_ext_mock.CallbackQueryHandler = MagicMock()
    telegram_ext_mock.CommandHandler = MagicMock()
    telegram_ext_mock.ContextTypes = MagicMock()
    telegram_ext_mock.MessageHandler = MagicMock()
    telegram_ext_mock.filters = MagicMock()

    monkeypatch.setitem(sys.modules, "telegram", telegram_mock)
    monkeypatch.setitem(sys.modules, "telegram.ext", telegram_ext_mock)

    # --- Discord mocks ---
    discord_mock = _create_mock_module("discord")
    discord_mock.Intents = MagicMock()
    discord_mock.Client = MagicMock()
    discord_mock.DMChannel = MagicMock()
    discord_mock.Member = MagicMock()
    discord_mock.User = MagicMock()
    discord_mock.Message = MagicMock()
    discord_mock.VoiceClient = MagicMock()
    discord_mock.FFmpegPCMAudio = MagicMock()
    discord_mock.HTTPException = Exception
    discord_mock.Interaction = MagicMock()

    app_commands_mock = _create_mock_module("discord.app_commands")
    app_commands_mock.CommandTree = MagicMock()
    app_commands_mock.Choice = MagicMock()
    app_commands_mock.describe = MagicMock(return_value=lambda f: f)
    app_commands_mock.autocomplete = MagicMock(return_value=lambda f: f)

    discord_mock.app_commands = app_commands_mock

    monkeypatch.setitem(sys.modules, "discord", discord_mock)
    monkeypatch.setitem(sys.modules, "discord.app_commands", app_commands_mock)

    # --- Keyring mock (for _get_bot_token) ---
    keyring_mock = _create_mock_module("keyring")
    keyring_mock.get_password = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "keyring", keyring_mock)

    # --- Third-party deps for tools/stateful/ ---
    if "requests" not in sys.modules:
        requests_mock = _create_mock_module("requests")
        requests_mock.post = MagicMock()
        requests_mock.get = MagicMock()
        monkeypatch.setitem(sys.modules, "requests", requests_mock)

    if "neo4j" not in sys.modules:
        neo4j_mock = _create_mock_module("neo4j")
        neo4j_mock.GraphDatabase = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)

    if "py2neo" not in sys.modules:
        py2neo_mock = _create_mock_module("py2neo")
        monkeypatch.setitem(sys.modules, "py2neo", py2neo_mock)

    # --- Anthropic SDK mock (for minds/nagatha/) ---
    if "anthropic" not in sys.modules:
        anthropic_mock = _create_mock_module("anthropic")
        anthropic_mock.AsyncAnthropic = MagicMock()
        monkeypatch.setitem(sys.modules, "anthropic", anthropic_mock)
