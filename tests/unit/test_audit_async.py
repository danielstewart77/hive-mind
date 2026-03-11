"""Unit tests for async support in audit_wrap (core/audit.py)."""

import asyncio
import inspect
import logging
from unittest.mock import MagicMock

import pytest

from core.audit import audit_wrap, get_audit_logger


class TestAuditWrapAsync:
    """Tests that audit_wrap correctly handles async functions."""

    def _make_logger(self) -> logging.Logger:
        """Create a logger with a mock handler for testing."""
        logger = logging.getLogger("test.audit.async")
        logger.handlers.clear()
        handler = MagicMock(spec=logging.Handler)
        handler.level = logging.DEBUG
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger

    def test_async_function_produces_async_wrapper(self):
        """Asserts audit_wrap returns a coroutine function when given an async function."""
        async def my_async_func(x: int) -> str:
            return f"result-{x}"

        logger = self._make_logger()
        wrapped = audit_wrap(my_async_func, logger)
        assert inspect.iscoroutinefunction(wrapped), (
            "audit_wrap must return a coroutine function for async inputs"
        )

    def test_sync_function_produces_sync_wrapper(self):
        """Asserts audit_wrap returns a regular function when given a sync function."""
        def my_sync_func(x: int) -> str:
            return f"result-{x}"

        logger = self._make_logger()
        wrapped = audit_wrap(my_sync_func, logger)
        assert not inspect.iscoroutinefunction(wrapped), (
            "audit_wrap must return a regular function for sync inputs"
        )

    @pytest.mark.asyncio
    async def test_async_wrapper_returns_awaited_result(self):
        """Asserts the async wrapper awaits the inner function and returns its result."""
        async def my_async_func(x: int) -> str:
            return f"result-{x}"

        logger = self._make_logger()
        wrapped = audit_wrap(my_async_func, logger)
        result = await wrapped(42)
        assert result == "result-42"

    @pytest.mark.asyncio
    async def test_async_wrapper_propagates_exception(self):
        """Asserts the async wrapper re-raises exceptions from the async function."""
        async def failing_async_func() -> str:
            raise ValueError("async boom")

        logger = self._make_logger()
        wrapped = audit_wrap(failing_async_func, logger)
        with pytest.raises(ValueError, match="async boom"):
            await wrapped()

    @pytest.mark.asyncio
    async def test_async_wrapper_logs_audit_data(self):
        """Asserts the async wrapper logs audit data with correct tool name and status."""
        async def my_async_func(url: str) -> str:
            return f"visited {url}"

        logger = self._make_logger()
        wrapped = audit_wrap(my_async_func, logger)
        await wrapped("https://example.com")

        # Verify logger.info was called with audit_data
        handler = logger.handlers[0]
        assert handler.handle.called
        log_record = handler.handle.call_args[0][0]
        audit_data = log_record.audit_data
        assert audit_data["tool"] == "my_async_func"
        assert audit_data["status"] == "success"
        assert "duration_ms" in audit_data

    @pytest.mark.asyncio
    async def test_async_wrapper_logs_error_on_exception(self):
        """Asserts the async wrapper logs error status when exception occurs."""
        async def failing_func() -> str:
            raise RuntimeError("test error")

        logger = self._make_logger()
        wrapped = audit_wrap(failing_func, logger)

        with pytest.raises(RuntimeError):
            await wrapped()

        handler = logger.handlers[0]
        log_record = handler.handle.call_args[0][0]
        audit_data = log_record.audit_data
        assert audit_data["status"] == "error"
        assert audit_data["error"] == "test error"

    def test_async_wrapper_preserves_function_name(self):
        """Asserts functools.wraps preserves __name__ on async wrapper."""
        async def browser_navigate(url: str) -> str:
            return url

        logger = self._make_logger()
        wrapped = audit_wrap(browser_navigate, logger)
        assert wrapped.__name__ == "browser_navigate"

    @pytest.mark.asyncio
    async def test_async_wrapper_redacts_sensitive_params(self):
        """Asserts sensitive parameter values are redacted in audit logs."""
        async def set_secret(key: str, value: str) -> str:
            return "ok"

        logger = self._make_logger()
        wrapped = audit_wrap(set_secret, logger)
        await wrapped("MY_KEY", "super-secret-123")

        handler = logger.handlers[0]
        log_record = handler.handle.call_args[0][0]
        audit_data = log_record.audit_data
        assert audit_data["args"]["value"] == "***REDACTED***"
        assert audit_data["args"]["key"] == "MY_KEY"

    def test_sync_wrapper_still_works(self):
        """Asserts existing sync behavior is not broken."""
        def my_sync_func(x: int) -> str:
            return f"sync-{x}"

        logger = self._make_logger()
        wrapped = audit_wrap(my_sync_func, logger)
        result = wrapped(5)
        assert result == "sync-5"
