"""Tests for Phase 2: parameterising Ada hardcodings in sessions.py.

Covers: _fetch_soul_sync uses mind_id, _build_base_prompt uses mind_name.
"""

from unittest.mock import MagicMock, patch



class TestFetchSoulSyncUsesMindId:
    """Verify _fetch_soul_sync respects mind_id parameter."""

    def test_fetch_soul_sync_uses_mind_id_for_graph_query(self):
        """When mind_id='nagatha', graph_query should be called with entity_name='Nagatha'."""
        mock_graph_query = MagicMock(return_value='{"found": false}')

        with patch.dict("sys.modules", {"knowledge_graph": MagicMock(graph_query=mock_graph_query)}):
            from core.sessions import _fetch_soul_sync
            _fetch_soul_sync(mind_id="nagatha")

        mock_graph_query.assert_called_once()
        call_kwargs = mock_graph_query.call_args
        # graph_query is called with keyword args
        assert call_kwargs.kwargs.get("entity_name") == "Nagatha" or \
            (len(call_kwargs.args) > 0 and call_kwargs.args[0] == "Nagatha") or \
            call_kwargs[1].get("entity_name") == "Nagatha"

    def test_fetch_soul_sync_defaults_to_ada(self):
        """Default mind_id should produce entity_name='Ada'."""
        mock_graph_query = MagicMock(return_value='{"found": false}')

        with patch.dict("sys.modules", {"knowledge_graph": MagicMock(graph_query=mock_graph_query)}):
            from core.sessions import _fetch_soul_sync
            _fetch_soul_sync()

        mock_graph_query.assert_called_once()
        call_kwargs = mock_graph_query.call_args
        assert call_kwargs.kwargs.get("entity_name") == "Ada" or \
            (len(call_kwargs.args) > 0 and call_kwargs.args[0] == "Ada") or \
            call_kwargs[1].get("entity_name") == "Ada"


class TestBuildBasePromptUsesMindName:
    """Verify _build_base_prompt uses mind_name derived from mind_id."""

    def test_build_base_prompt_uses_mind_name_in_soul_instruction(self):
        """When mind_id='nagatha', soul instruction should reference 'Nagatha node'."""
        with patch("core.sessions._fetch_soul_sync", return_value="<soul>\nTest\n</soul>"):
            from core.sessions import _build_base_prompt
            result = _build_base_prompt(mind_id="nagatha")

        assert "on the Nagatha node" in result

    def test_build_base_prompt_uses_mind_name_in_email_signature(self):
        """When mind_id='nagatha', email signature should say 'by Nagatha'."""
        with patch("core.sessions._fetch_soul_sync", return_value=None):
            from core.sessions import _build_base_prompt
            result = _build_base_prompt(mind_id="nagatha")

        assert "by Nagatha" in result
        assert "by Ada" not in result

    def test_build_base_prompt_defaults_to_ada(self):
        """Default mind_id should produce 'Ada node' and 'by Ada'."""
        with patch("core.sessions._fetch_soul_sync", return_value="<soul>\nTest\n</soul>"):
            from core.sessions import _build_base_prompt
            result = _build_base_prompt()

        assert "on the Ada node" in result
        assert "by Ada" in result
