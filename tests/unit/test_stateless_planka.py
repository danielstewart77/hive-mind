"""Unit tests for the stateless planka tool."""

import json
import subprocess
import sys


SCRIPT_PATH = "/usr/src/app/tools/stateless/planka/planka.py"


class TestStatelessPlanka:
    def test_planka_list_projects(self):
        """Asserts projects JSON returned (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "list-projects", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "projects" in data
        assert len(data["projects"]) > 0

    def test_planka_get_board(self):
        """Asserts board with lists and cards (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "get-board", "--board-id", "123", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "board" in data
        assert "lists" in data
        assert "cards" in data

    def test_planka_get_card(self):
        """Asserts card details JSON (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "get-card", "--card-id", "456", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert "card" in data

    def test_planka_move_card(self):
        """Asserts move card returns confirmation (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "move-card",
             "--card-id", "456", "--list-id", "789", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["moved"] is True

    def test_planka_create_card(self):
        """Asserts create card returns card data (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "create-card",
             "--list-id", "789", "--name", "Test Card", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["created"] is True
        assert data["name"] == "Test Card"

    def test_planka_auth_failure(self):
        """Asserts error when credentials missing (not test mode)."""
        import os
        env = os.environ.copy()
        env.pop("PLANKA_EMAIL", None)
        env.pop("PLANKA_PASSWORD", None)
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "list-projects"],
            capture_output=True, text=True, timeout=10,
            env=env,
        )
        data = json.loads(result.stdout)
        assert "error" in data

    def test_planka_exit_code(self):
        """Asserts exit 0 on success."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "list-projects", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
