"""Unit tests for core/audit.py — audit logging infrastructure."""

import json
import logging
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from core.audit import (
    JSONAuditFormatter,
    audit_wrap,
    get_audit_logger,
    redact_args,
)


class TestRedactArgs:
    """Tests for the redact_args function."""

    def test_redact_secrets_redacts_value_param(self) -> None:
        args = {"content": "hello", "value": "my-secret-password"}
        result = redact_args(args)
        assert result["value"] == "***REDACTED***"
        assert result["content"] == "hello"

    def test_redact_secrets_redacts_password_param(self) -> None:
        args = {"username": "admin", "password": "hunter2"}
        result = redact_args(args)
        assert result["password"] == "***REDACTED***"
        assert result["username"] == "admin"

    def test_redact_secrets_redacts_token_param(self) -> None:
        args = {"token": "abc123xyz"}
        result = redact_args(args)
        assert result["token"] == "***REDACTED***"

    def test_redact_secrets_redacts_secret_param(self) -> None:
        args = {"secret": "top-secret-value"}
        result = redact_args(args)
        assert result["secret"] == "***REDACTED***"

    def test_redact_secrets_redacts_auth_param(self) -> None:
        args = {"auth": "bearer xyz"}
        result = redact_args(args)
        assert result["auth"] == "***REDACTED***"

    def test_redact_secrets_truncates_long_code(self) -> None:
        long_code = "x" * 500
        args = {"code": long_code}
        result = redact_args(args)
        assert result["code"] == f"[{len(long_code)} chars]"

    def test_redact_secrets_preserves_short_code(self) -> None:
        short_code = "x" * 50
        args = {"code": short_code}
        result = redact_args(args)
        assert result["code"] == short_code

    def test_redact_secrets_preserves_safe_params(self) -> None:
        args = {"query": "SELECT *", "board_id": "123", "tags": "session"}
        result = redact_args(args)
        assert result == args

    def test_redact_does_not_mutate_original(self) -> None:
        original = {"value": "secret", "name": "test"}
        redact_args(original)
        assert original["value"] == "secret"

    def test_redact_handles_non_string_values(self) -> None:
        args = {"count": 42, "flag": True, "items": [1, 2, 3]}
        result = redact_args(args)
        assert result == args

    def test_redact_handles_empty_dict(self) -> None:
        assert redact_args({}) == {}


class TestJSONAuditFormatter:
    """Tests for the JSON log formatter."""

    def test_json_formatter_produces_valid_json(self) -> None:
        formatter = JSONAuditFormatter()
        record = logging.LogRecord(
            name="hive_mind.audit",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test message",
            args=None,
            exc_info=None,
        )
        record.audit_data = {
            "event": "tool_call",
            "tool": "test_tool",
            "args": {"query": "hello"},
            "status": "success",
            "duration_ms": 42,
            "error": None,
        }
        output = formatter.format(record)
        data = json.loads(output)
        assert data["event"] == "tool_call"
        assert data["tool"] == "test_tool"
        assert data["status"] == "success"
        assert data["duration_ms"] == 42
        assert "timestamp" in data

    def test_json_formatter_handles_record_without_audit_data(self) -> None:
        formatter = JSONAuditFormatter()
        record = logging.LogRecord(
            name="hive_mind.audit",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="plain message",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["message"] == "plain message"
        assert "timestamp" in data


class TestGetAuditLogger:
    """Tests for the audit logger factory."""

    def test_get_audit_logger_returns_logger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test_audit.log")
            logger = get_audit_logger(log_path)
            assert isinstance(logger, logging.Logger)
            assert logger.name == "hive_mind.audit"

    def test_get_audit_logger_has_rotating_handler(self) -> None:
        from logging.handlers import RotatingFileHandler

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test_audit.log")
            logger = get_audit_logger(log_path)
            rotating_handlers = [
                h for h in logger.handlers if isinstance(h, RotatingFileHandler)
            ]
            assert len(rotating_handlers) >= 1

    def test_get_audit_logger_uses_json_formatter(self) -> None:
        from logging.handlers import RotatingFileHandler

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "test_audit.log")
            logger = get_audit_logger(log_path)
            rotating_handlers = [
                h for h in logger.handlers if isinstance(h, RotatingFileHandler)
            ]
            assert isinstance(rotating_handlers[0].formatter, JSONAuditFormatter)

    def test_get_audit_logger_default_path(self) -> None:
        """Test that get_audit_logger works with default path."""
        logger = get_audit_logger()
        assert isinstance(logger, logging.Logger)


class TestAuditWrap:
    """Tests for the audit_wrap decorator."""

    def test_audit_wrap_logs_success(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)

        def my_tool(query: str) -> str:
            return "result"

        wrapped = audit_wrap(my_tool, mock_logger)
        result = wrapped(query="hello")

        assert result == "result"
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        # The info call should pass audit_data as extra
        extra = call_args[1].get("extra", {})
        audit_data = extra.get("audit_data", {})
        assert audit_data["tool"] == "my_tool"
        assert audit_data["status"] == "success"
        assert audit_data["error"] is None
        assert "duration_ms" in audit_data

    def test_audit_wrap_logs_error(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)

        def failing_tool(query: str) -> str:
            raise ValueError("something broke")

        wrapped = audit_wrap(failing_tool, mock_logger)

        with pytest.raises(ValueError, match="something broke"):
            wrapped(query="hello")

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        extra = call_args[1].get("extra", {})
        audit_data = extra.get("audit_data", {})
        assert audit_data["status"] == "error"
        assert "something broke" in audit_data["error"]

    def test_audit_wrap_preserves_return_value(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)

        def add_tool(a: int, b: int) -> int:
            return a + b

        wrapped = audit_wrap(add_tool, mock_logger)
        assert wrapped(a=3, b=4) == 7

    def test_audit_wrap_preserves_exception(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)

        def boom() -> None:
            raise RuntimeError("kaboom")

        wrapped = audit_wrap(boom, mock_logger)

        with pytest.raises(RuntimeError, match="kaboom"):
            wrapped()

    def test_audit_wrap_redacts_sensitive_args(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)

        def store_secret(name: str, value: str) -> str:
            return "stored"

        wrapped = audit_wrap(store_secret, mock_logger)
        wrapped(name="api_key", value="super-secret")

        call_args = mock_logger.info.call_args
        extra = call_args[1].get("extra", {})
        audit_data = extra.get("audit_data", {})
        assert audit_data["args"]["value"] == "***REDACTED***"
        assert audit_data["args"]["name"] == "api_key"

    def test_audit_wrap_preserves_function_name(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)

        def my_special_tool() -> str:
            """A special tool."""
            return "special"

        wrapped = audit_wrap(my_special_tool, mock_logger)
        assert wrapped.__name__ == "my_special_tool"
        assert wrapped.__doc__ == "A special tool."

    def test_audit_wrap_handles_positional_args(self) -> None:
        mock_logger = MagicMock(spec=logging.Logger)

        def greet(name: str, greeting: str = "hello") -> str:
            return f"{greeting} {name}"

        wrapped = audit_wrap(greet, mock_logger)
        result = wrapped("world")
        assert result == "hello world"
