"""Shared test fixtures for unit tests.

Provides conditional mocks for third-party dependencies. When a real package
is installed (the default — see requirements.txt), the mock is skipped so
tests run against actual library behavior.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

for _real_pkg in ("telegram", "telegram.ext", "discord", "discord.app_commands", "anthropic", "keyring", "requests"):
    try:
        __import__(_real_pkg)
    except ImportError:
        pass


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
    """Conditionally mock missing third-party modules for unit tests."""
    if "telegram" not in sys.modules:
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

    if "discord" not in sys.modules:
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

    if "keyring" not in sys.modules:
        keyring_mock = _create_mock_module("keyring")
        keyring_mock.get_password = MagicMock(return_value=None)
        monkeypatch.setitem(sys.modules, "keyring", keyring_mock)

    if "requests" not in sys.modules:
        requests_mock = _create_mock_module("requests")
        requests_mock.post = MagicMock()
        requests_mock.get = MagicMock()
        monkeypatch.setitem(sys.modules, "requests", requests_mock)

    if "anthropic" not in sys.modules:
        anthropic_mock = _create_mock_module("anthropic")
        anthropic_mock.AsyncAnthropic = MagicMock()
        monkeypatch.setitem(sys.modules, "anthropic", anthropic_mock)
