"""Tests for minds/bob/implementation.py -- CLI-based spawn/kill for Ollama."""

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestBobModuleInterface:
    """Verify Bob implementation exposes required functions."""

    def test_bob_module_has_spawn_function(self):
        from minds.bob.implementation import spawn
        assert callable(spawn)
        assert asyncio.iscoroutinefunction(spawn)

    def test_bob_module_has_kill_function(self):
        from minds.bob.implementation import kill
        assert callable(kill)
        assert asyncio.iscoroutinefunction(kill)


class TestBobSpawn:
    """Verify Bob spawn builds correct CLI commands."""

    @pytest.mark.asyncio
    async def test_bob_spawn_builds_claude_cli_command(self):
        from minds.bob.implementation import spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await spawn(
                session_id="test-bob-123",
                model="gpt-oss:20b-32k",
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
    async def test_bob_spawn_uses_model_from_args(self):
        from minds.bob.implementation import spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await spawn(
                session_id="test-bob-123",
                model="gpt-oss:20b-32k",
                build_base_prompt=mock_build_prompt,
                mcp_config="/tmp/mcp.json",
                registry=mock_registry,
            )

            call_args = mock_exec.call_args[0]
            assert "--model" in call_args
            idx = list(call_args).index("--model")
            assert call_args[idx + 1] == "gpt-oss:20b-32k"

    @pytest.mark.asyncio
    async def test_bob_spawn_injects_ollama_env_vars(self):
        """When registry returns a provider with ollama env overrides, they are applied."""
        from minds.bob.implementation import spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {
            "ANTHROPIC_AUTH_TOKEN": "ollama",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_BASE_URL": "http://203.0.113.10:11434",
        }
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await spawn(
                session_id="test-bob-123",
                model="gpt-oss:20b-32k",
                build_base_prompt=mock_build_prompt,
                mcp_config="/tmp/mcp.json",
                registry=mock_registry,
            )

            call_kwargs = mock_exec.call_args[1]
            env = call_kwargs["env"]
            assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
            assert env["ANTHROPIC_API_KEY"] == ""
            assert env["ANTHROPIC_BASE_URL"] == "http://203.0.113.10:11434"


class TestBobKill:
    """Verify Bob kill sends SIGTERM."""

    @pytest.mark.asyncio
    async def test_bob_kill_sends_sigterm(self):
        from minds.bob.implementation import kill

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock()
        mock_proc.send_signal = MagicMock()

        await kill(mock_proc)
        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
