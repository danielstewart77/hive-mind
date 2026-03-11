"""Unit tests for the stateless secrets tool.

Tests the secrets module functions directly with mocked keyring backend.
"""

import json
import argparse
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def _secrets_mod(monkeypatch):
    """Import secrets module with a mocked keyring backend."""
    # Create a simple in-memory keyring mock
    store = {}
    keyring_mock = MagicMock()
    keyring_mock.get_password = MagicMock(side_effect=lambda svc, key: store.get(key))
    keyring_mock.set_password = MagicMock(
        side_effect=lambda svc, key, val: store.__setitem__(key, val)
    )
    monkeypatch.setitem(sys.modules, "keyring", keyring_mock)

    # Clear any cached import
    for mod_name in list(sys.modules.keys()):
        if "tools.stateless.secrets" in mod_name:
            del sys.modules[mod_name]

    import tools.stateless.secrets.secrets as mod
    return mod


class TestStatelessSecrets:
    def test_set_secret_stores_value(self, _secrets_mod, capsys):
        """Asserts set subcommand stores a secret and returns confirmation."""
        args = argparse.Namespace(key="TEST_API_KEY", value="test-value-123")
        rc = _secrets_mod.cmd_set(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data.get("stored") is True
        assert data["key"] == "TEST_API_KEY"
        assert rc == 0

    def test_get_secret_confirms_existence(self, _secrets_mod, capsys):
        """Asserts get subcommand confirms a stored secret exists."""
        # Store first
        _secrets_mod.cmd_set(argparse.Namespace(key="TEST_CHECK_KEY", value="hidden-value"))
        capsys.readouterr()  # discard set output

        rc = _secrets_mod.cmd_get(argparse.Namespace(key="TEST_CHECK_KEY"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data.get("configured") is True
        assert "hidden-value" not in out
        assert rc == 0

    def test_get_secret_reports_missing(self, _secrets_mod, capsys):
        """Asserts get subcommand reports when secret is not found."""
        rc = _secrets_mod.cmd_get(argparse.Namespace(key="NONEXISTENT_ZZZZZ_KEY"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data.get("configured") is False
        assert rc == 0

    def test_list_secrets_returns_keys(self, _secrets_mod, capsys):
        """Asserts list subcommand returns stored key names."""
        _secrets_mod.cmd_set(argparse.Namespace(key="LIST_TEST_KEY", value="list-value"))
        capsys.readouterr()

        rc = _secrets_mod.cmd_list(argparse.Namespace())
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "keys" in data
        assert isinstance(data["keys"], list)
        assert rc == 0

    def test_set_rejects_invalid_key_name(self, _secrets_mod, capsys):
        """Asserts set rejects keys that don't match allowed naming patterns."""
        rc = _secrets_mod.cmd_set(argparse.Namespace(key="badname", value="test-value"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "error" in data
        assert rc == 1

    def test_set_rejects_empty_key(self, _secrets_mod, capsys):
        """Asserts set rejects empty key names."""
        rc = _secrets_mod.cmd_set(argparse.Namespace(key="  ", value="test-value"))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "error" in data
        assert rc == 1

    def test_exit_codes(self, _secrets_mod, capsys):
        """Asserts list returns exit 0."""
        rc = _secrets_mod.cmd_list(argparse.Namespace())
        assert rc == 0
