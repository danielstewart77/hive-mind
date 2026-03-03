"""Tests for idle reaper behavior after epilogue system replacement."""

import inspect

import pytest


class TestIdleReaperNoOldEpilogue:
    """Verify old epilogue methods are removed."""

    def test_idle_reaper_does_not_call_old_epilogue(self):
        """Assert _run_epilogue is no longer a method on SessionManager."""
        from core.sessions import SessionManager
        assert not hasattr(SessionManager, "_run_epilogue"), (
            "_run_epilogue should have been removed from SessionManager"
        )

    def test_store_memory_sync_removed(self):
        """Assert _store_memory_sync is no longer in core.sessions module."""
        from core import sessions
        assert not hasattr(sessions, "_store_memory_sync"), (
            "_store_memory_sync should have been removed from core.sessions"
        )

    def test_reaped_sessions_get_null_epilogue_status(self):
        """Verify the idle reaper sets status to 'idle' without modifying epilogue_status.

        The reaper only sets status='idle', leaving epilogue_status as NULL so
        triggers A/B can process it later. We verify no SQL UPDATE sets epilogue_status.
        """
        from core.sessions import SessionManager
        source = inspect.getsource(SessionManager._idle_reaper)
        # Should NOT contain _run_epilogue or _store_memory_sync
        assert "_run_epilogue" not in source
        assert "_store_memory_sync" not in source
        # Should set status to idle
        assert "status = 'idle'" in source
        # The SQL UPDATE should NOT modify epilogue_status (only mentioned in docstring is OK)
        # Check that no UPDATE query contains epilogue_status
        import re
        update_statements = re.findall(r'UPDATE\s+sessions\s+SET\s+[^"]+', source)
        for stmt in update_statements:
            assert "epilogue_status" not in stmt, (
                f"idle reaper UPDATE should not modify epilogue_status: {stmt}"
            )
