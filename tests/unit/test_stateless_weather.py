"""Unit tests for the stateless weather tool."""

import json
import subprocess
import sys

import pytest

SCRIPT_PATH = "/usr/src/app/tools/stateless/weather/weather.py"


class TestStatelessWeather:
    def test_weather_default_location(self):
        """Asserts default location 'missouri city, tx' used when not specified."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["location"] == "missouri city, tx"

    def test_weather_returns_forecast_json(self):
        """Asserts JSON with location, time_span, forecast keys (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--location", "new york, ny", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "location" in data
        assert "time_span" in data
        assert "forecast" in data
        assert len(data["forecast"]) > 0

    def test_weather_time_spans(self):
        """Asserts 'today', 'this week', 'this weekend' all produce valid output."""
        for span in ["today", "this week", "this weekend"]:
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "--time-span", span, "--test-mode"],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["time_span"] == span
            assert "forecast" in data

    def test_weather_exit_code(self):
        """Asserts exit 0 on success."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_weather_json_structure(self):
        """Asserts forecast entries have expected keys."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        for day in data["forecast"]:
            assert "date" in day
            assert "condition" in day
            assert "temp_max_c" in day
            assert "temp_min_c" in day
