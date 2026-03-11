"""Unit tests for the memory expiry sweep scheduler integration."""

import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock third-party dependencies for importing clients.scheduler."""
    # Remove cached scheduler module so it gets reimported with our mocks
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("clients.scheduler"):
            del sys.modules[mod_name]

    if "neo4j" not in sys.modules:
        neo4j_mock = MagicMock()
        monkeypatch.setitem(sys.modules, "neo4j", neo4j_mock)

    # Mock apscheduler modules
    apscheduler_mock = MagicMock()
    apscheduler_schedulers_mock = MagicMock()
    apscheduler_schedulers_asyncio_mock = MagicMock()
    apscheduler_triggers_mock = MagicMock()
    apscheduler_triggers_cron_mock = MagicMock()

    monkeypatch.setitem(sys.modules, "apscheduler", apscheduler_mock)
    monkeypatch.setitem(sys.modules, "apscheduler.schedulers", apscheduler_schedulers_mock)
    monkeypatch.setitem(sys.modules, "apscheduler.schedulers.asyncio", apscheduler_schedulers_asyncio_mock)
    monkeypatch.setitem(sys.modules, "apscheduler.triggers", apscheduler_triggers_mock)
    monkeypatch.setitem(sys.modules, "apscheduler.triggers.cron", apscheduler_triggers_cron_mock)


class TestMemoryExpirySweepScheduler:
    """Tests for _memory_expiry_sweep in clients.scheduler."""

    @pytest.mark.asyncio
    async def test_memory_expiry_sweep_calls_endpoint(self) -> None:
        """Assert that _memory_expiry_sweep POSTs to /memory/expiry-sweep with auth."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"deleted": 1, "prompted": 0, "errors": 0})
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("clients.scheduler.aiohttp.ClientSession", return_value=mock_session), \
             patch("clients.scheduler.config") as mock_cfg:
            mock_cfg.hitl_internal_token = "test-token"

            from clients.scheduler import _memory_expiry_sweep
            await _memory_expiry_sweep()

            mock_session.post.assert_called_once()
            call_args = mock_session.post.call_args
            assert "/memory/expiry-sweep" in call_args[0][0]
            assert call_args[1]["headers"]["X-HITL-Internal"] == "test-token"

    @pytest.mark.asyncio
    async def test_memory_expiry_sweep_logs_results(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert that sweep results are logged."""
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"deleted": 2, "prompted": 1, "errors": 0})
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("clients.scheduler.aiohttp.ClientSession", return_value=mock_session), \
             patch("clients.scheduler.config") as mock_cfg, \
             caplog.at_level(logging.INFO, logger="hive-mind-scheduler"):
            mock_cfg.hitl_internal_token = "test-token"

            from clients.scheduler import _memory_expiry_sweep
            await _memory_expiry_sweep()

        assert any("deleted=2" in record.message for record in caplog.records)
        assert any("prompted=1" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_memory_expiry_sweep_handles_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Assert that sweep failure is handled gracefully."""
        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=Exception("Connection refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("clients.scheduler.aiohttp.ClientSession", return_value=mock_session), \
             patch("clients.scheduler.config") as mock_cfg, \
             caplog.at_level(logging.ERROR, logger="hive-mind-scheduler"):
            mock_cfg.hitl_internal_token = "test-token"

            from clients.scheduler import _memory_expiry_sweep
            # Should not raise
            await _memory_expiry_sweep()

        assert any("failed" in record.message.lower() for record in caplog.records)
