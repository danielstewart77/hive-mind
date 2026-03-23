"""Tests for minds/ada/implementation.py — CLI-based spawn/kill."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAdaModuleInterface:
    """Verify Ada implementation exposes required functions."""

    def test_ada_module_has_spawn_function(self):
        from minds.ada.implementation import spawn
        assert callable(spawn)
        assert asyncio.iscoroutinefunction(spawn)

    def test_ada_module_has_kill_function(self):
        from minds.ada.implementation import kill
        assert callable(kill)
        assert asyncio.iscoroutinefunction(kill)


class TestAdaSpawn:
    """Verify Ada spawn builds correct CLI commands."""

    @pytest.mark.asyncio
    async def test_ada_spawn_builds_claude_cli_command(self):
        from minds.ada.implementation import spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.ada.implementation.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await spawn(
                session_id="test-123",
                model="sonnet",
                build_base_prompt=mock_build_prompt,
                mcp_config="/tmp/mcp.json",
                registry=mock_registry,
            )

            call_args = mock_exec.call_args[0]
            assert "claude" in call_args
            assert "-p" in call_args
            assert "--input-format" in call_args
            assert "stream-json" in call_args
            assert "--output-format" in call_args

    @pytest.mark.asyncio
    async def test_ada_spawn_uses_model_from_args(self):
        from minds.ada.implementation import spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.ada.implementation.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await spawn(
                session_id="test-123",
                model="opus",
                build_base_prompt=mock_build_prompt,
                mcp_config="/tmp/mcp.json",
                registry=mock_registry,
            )

            call_args = mock_exec.call_args[0]
            assert "--model" in call_args
            idx = list(call_args).index("--model")
            assert call_args[idx + 1] == "opus"


class TestAdaKill:
    """Verify Ada kill sends SIGTERM."""

    @pytest.mark.asyncio
    async def test_ada_kill_sends_sigterm(self):
        import signal
        from minds.ada.implementation import kill

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock()
        mock_proc.send_signal = MagicMock()

        await kill(mock_proc)
        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
