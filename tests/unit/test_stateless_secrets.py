"""Unit tests for the stateless secrets tool."""

import json
import subprocess
import sys

import pytest

SCRIPT_PATH = "/usr/src/app/tools/stateless/secrets/secrets.py"


class TestStatelessSecrets:
    def test_set_secret_stores_value(self):
        """Asserts set subcommand stores a secret and returns confirmation."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "set",
             "--key", "TEST_API_KEY",
             "--value", "test-value-123"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data.get("stored") is True
        assert data["key"] == "TEST_API_KEY"
        assert result.returncode == 0

    def test_get_secret_confirms_existence(self):
        """Asserts get subcommand confirms a stored secret exists without revealing value."""
        # First store a secret
        subprocess.run(
            [sys.executable, SCRIPT_PATH, "set",
             "--key", "TEST_CHECK_KEY",
             "--value", "hidden-value"],
            capture_output=True, text=True, timeout=10,
        )
        # Then check it
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "get",
             "--key", "TEST_CHECK_KEY"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data.get("configured") is True
        assert "hidden-value" not in result.stdout
        assert result.returncode == 0

    def test_get_secret_reports_missing(self):
        """Asserts get subcommand reports when secret is not found."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "get",
             "--key", "NONEXISTENT_ZZZZZ_KEY"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data.get("configured") is False
        assert result.returncode == 0

    def test_list_secrets_returns_keys(self):
        """Asserts list subcommand returns stored key names."""
        # Store a secret first
        subprocess.run(
            [sys.executable, SCRIPT_PATH, "set",
             "--key", "LIST_TEST_KEY",
             "--value", "list-value"],
            capture_output=True, text=True, timeout=10,
        )
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "list"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "keys" in data
        assert isinstance(data["keys"], list)
        assert result.returncode == 0

    def test_set_rejects_invalid_key_name(self):
        """Asserts set rejects keys that don't match allowed naming patterns."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "set",
             "--key", "badname",
             "--value", "test-value"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "error" in data
        assert result.returncode == 1

    def test_set_rejects_empty_key(self):
        """Asserts set rejects empty key names."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "set",
             "--key", "  ",
             "--value", "test-value"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "error" in data
        assert result.returncode == 1

    def test_exit_codes(self):
        """Asserts exit 0 on successful operations."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "list"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
