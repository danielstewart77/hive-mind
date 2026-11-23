"""Unit tests for the stateless notify tool."""

import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path as _Path

_PROJECT_ROOT = _Path(__file__).resolve().parents[2]


SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/notify/notify.py")


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
        # Clear credential-related vars AND force null keyring so get_credential()
        # cannot fall back to keyring and accidentally send a real Telegram message.
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("TELEGRAM_OWNER_CHAT_ID", None)
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
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


# ------------------- stories 2.1.15 and 2.1.19: the claim decides whose chat

def _notify_module(monkeypatch):
    import importlib.util
    monkeypatch.syspath_prepend(str(_PROJECT_ROOT))
    spec = importlib.util.spec_from_file_location("notify_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(monkeypatch):
    monkeypatch.setenv("COMMS_URL", "http://comms.invalid")
    monkeypatch.setenv("MIND_ID", "mind-2")
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "mind-2-credential")  # secret-guard: allow
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "8776938611")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")  # secret-guard: allow


def test_a_container_minds_notification_goes_to_its_claim_not_the_configured_chat(monkeypatch):
    import httpx
    _env(monkeypatch)
    notify = _notify_module(monkeypatch)
    sent = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        return httpx.Response(200, json={"claimed": True, "claimed_ref": "5550001"},
                              request=httpx.Request("GET", url))

    def fake_post(url, json=None, timeout=None, **_):
        sent["chat_id"] = (json or {}).get("chat_id")
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    ok, _ = notify._telegram_direct("your reminder")

    assert ok is True
    assert sent["chat_id"] == "5550001"


def test_an_unclaimed_container_minds_notification_goes_to_nobody(monkeypatch):
    import httpx
    _env(monkeypatch)
    notify = _notify_module(monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"claimed": False, "claimed_ref": ""})))

    assert notify._owner_chat_id(client) == ""


def test_a_refused_claim_read_sends_a_container_minds_notification_to_nobody(monkeypatch):
    import httpx
    _env(monkeypatch)
    notify = _notify_module(monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(404, json={"detail": "No such claim"})))

    assert notify._owner_chat_id(client) == ""


class _ChildDone(BaseException):
    """Raised where the forked child would exit, so the test regains control."""


def test_a_sent_report_asked_to_be_spoken_is_followed_by_a_voice_note_to_the_same_chat(
        monkeypatch, capsys):
    """Daniel, 2026-11-23: the chief of staff's reports after a wake reached
    Telegram as text with no voice. `send --voice` says the text, then
    speaks it, to the person the text went to."""
    import argparse
    import httpx
    _env(monkeypatch)
    notify = _notify_module(monkeypatch)
    posts = []

    def fake_get(url, params=None, headers=None, timeout=None):
        return httpx.Response(200, json={"claimed": True, "claimed_ref": "5550001"},
                              request=httpx.Request("GET", url))

    def fake_post(url, json=None, data=None, files=None, timeout=None, **_):
        posts.append({"url": url, "json": json, "data": data, "files": files})
        return httpx.Response(200, content=b"SPOKEN", request=httpx.Request("POST", url))

    def child_exit(code):
        raise _ChildDone()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    # The process boundary: run the child's half in this process.
    monkeypatch.setattr(notify.os, "fork", lambda: 0)
    monkeypatch.setattr(notify.os, "setsid", lambda: None)
    monkeypatch.setattr(notify.os, "_exit", child_exit)

    args = argparse.Namespace(message="Mordecai has finished the plan.", channels="telegram",
                              alert_file=None, test_mode=False, voice=True)
    try:
        notify.cmd_send(args)
    except _ChildDone:
        pass

    text, speech, note = posts
    assert text["url"].endswith("/sendMessage")
    assert speech["url"].endswith("/tts")
    assert speech["json"]["text"] == "Mordecai has finished the plan."
    assert note["url"].endswith("/sendVoice")
    assert note["data"] == {"chat_id": "5550001"}
    assert note["files"]["voice"][1] == b"SPOKEN"
