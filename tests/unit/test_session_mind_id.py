"""Tests for mind_id support in the session manager.

Covers: schema definition, DB migration, _session_dict output,
_build_base_prompt soul_file parameterization, and create_session mind lookup.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---- Step 2: Schema includes mind_id column ----

class TestSchemaIncludesMindId:
    """Verify _SCHEMA string includes the mind_id column."""

    def test_schema_includes_mind_id_column(self):
        from core.sessions import _SCHEMA

        assert "mind_id" in _SCHEMA

    def test_schema_mind_id_has_default_ada(self):
        from core.sessions import _SCHEMA

        # Schema uses alignment spaces, so normalize whitespace for the check
        normalized = " ".join(_SCHEMA.split())
        assert "mind_id TEXT DEFAULT 'ada'" in normalized


# ---- Step 3: _session_dict includes mind_id ----

class TestSessionDictIncludesMindId:
    """Verify _session_dict output includes the mind_id key."""

    @pytest.mark.asyncio
    async def test_session_dict_includes_mind_id_ada(self):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        mock_row = {
            "id": "test-session-id",
            "claude_sid": "claude-123",
            "owner_type": "telegram",
            "owner_ref": "user-1",
            "summary": "Test session",
            "model": "sonnet",
            "autopilot": 0,
            "created_at": 1000.0,
            "last_active": 1000.0,
            "status": "running",
            "epilogue_status": None,
            "mind_id": "ada",
        }
        mgr._get_row = AsyncMock(return_value=mock_row)

        result = await mgr._session_dict("test-session-id")
        assert result is not None
        assert "mind_id" in result
        assert result["mind_id"] == "ada"

    @pytest.mark.asyncio
    async def test_session_dict_includes_mind_id_nagatha(self):
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        mock_row = {
            "id": "test-session-id",
            "claude_sid": "claude-123",
            "owner_type": "discord",
            "owner_ref": "user-2",
            "summary": "Nagatha session",
            "model": "sonnet",
            "autopilot": 0,
            "created_at": 2000.0,
            "last_active": 2000.0,
            "status": "running",
            "epilogue_status": None,
            "mind_id": "nagatha",
        }
        mgr._get_row = AsyncMock(return_value=mock_row)

        result = await mgr._session_dict("test-session-id")
        assert result is not None
        assert result["mind_id"] == "nagatha"

    @pytest.mark.asyncio
    async def test_session_dict_defaults_mind_id_when_missing(self):
        """Pre-migration rows lack mind_id; _session_dict should fall back to 'ada'."""
        from core.sessions import SessionManager
        from core.models import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        mgr = SessionManager(registry)

        # Row from old schema -- no mind_id key at all
        mock_row = {
            "id": "old-session",
            "claude_sid": None,
            "owner_type": "terminal",
            "owner_ref": "user-0",
            "summary": "Old session",
            "model": "sonnet",
            "autopilot": 0,
            "created_at": 500.0,
            "last_active": 500.0,
            "status": "idle",
            "epilogue_status": None,
        }
        mgr._get_row = AsyncMock(return_value=mock_row)

        result = await mgr._session_dict("old-session")
        assert result is not None
        assert result["mind_id"] == "ada"


# ---- Step 4: _build_base_prompt and _spawn accept soul_file ----

class TestBuildBasePromptSoulFile:
    """Verify _build_base_prompt can accept and use a custom soul_file path."""

    def test_build_base_prompt_graph_unavailable_no_soul_content(self):
        """When soul graph is unavailable, no soul content is injected. No soul file fallback."""
        from core.sessions import _build_base_prompt

        with patch("core.sessions._fetch_soul_sync", return_value=None):
            result = _build_base_prompt()

        assert "<soul>" not in result
        assert "souls/ada.md" not in result

    def test_build_base_prompt_soul_file_param_ignored_when_graph_unavailable(self):
        """soul_file parameter is accepted but never read — graph is the only identity source."""
        from core.sessions import _build_base_prompt

        custom_soul = Path("/tmp/test_custom_soul.md")
        with patch("core.sessions._fetch_soul_sync", return_value=None):
            result = _build_base_prompt(soul_file=custom_soul)

        # Soul file path must not appear in output — it is not read
        assert str(custom_soul) not in result

    def test_build_base_prompt_with_soul_graph_ignores_soul_file(self):
        """When the soul graph returns data, the soul_file path is not referenced in the main instruction."""
        from core.sessions import _build_base_prompt

        custom_soul = Path("/tmp/custom_soul.md")
        with patch("core.sessions._fetch_soul_sync", return_value="<soul>\nI am Ada\n</soul>"):
            result = _build_base_prompt(soul_file=custom_soul)

        # The soul content from the graph should be present
        assert "I am Ada" in result
        # The fallback file path should NOT be in the prompt when graph is available
        assert str(custom_soul) not in result


class TestCreateSessionMindLookup:
    """Verify create_session looks up mind config and passes soul_file to _spawn."""

    @pytest.mark.asyncio
    async def test_create_session_looks_up_mind_config(self):
        """When mind_id='ada' and config.minds has ada with a soul path, _spawn gets that path."""
        from core.sessions import SessionManager, PROJECT_DIR
        from core.models import ModelRegistry, Provider

        registry = MagicMock(spec=ModelRegistry)
        provider = MagicMock(spec=Provider)
        provider.env_overrides = {}
        registry.get_provider = MagicMock(return_value=provider)

        mgr = SessionManager(registry)

        # Set up a real in-memory aiosqlite DB
        import aiosqlite
        mgr._db = await aiosqlite.connect(":memory:")
        mgr._db.row_factory = aiosqlite.Row
        from core.sessions import _SCHEMA
        await mgr._db.executescript(_SCHEMA)
        await mgr._db.commit()

        minds_config = {"ada": {"soul": "souls/ada.md", "backend": "cli_claude", "model": "sonnet"}}

        with patch("core.sessions.config") as mock_config, \
             patch.object(mgr, "_spawn", new_callable=AsyncMock) as mock_spawn:
            mock_config.default_model = "sonnet"
            mock_config.minds = minds_config

            await mgr.create_session(
                owner_type="test",
                owner_ref="user-1",
                client_ref="client-1",
                mind_id="ada",
            )

            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args
            # soul_file should be PROJECT_DIR / "souls/ada.md"
            assert call_kwargs.kwargs.get("soul_file") == PROJECT_DIR / "souls/ada.md"

        await mgr._db.close()

    @pytest.mark.asyncio
    async def test_create_session_looks_up_bob_mind_config(self):
        """When mind_id='bob' and config.minds has bob with soul path, _spawn gets that path."""
        from core.sessions import SessionManager, PROJECT_DIR
        from core.models import ModelRegistry, Provider

        registry = MagicMock(spec=ModelRegistry)
        provider = MagicMock(spec=Provider)
        provider.env_overrides = {}
        registry.get_provider = MagicMock(return_value=provider)

        mgr = SessionManager(registry)

        import aiosqlite
        mgr._db = await aiosqlite.connect(":memory:")
        mgr._db.row_factory = aiosqlite.Row
        from core.sessions import _SCHEMA
        await mgr._db.executescript(_SCHEMA)
        await mgr._db.commit()

        minds_config = {
            "ada": {"soul": "souls/ada.md", "backend": "cli_claude", "model": "sonnet"},
            "bob": {"soul": "souls/bob.md", "backend": "cli_ollama", "model": "gpt-oss:20b-32k"},
        }

        with patch("core.sessions.config") as mock_config, \
             patch.object(mgr, "_spawn", new_callable=AsyncMock) as mock_spawn:
            mock_config.default_model = "sonnet"
            mock_config.minds = minds_config

            await mgr.create_session(
                owner_type="test",
                owner_ref="user-1",
                client_ref="client-1",
                mind_id="bob",
            )

            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args
            assert call_kwargs.kwargs.get("soul_file") == PROJECT_DIR / "souls/bob.md"

        await mgr._db.close()

    @pytest.mark.asyncio
    async def test_create_session_unknown_mind_falls_back(self):
        """When mind_id is unknown, soul_file should be None (uses default _SOUL_FILE)."""
        from core.sessions import SessionManager
        from core.models import ModelRegistry, Provider

        registry = MagicMock(spec=ModelRegistry)
        provider = MagicMock(spec=Provider)
        provider.env_overrides = {}
        registry.get_provider = MagicMock(return_value=provider)

        mgr = SessionManager(registry)

        import aiosqlite
        mgr._db = await aiosqlite.connect(":memory:")
        mgr._db.row_factory = aiosqlite.Row
        from core.sessions import _SCHEMA
        await mgr._db.executescript(_SCHEMA)
        await mgr._db.commit()

        with patch("core.sessions.config") as mock_config, \
             patch.object(mgr, "_spawn", new_callable=AsyncMock) as mock_spawn:
            mock_config.default_model = "sonnet"
            mock_config.minds = {"ada": {"soul": "souls/ada.md"}}

            await mgr.create_session(
                owner_type="test",
                owner_ref="user-1",
                client_ref="client-1",
                mind_id="unknown_mind",
            )

            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args
            # Unknown mind has no soul config, so soul_file should be None
            assert call_kwargs.kwargs.get("soul_file") is None

        await mgr._db.close()
