"""Tests for minds/cli_harness.py -- shared CLI spawn/kill logic."""

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCliHarnessModuleInterface:
    """Verify cli_harness exposes spawn and kill functions."""

    def test_cli_harness_has_spawn_function(self):
        from minds.cli_harness import cli_spawn
        assert callable(cli_spawn)
        assert asyncio.iscoroutinefunction(cli_spawn)

    def test_cli_harness_has_kill_function(self):
        from minds.cli_harness import cli_kill
        assert callable(cli_kill)
        assert asyncio.iscoroutinefunction(cli_kill)


class TestCliHarnessSpawn:
    """Verify cli_harness spawn builds correct CLI commands."""

    @pytest.mark.asyncio
    async def test_spawn_builds_claude_cli_command(self):
        from minds.cli_harness import cli_spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await cli_spawn(
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
    async def test_spawn_uses_model_from_args(self):
        from minds.cli_harness import cli_spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await cli_spawn(
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

    @pytest.mark.asyncio
    async def test_spawn_injects_env_overrides(self):
        """When registry returns a provider with env overrides, they are applied."""
        from minds.cli_harness import cli_spawn

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
            await cli_spawn(
                session_id="test-123",
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

    @pytest.mark.asyncio
    async def test_spawn_sets_group_session_env(self):
        """When is_group_session is True, HIVEMIND_GROUP_SESSION=1 is set."""
        from minds.cli_harness import cli_spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await cli_spawn(
                session_id="test-123",
                model="sonnet",
                build_base_prompt=mock_build_prompt,
                mcp_config="/tmp/mcp.json",
                registry=mock_registry,
                is_group_session=True,
            )

            call_kwargs = mock_exec.call_args[1]
            env = call_kwargs["env"]
            assert env["HIVEMIND_GROUP_SESSION"] == "1"

    @pytest.mark.asyncio
    async def test_spawn_uses_custom_logger(self):
        """When a logger is provided, it should be used for logging."""
        import logging
        from minds.cli_harness import cli_spawn

        custom_logger = logging.getLogger("hive-mind.minds.test-mind")

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch.object(custom_logger, "info") as mock_log:
                await cli_spawn(
                    session_id="test-123",
                    model="sonnet",
                    build_base_prompt=mock_build_prompt,
                    mcp_config="/tmp/mcp.json",
                    registry=mock_registry,
                    logger=custom_logger,
                )
                mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_autopilot_adds_skip_permissions_and_budget(self):
        """When autopilot is True and config_obj is set, budget flag is added."""
        from minds.cli_harness import cli_spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider
        mock_config = MagicMock()
        mock_config.autopilot_guards.max_budget_usd = 5.0

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await cli_spawn(
                session_id="test-123",
                model="sonnet",
                autopilot=True,
                config_obj=mock_config,
                build_base_prompt=mock_build_prompt,
                mcp_config="/tmp/mcp.json",
                registry=mock_registry,
            )

            call_args = mock_exec.call_args[0]
            assert "--dangerously-skip-permissions" in call_args
            assert "--max-budget-usd" in call_args
            idx = list(call_args).index("--max-budget-usd")
            assert call_args[idx + 1] == "5.0"

    @pytest.mark.asyncio
    async def test_spawn_with_resume_sid(self):
        """When resume_sid is provided, --resume flag is added."""
        from minds.cli_harness import cli_spawn

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_build_prompt = MagicMock(return_value="test prompt")
        mock_registry = MagicMock()
        mock_provider = MagicMock()
        mock_provider.env_overrides = {}
        mock_registry.get_provider.return_value = mock_provider

        with patch("minds.cli_harness.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await cli_spawn(
                session_id="test-123",
                model="sonnet",
                resume_sid="prev-session-id",
                build_base_prompt=mock_build_prompt,
                mcp_config="/tmp/mcp.json",
                registry=mock_registry,
            )

            call_args = mock_exec.call_args[0]
            assert "--resume" in call_args
            idx = list(call_args).index("--resume")
            assert call_args[idx + 1] == "prev-session-id"


class TestCliHarnessKill:
    """Verify cli_harness kill sends SIGTERM."""

    @pytest.mark.asyncio
    async def test_kill_sends_sigterm(self):
        from minds.cli_harness import cli_kill

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock()
        mock_proc.send_signal = MagicMock()

        await cli_kill(mock_proc)
        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_kill_falls_back_to_sigkill_on_timeout(self):
        from minds.cli_harness import cli_kill

        mock_proc = MagicMock()
        mock_proc.returncode = None
        # First wait() call (inside wait_for) raises TimeoutError;
        # second wait() call (after kill) succeeds.
        mock_proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError, None])
        mock_proc.send_signal = MagicMock()
        mock_proc.kill = MagicMock()

        await cli_kill(mock_proc)
        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_noop_when_proc_already_exited(self):
        from minds.cli_harness import cli_kill

        mock_proc = MagicMock()
        mock_proc.returncode = 0  # already exited

        await cli_kill(mock_proc)
        mock_proc.send_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_kill_noop_when_proc_is_none(self):
        from minds.cli_harness import cli_kill

        # Should not raise
        await cli_kill(None)

    @pytest.mark.asyncio
    async def test_kill_handles_process_lookup_error(self):
        from minds.cli_harness import cli_kill

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.send_signal = MagicMock(side_effect=ProcessLookupError)

        # Should not raise
        await cli_kill(mock_proc)
