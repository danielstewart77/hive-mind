"""Unit tests for the stateless X/Twitter API tool."""

import json
import subprocess
import sys

import pytest

SCRIPT_PATH = "/usr/src/app/tools/stateless/x_api/x_api.py"


class TestStatelessXApi:
    def test_x_search_returns_tweets_json(self):
        """Asserts JSON with tweets array (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "search", "--query", "#AI", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "tweets" in data
        assert data["query"] == "#AI"
        assert len(data["tweets"]) > 0

    def test_x_search_missing_bearer_token(self):
        """Asserts error when no bearer token and not in test mode."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "search", "--query", "#AI"],
            capture_output=True, text=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        )
        data = json.loads(result.stdout)
        assert "error" in data

    def test_x_thread_replies_returns_json(self):
        """Asserts replies JSON (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "replies", "--conversation-id", "12345", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "replies" in data
        assert data["conversation_id"] == "12345"

    def test_x_api_sorts_by_engagement(self):
        """Asserts tweets sorted by likes + reposts descending."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "search", "--query", "test", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        tweets = data["tweets"]
        for i in range(len(tweets) - 1):
            assert (tweets[i]["likes"] + tweets[i]["reposts"]) >= (tweets[i + 1]["likes"] + tweets[i + 1]["reposts"])

    def test_x_api_exit_code(self):
        """Asserts exit 0 on success."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "search", "--query", "#AI", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
