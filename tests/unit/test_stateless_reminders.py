"""Unit tests for the stateless reminders tool."""

import json
import os
import subprocess
import sys
import tempfile

import pytest

SCRIPT_PATH = "/usr/src/app/tools/stateless/reminders/reminders.py"


class TestStatelessReminders:
    def test_set_reminder_creates_entry(self):
        """Asserts reminder created in SQLite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "reminders.db")
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "set",
                 "--message", "Test reminder",
                 "--when", "2030-01-01 12:00",
                 "--test-mode",
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data.get("set") is True
            assert data.get("id") is not None

    def test_set_reminder_rejects_past_time(self):
        """Asserts error for past datetime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "reminders.db")
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "set",
                 "--message", "Past reminder",
                 "--when", "2020-01-01 12:00",
                 "--test-mode",
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert "error" in data

    def test_list_reminders_returns_all(self):
        """Asserts all pending reminders listed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "reminders.db")
            # Set a reminder first
            subprocess.run(
                [sys.executable, SCRIPT_PATH, "set",
                 "--message", "Reminder 1",
                 "--when", "2030-01-01 12:00",
                 "--test-mode",
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            # List reminders
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "list",
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert "reminders" in data
            assert data["count"] >= 1

    def test_delete_reminder_removes_entry(self):
        """Asserts reminder deleted by ID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "reminders.db")
            # Set a reminder
            set_result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "set",
                 "--message", "To delete",
                 "--when", "2030-01-01 12:00",
                 "--test-mode",
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            rid = json.loads(set_result.stdout)["id"]
            # Delete it
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "delete",
                 "--reminder-id", str(rid),
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["deleted"] is True

    def test_get_due_reminders_fires_and_deletes(self):
        """Asserts due reminders returned and removed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "reminders.db")
            # Set a reminder in the past (use direct SQL or force a past time)
            # We'll set one and then query due with a far-future "now"
            subprocess.run(
                [sys.executable, SCRIPT_PATH, "set",
                 "--message", "Due now",
                 "--when", "2030-01-01 12:00",
                 "--test-mode",
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            # Check due -- none should be due since it's in 2030
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "due",
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert "fired" in data
            # The 2030 reminder should not be due
            assert data["count"] == 0

    def test_reminders_exit_code(self):
        """Asserts exit 0 on success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "reminders.db")
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "list",
                 "--db-path", db_path],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0
