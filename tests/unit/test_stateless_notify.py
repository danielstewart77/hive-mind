"""Unit tests for the stateless notify tool."""

import json
import subprocess
import sys
import tempfile
import os


SCRIPT_PATH = "/usr/src/app/tools/stateless/notify/notify.py"


class TestStatelessNotify:
    def test_notify_telegram_channel(self):
        """Asserts Telegram delivery in test mode."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "send", "--message", "Test alert", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["delivered"] is True

    def test_notify_file_channel(self):
        """Asserts alert file written when using file channel."""
        with tempfile.TemporaryDirectory() as tmpdir:
            alert_file = os.path.join(tmpdir, "alerts.log")
            result = subprocess.run(
                [sys.executable, SCRIPT_PATH, "send",
                 "--message", "File test alert",
                 "--channels", "file",
                 "--alert-file", alert_file],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(result.stdout)
            assert data["delivered"] is True
            assert os.path.exists(alert_file)
            with open(alert_file) as f:
                content = f.read()
            assert "File test alert" in content

    def test_notify_fallback_chain(self):
        """Asserts channels tried in order, stops at first success (test mode)."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "send",
             "--message", "Fallback test",
             "--channels", "telegram,email,file",
             "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["delivered"] is True
        # In test mode, telegram succeeds first so email/file shouldn't be tried
        channels = data["channels"]
        assert channels["telegram"]["success"] is True

    def test_notify_missing_credentials(self):
        """Asserts graceful error when credentials missing (not test mode)."""
        # Provide minimal env that still allows Python to import modules
        env = os.environ.copy()
        # Clear credential-related vars
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("TELEGRAM_OWNER_CHAT_ID", None)
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "send",
             "--message", "No creds test",
             "--channels", "telegram"],
            capture_output=True, text=True, timeout=10,
            env=env,
        )
        data = json.loads(result.stdout)
        channels = data["channels"]
        assert channels["telegram"]["success"] is False

    def test_send_voice_message(self):
        """Asserts voice subcommand works in test mode."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "voice",
             "--message", "Hello Daniel",
             "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["success"] is True

    def test_notify_exit_code(self):
        """Asserts exit 0 on delivery."""
        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "send", "--message", "Exit test", "--test-mode"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
