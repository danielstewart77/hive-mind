"""Tests for soul loading in core/sessions.py.

Soul files are one-time seeds for /seed-mind only. Sessions load identity
exclusively from the knowledge graph. No soul file fallback at session start.
"""

from unittest.mock import patch


class TestSoulLoading:
    """_build_base_prompt loads soul from graph only — no soul file fallback."""

    def test_build_base_prompt_graph_available_injects_soul(self):
        """When graph is available, the soul block is injected into the prompt."""
        soul_block = "<soul>\nI am Ada\n</soul>"
        with patch("core.sessions._fetch_soul_sync", return_value=soul_block):
            from core.sessions import _build_base_prompt
            prompt = _build_base_prompt()
        assert "I am Ada" in prompt

    def test_build_base_prompt_graph_unavailable_degrades_gracefully(self):
        """When graph is unavailable, prompt is returned without soul content.
        No soul file is read as fallback."""
        with patch("core.sessions._fetch_soul_sync", return_value=None):
            from core.sessions import _build_base_prompt
            prompt = _build_base_prompt()
        # Prompt is still valid (date, instructions present)
        assert "Hive Mind" in prompt
        # No soul content injected
        assert "<soul>" not in prompt
        # No soul file path referenced
        assert "souls/ada.md" not in prompt
        assert "souls/bob.md" not in prompt
