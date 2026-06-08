"""Unit tests for the global error handler in telegram_bot."""

import logging

import pytest
from unittest.mock import MagicMock
from telegram.error import NetworkError, TimedOut

from bots.telegram_bot import _on_error


@pytest.mark.asyncio
async def test_on_error_swallows_network_error_at_info(caplog):
    ctx = MagicMock()
    ctx.error = NetworkError("Bad Gateway")
    with caplog.at_level(logging.INFO, logger="hive-mind-telegram"):
        await _on_error(MagicMock(), ctx)
    assert any("transient telegram network error" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_on_error_swallows_timed_out_at_info(caplog):
    ctx = MagicMock()
    ctx.error = TimedOut("read timed out")
    with caplog.at_level(logging.INFO, logger="hive-mind-telegram"):
        await _on_error(MagicMock(), ctx)
    assert any("transient telegram network error" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_on_error_logs_other_exceptions_at_error(caplog):
    ctx = MagicMock()
    ctx.error = RuntimeError("boom")
    with caplog.at_level(logging.ERROR, logger="hive-mind-telegram"):
        await _on_error(MagicMock(), ctx)
    assert any("unhandled exception" in r.message for r in caplog.records)
