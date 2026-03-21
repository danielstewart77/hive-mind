"""Tests for _SOUL_FILE reference in core/sessions.py."""

from unittest.mock import patch


class TestSoulFileConstant:
    """Step 4: _SOUL_FILE points to souls/ada.md."""

    def test_soul_file_constant_points_to_souls_ada(self):
        from core.sessions import _SOUL_FILE

        path_str = str(_SOUL_FILE)
        assert path_str.endswith("souls/ada.md"), f"Expected path ending with souls/ada.md, got {path_str}"

    def test_build_base_prompt_references_souls_ada_on_graph_fallback(self):
        """When graph is unavailable, the fallback prompt should reference souls/ada.md."""
        with patch("core.sessions._fetch_soul_sync", return_value=None):
            from core.sessions import _build_base_prompt

            prompt = _build_base_prompt()
        assert "souls" in prompt and "ada.md" in prompt, (
            "Fallback prompt should reference souls/ada.md"
        )
        # Should not contain bare "soul.md" without the souls/ prefix
        # The prompt uses the _SOUL_FILE path which should be souls/ada.md
        # We check that the path referenced is the new one
        assert "souls/ada.md" in prompt or "souls\\ada.md" in prompt

    def test_build_base_prompt_graph_available_mentions_soul_md_as_fallback_stub(self):
        """When graph IS available, the prompt still mentions soul.md as a fallback stub to ignore."""
        soul_block = "<soul>\nI am Ada\n</soul>"
        with patch("core.sessions._fetch_soul_sync", return_value=soul_block):
            from core.sessions import _build_base_prompt

            prompt = _build_base_prompt()
        assert "soul.md" in prompt, "Graph-available prompt should mention soul.md as fallback stub"
