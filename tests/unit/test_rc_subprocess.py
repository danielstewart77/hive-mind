"""Unit tests for Remote Control subprocess spawn/kill in SessionManager.

Covers: _rc_procs dict initialization, spawn_rc_process(), kill_rc_process(),
URL parsing from stdout, edge cases (timeout, missing claude_sid, ANSI codes).
"""

import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_proc(pid: int = 99999, stdout_lines: list[bytes] | None = None):
    """Create a mock async subprocess with controllable stdout."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    if stdout_lines is not None:
        async def _readline():
            if stdout_lines:
                return stdout_lines.pop(0)
            return b""
        proc.stdout = MagicMock()
        proc.stdout.readline = _readline
    else:
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=b"")

    proc.stderr = MagicMock()
    return proc


class TestRcProcsDict:
    """SessionManager should initialise an _rc_procs dict."""

    def test_session_manager_has_rc_procs_dict(self):
        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        assert hasattr(mgr, "_rc_procs")
        assert isinstance(mgr._rc_procs, dict)
        assert len(mgr._rc_procs) == 0


class TestSpawnRcProcess:
    """spawn_rc_process() should spawn a claude --remote-control subprocess."""

    @pytest.mark.asyncio
    async def test_spawn_rc_process_calls_create_subprocess_exec(self, tmp_path):
        """Verify the subprocess is created with claude --remote-control --resume."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        mock_proc = _make_mock_proc(
            stdout_lines=[b"https://claude.ai/code/sessions/abc123\n"]
        )

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config, \
             patch("core.sessions.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            # Insert a session row with a claude_sid
            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-1', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            await mgr.spawn_rc_process("sess-1")

            # Verify create_subprocess_exec was called
            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]
            assert "claude" in call_args
            assert "--remote-control" in call_args
            assert "--resume" in call_args
            assert "claude-sid-abc" in call_args

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_spawn_rc_process_includes_name_flag(self, tmp_path):
        """Verify --name flag is passed with the mind_id's capitalised name."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        mock_proc = _make_mock_proc(
            stdout_lines=[b"https://claude.ai/code/sessions/abc123\n"]
        )

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config, \
             patch("core.sessions.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-1', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            await mgr.spawn_rc_process("sess-1")

            from core.sessions import asyncio as _aio
            call_args = _aio.create_subprocess_exec.call_args[0]
            assert "--name" in call_args
            name_idx = list(call_args).index("--name")
            assert call_args[name_idx + 1] == "Ada"

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_spawn_rc_process_parses_url_from_stdout(self, tmp_path):
        """Verify the URL is correctly extracted from stdout output."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        mock_proc = _make_mock_proc(
            stdout_lines=[
                b"Starting remote control...\n",
                b"https://claude.ai/code/sessions/xyz789\n",
            ]
        )

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config, \
             patch("core.sessions.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-1', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            result = await mgr.spawn_rc_process("sess-1")
            assert result["url"] == "https://claude.ai/code/sessions/xyz789"

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_spawn_rc_process_raises_on_missing_claude_sid(self, tmp_path):
        """Verify ValueError when session has no claude_sid."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            # Session with no claude_sid
            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-no-sid', NULL, 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            with pytest.raises(ValueError, match="claude_sid"):
                await mgr.spawn_rc_process("sess-no-sid")

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_spawn_rc_process_raises_on_url_timeout(self, tmp_path):
        """Verify RuntimeError when stdout does not produce a URL within the timeout."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        # Proc that never outputs a URL
        mock_proc = _make_mock_proc(stdout_lines=[])
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config, \
             patch("core.sessions.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-timeout', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            with pytest.raises(RuntimeError, match="URL"):
                await mgr.spawn_rc_process("sess-timeout", timeout=0.1)

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_spawn_rc_process_stores_proc_in_rc_procs(self, tmp_path):
        """Verify the spawned process is stored in _rc_procs[session_id]."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        mock_proc = _make_mock_proc(
            stdout_lines=[b"https://claude.ai/code/sessions/abc123\n"]
        )

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config, \
             patch("core.sessions.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-1', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            await mgr.spawn_rc_process("sess-1")
            assert "sess-1" in mgr._rc_procs
            assert mgr._rc_procs["sess-1"] is mock_proc

            await mgr.shutdown()


class TestKillRcProcess:
    """kill_rc_process() should terminate the RC subprocess."""

    @pytest.mark.asyncio
    async def test_kill_rc_process_sends_sigterm(self, tmp_path):
        """Verify kill_rc_process() sends SIGTERM to the RC process."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        mock_proc = _make_mock_proc()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()
            mgr._rc_procs["sess-1"] = mock_proc

            await mgr.kill_rc_process("sess-1")
            mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_kill_rc_process_removes_from_rc_procs(self, tmp_path):
        """Verify the entry is removed from _rc_procs after kill."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        mock_proc = _make_mock_proc()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()
            mgr._rc_procs["sess-1"] = mock_proc

            await mgr.kill_rc_process("sess-1")
            assert "sess-1" not in mgr._rc_procs

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_kill_rc_process_noop_when_not_running(self, tmp_path):
        """Verify no error when called on a session with no RC process."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()

            # Should not raise
            await mgr.kill_rc_process("nonexistent-session")

            await mgr.shutdown()


class TestRcUrlParsing:
    """Edge cases for URL parsing from RC subprocess stdout."""

    @pytest.mark.asyncio
    async def test_parse_rc_url_from_noisy_stdout(self, tmp_path):
        """Verify URL is extracted when stdout contains ANSI escape codes."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        # ANSI escape codes around the URL
        mock_proc = _make_mock_proc(
            stdout_lines=[
                b"\x1b[32mConnecting...\x1b[0m\n",
                b"\x1b[1mSession URL: \x1b[0mhttps://claude.ai/code/sessions/ansi-test-123\x1b[0m\n",
            ]
        )

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config, \
             patch("core.sessions.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-ansi', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            result = await mgr.spawn_rc_process("sess-ansi")
            assert result["url"] == "https://claude.ai/code/sessions/ansi-test-123"

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_parse_rc_url_rejects_non_claude_urls(self, tmp_path):
        """Verify only https://claude.ai/code/... URLs are accepted."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        # Stdout with a non-Claude URL only
        mock_proc = _make_mock_proc(
            stdout_lines=[
                b"Visit https://example.com/session/123 for details\n",
            ]
        )
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config, \
             patch("core.sessions.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-bad-url', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            with pytest.raises(RuntimeError, match="URL"):
                await mgr.spawn_rc_process("sess-bad-url", timeout=0.1)

            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_spawn_rc_kills_process_on_url_timeout(self, tmp_path):
        """Verify that if URL parsing times out, the spawned process is killed."""
        import os

        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        mock_proc = _make_mock_proc(stdout_lines=[])
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config, \
             patch("core.sessions.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"

            await mgr.start()

            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-kill', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            with pytest.raises(RuntimeError):
                await mgr.spawn_rc_process("sess-kill", timeout=0.1)

            # The orphaned process should have been killed
            mock_proc.kill.assert_called_once()

            await mgr.shutdown()
