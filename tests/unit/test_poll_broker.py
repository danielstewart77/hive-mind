"""Unit tests for tools/stateless/poll_broker/poll_broker.py."""

import json
import sys
import os
from unittest.mock import patch, MagicMock

import pytest

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


def _import_poll():
    """Import the poll_broker module."""
    from tools.stateless.poll_broker import poll_broker
    return poll_broker


class TestParseArgs:
    def test_parse_args_all_required(self):
        mod = _import_poll()
        args = mod.parse_args([
            "--conversation_id", "conv-1",
            "--from_mind", "ada",
            "--to_mind", "nagatha",
            "--request_type", "quick_query",
        ])
        assert args.conversation_id == "conv-1"
        assert args.from_mind == "ada"
        assert args.to_mind == "nagatha"
        assert args.request_type == "quick_query"

    def test_parse_args_gateway_url_default(self):
        mod = _import_poll()
        args = mod.parse_args([
            "--conversation_id", "conv-1",
            "--from_mind", "ada",
            "--to_mind", "nagatha",
            "--request_type", "quick_query",
        ])
        assert args.gateway_url == "http://localhost:8420"


class TestThresholds:
    def test_threshold_lookup_returns_correct_values(self):
        mod = _import_poll()
        assert mod.get_threshold("quick_query") == 300
        assert mod.get_threshold("security_remediation") == 5400

    def test_threshold_lookup_unknown_type_returns_default(self):
        mod = _import_poll()
        assert mod.get_threshold("unknown_type") == 1200

    def test_hard_ceiling_is_4x_threshold(self):
        mod = _import_poll()
        assert mod.get_hard_ceiling("quick_query") == 1200
        assert mod.get_hard_ceiling("security_remediation") == 21600


class TestCheckForResult:
    def _mock_urlopen(self, payload):
        """Build a urllib.request.urlopen replacement that returns `payload` JSON."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        return MagicMock(return_value=mock_resp)

    def test_finds_callee_response(self):
        mod = _import_poll()
        payload = [
            {"from_mind": "ada", "to_mind": "nagatha", "status": "completed", "content": "request"},
            {"from_mind": "nagatha", "to_mind": "ada", "status": "completed", "content": "done"},
        ]
        with patch("tools.stateless.poll_broker.poll_broker.urllib.request.urlopen", self._mock_urlopen(payload)):
            result = mod.check_for_result("http://localhost:8420", "conv-1", "nagatha")

        assert result is not None
        assert result["content"] == "done"

    def test_returns_none_when_no_response(self):
        mod = _import_poll()
        payload = [
            {"from_mind": "ada", "to_mind": "nagatha", "status": "completed", "content": "request"},
        ]
        with patch("tools.stateless.poll_broker.poll_broker.urllib.request.urlopen", self._mock_urlopen(payload)):
            result = mod.check_for_result("http://localhost:8420", "conv-1", "nagatha")

        assert result is None

    def test_ignores_pending_callee_messages(self):
        mod = _import_poll()
        payload = [
            {"from_mind": "ada", "to_mind": "nagatha", "status": "completed", "content": "request"},
            {"from_mind": "nagatha", "to_mind": "ada", "status": "pending", "content": "not ready"},
        ]
        with patch("tools.stateless.poll_broker.poll_broker.urllib.request.urlopen", self._mock_urlopen(payload)):
            result = mod.check_for_result("http://localhost:8420", "conv-1", "nagatha")

        assert result is None


class TestBuildNotification:
    def test_build_notification_message(self):
        mod = _import_poll()
        msg = mod.build_notification_message(
            request_type="security_triage",
            threshold=1800,
            conversation_id="conv-1",
        )
        assert "security_triage" in msg
        assert "1800" in msg or "30" in msg  # seconds or minutes
        assert "conv-1" in msg
