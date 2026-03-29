"""Integration tests for RC process lifecycle with real SQLite.

Covers: RC process cleanup on session kill with actual database operations.
"""

import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_rc_proc():
    """Create a mock RC process."""
    proc = MagicMock()
    proc.pid = 77777
    proc.returncode = None
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@pytest.fixture
def tmp_db_path(tmp_path):
    """Provide a temporary database path."""
    return str(tmp_path / "test_sessions.db")


class TestRcLifecycleFlow:
    """Integration tests for RC process lifecycle."""

    @pytest.mark.asyncio
    async def test_rc_process_cleaned_up_on_session_kill_db(self, tmp_db_path):
        """With real SQLite, creates a session, mocks an RC process into _rc_procs,
        calls kill_session, asserts RC process is gone from _rc_procs."""
        from core.models import ModelRegistry
        from core.sessions import SessionManager

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        rc_proc = _make_mock_rc_proc()

        with patch.dict(os.environ, {"SESSIONS_DB_PATH": tmp_db_path}), \
             patch.object(mgr, "_spawn", new_callable=AsyncMock), \
             patch("core.sessions.config") as mock_config:
            mock_config.idle_timeout_minutes = 30
            mock_config.autopilot_guards = MagicMock(max_minutes_without_input=30)
            mock_config.default_model = "sonnet"
            mock_config.minds = {"ada": {"soul": "souls/ada.md"}}

            await mgr.start()

            # Create a real session via the manager
            session = await mgr.create_session(
                owner_type="test",
                owner_ref="user-1",
                client_ref="client-1",
                mind_id="ada",
            )
            session_id = session["id"]

            # Inject RC process
            mgr._rc_procs[session_id] = rc_proc

            # Kill the session
            await mgr.kill_session(session_id)

            # Verify RC proc was cleaned up
            assert session_id not in mgr._rc_procs
            rc_proc.send_signal.assert_called_with(signal.SIGTERM)

            # Verify session is marked closed in DB
            session_after = await mgr.get_session(session_id)
            assert session_after["status"] == "closed"

            await mgr.shutdown()
