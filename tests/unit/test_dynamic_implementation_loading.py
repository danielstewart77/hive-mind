"""Tests for dynamic mind implementation loading in core/sessions.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestLoadImplementation:
    """Verify _load_implementation loads correct modules."""

    def test_load_implementation_returns_ada_module(self):
        from core.sessions import _load_implementation, _implementation_cache
        _implementation_cache.clear()

        mod = _load_implementation("ada")
        assert hasattr(mod, "spawn")
        assert hasattr(mod, "kill")
        _implementation_cache.clear()

    def test_load_implementation_returns_nagatha_module(self):
        from core.sessions import _load_implementation, _implementation_cache
        _implementation_cache.clear()

        mod = _load_implementation("nagatha")
        assert hasattr(mod, "spawn")
        assert hasattr(mod, "send")
        assert hasattr(mod, "kill")
        _implementation_cache.clear()

    def test_load_implementation_returns_bob_module(self):
        from core.sessions import _load_implementation, _implementation_cache
        _implementation_cache.clear()

        mod = _load_implementation("bob")
        assert hasattr(mod, "spawn")
        assert hasattr(mod, "kill")
        _implementation_cache.clear()

    def test_load_implementation_unknown_mind_falls_back_to_ada(self):
        from core.sessions import _load_implementation, _implementation_cache
        _implementation_cache.clear()

        mod = _load_implementation("nonexistent")
        # Should fall back to ada's module
        import minds.ada.implementation as ada_impl
        assert mod is ada_impl
        _implementation_cache.clear()

    def test_load_implementation_caches_result(self):
        from core.sessions import _load_implementation, _implementation_cache
        _implementation_cache.clear()

        mod1 = _load_implementation("ada")
        mod2 = _load_implementation("ada")
        assert mod1 is mod2
        _implementation_cache.clear()


class TestSpawnDelegatesToImplementation:
    """Verify _spawn delegates to the loaded implementation module."""

    @pytest.mark.asyncio
    async def test_spawn_delegates_to_implementation_module(self):
        from core.sessions import SessionManager, _implementation_cache
        from core.models import ModelRegistry

        _implementation_cache.clear()

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        mock_proc = AsyncMock()
        mock_proc.pid = 99999

        mock_impl = MagicMock()
        mock_impl.spawn = AsyncMock(return_value=mock_proc)

        with patch("core.sessions._load_implementation", return_value=mock_impl):
            result = await mgr._spawn("test-sess", "sonnet", mind_id="ada")

        mock_impl.spawn.assert_called_once()
        assert result is mock_proc
        assert mgr._procs["test-sess"] is mock_proc
        assert mgr._mind_ids["test-sess"] == "ada"

        _implementation_cache.clear()

    @pytest.mark.asyncio
    async def test_kill_delegates_to_implementation_module(self):
        from core.sessions import SessionManager, _implementation_cache
        from core.models import ModelRegistry

        _implementation_cache.clear()

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        mock_proc = MagicMock()
        mock_proc.returncode = None

        mgr._procs["test-kill"] = mock_proc
        mgr._mind_ids["test-kill"] = "ada"

        mock_impl = MagicMock()
        mock_impl.kill = AsyncMock()

        with patch("core.sessions._load_implementation", return_value=mock_impl):
            await mgr._kill_process("test-kill")

        mock_impl.kill.assert_called_once()
        assert "test-kill" not in mgr._procs
        assert "test-kill" not in mgr._mind_ids

        _implementation_cache.clear()
