"""Unit tests for Remote Control lifecycle management.

Covers: RC cleanup on _kill_process(), kill_session(), and shutdown().
"""

import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_rc_proc():
    """Create a mock RC process."""
    proc = MagicMock()
    proc.pid = 88888
    proc.returncode = None
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestKillProcessCleansUpRc:
    """_kill_process() should also kill RC processes for the same session."""

    @pytest.mark.asyncio
    async def test_kill_process_also_kills_rc_process(self, tmp_path):
        """When _kill_process(session_id) is called and an RC process exists,
        the RC process should also be terminated."""
        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        rc_proc = _make_mock_rc_proc()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()

            # Simulate an RC process exists for this session
            mgr._rc_procs["sess-1"] = rc_proc

            # Call _kill_process (which kills main + RC)
            await mgr._kill_process("sess-1")

            # RC process should have been killed
            rc_proc.send_signal.assert_called_once_with(signal.SIGTERM)
            assert "sess-1" not in mgr._rc_procs

            await mgr.shutdown()


class TestKillSessionCleansUpRc:
    """kill_session() should clean up RC processes."""

    @pytest.mark.asyncio
    async def test_kill_session_cleans_up_rc_process(self, tmp_path):
        """kill_session() should result in RC process cleanup via _kill_process."""
        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        rc_proc = _make_mock_rc_proc()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"
            mock_config.minds = {"ada": {"soul": "souls/ada.md"}}

            await mgr.start()

            # Create a session in DB
            await mgr._db.execute(
                """INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
                   VALUES ('sess-1', 'claude-sid-abc', 'test', 'user-1', 'sonnet', 100.0, 100.0, 'running', 'ada')"""
            )
            await mgr._db.commit()

            # Simulate an RC process
            mgr._rc_procs["sess-1"] = rc_proc

            await mgr.kill_session("sess-1")

            # RC process should be cleaned up
            rc_proc.send_signal.assert_called_with(signal.SIGTERM)
            assert "sess-1" not in mgr._rc_procs

            await mgr.shutdown()


class TestShutdownKillsRc:
    """shutdown() should kill all tracked RC processes."""

    @pytest.mark.asyncio
    async def test_shutdown_kills_all_rc_processes(self, tmp_path):
        """shutdown() should kill all RC subprocesses."""
        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        rc_proc_1 = _make_mock_rc_proc()
        rc_proc_2 = _make_mock_rc_proc()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()

            # Register main processes so _kill_process is called during shutdown,
            # which in turn calls kill_rc_process for each session.
            main_proc_1 = _make_mock_rc_proc()
            main_proc_2 = _make_mock_rc_proc()
            mgr._procs["sess-1"] = main_proc_1
            mgr._procs["sess-2"] = main_proc_2
            mgr._mind_ids["sess-1"] = "ada"
            mgr._mind_ids["sess-2"] = "ada"

            mgr._rc_procs["sess-1"] = rc_proc_1
            mgr._rc_procs["sess-2"] = rc_proc_2

            mock_impl = MagicMock()
            del mock_impl.kill  # Simulate no kill method so _kill_process skips it
            with patch("core.sessions._load_implementation", return_value=mock_impl):
                await mgr.shutdown()

            # Both RC processes should have been killed via _kill_process -> kill_rc_process
            rc_proc_1.send_signal.assert_called_with(signal.SIGTERM)
            rc_proc_2.send_signal.assert_called_with(signal.SIGTERM)
            assert len(mgr._rc_procs) == 0

    @pytest.mark.asyncio
    async def test_shutdown_kills_rc_processes_without_main_proc(self, tmp_path):
        """shutdown() should kill orphaned RC processes whose session_ids
        are NOT in _procs (e.g., main process already exited or was never started)."""
        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)
        db_path = str(tmp_path / "test.db")

        orphan_rc_proc = _make_mock_rc_proc()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": db_path}), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)

            await mgr.start()

            # Register an RC process with NO corresponding main process in _procs
            mgr._rc_procs["orphan-sess"] = orphan_rc_proc
            assert "orphan-sess" not in mgr._procs

            await mgr.shutdown()

            # The orphaned RC process should still have been killed
            orphan_rc_proc.send_signal.assert_called_once_with(signal.SIGTERM)
            assert "orphan-sess" not in mgr._rc_procs
