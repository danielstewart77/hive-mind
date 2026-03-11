"""Unit tests for the stateless crypto price tool."""

import json
import subprocess
import sys

import pytest

SCRIPT_PATH = "/usr/src/app/tools/stateless/crypto/crypto.py"


class TestStatelessCrypto:
    def test_crypto_price_returns_json(self):
        """Asserts JSON output with price data (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--coin", "bitcoin", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "coin" in data
        assert "price_usd" in data

    def test_crypto_price_exit_code(self):
        """Asserts exit 0 on success in test mode."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--coin", "bitcoin", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_crypto_price_json_structure(self):
        """Asserts JSON output has expected keys in test mode."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--coin", "bitcoin", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "coin" in data
        assert "price_usd" in data

    def test_crypto_price_unknown_coin_test_mode(self):
        """Asserts error message for invalid coin ID in test mode."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--coin", "nonexistent_coin_xyz", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "error" in data

    def test_crypto_price_no_args_defaults(self):
        """Asserts missing --coin argument triggers argparse error."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0
