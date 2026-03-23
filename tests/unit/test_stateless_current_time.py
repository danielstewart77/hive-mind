"""Unit tests for the stateless current_time tool."""

import json
import subprocess
import sys


SCRIPT_PATH = "/usr/src/app/tools/stateless/current_time/current_time.py"


class TestStatelessCurrentTime:
    def test_current_time_default_timezone(self):
        """Asserts script outputs valid datetime string for America/Chicago."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["timezone"] == "America/Chicago"
        assert "time" in data
        assert len(data["time"]) > 0

    def test_current_time_custom_timezone(self):
        """Asserts script handles --timezone UTC argument."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--timezone", "UTC"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["timezone"] == "UTC"
        assert "time" in data

    def test_current_time_exit_code_zero(self):
        """Asserts exit code 0 on success."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_current_time_invalid_timezone_returns_error(self):
        """Asserts non-zero exit and error JSON for invalid timezone."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--timezone", "Invalid/Zone"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0
        data = json.loads(result.stdout)
        assert "error" in data

    def test_current_time_json_output(self):
        """Asserts stdout is valid JSON with expected keys."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "time" in data
        assert "timezone" in data
