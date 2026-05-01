"""Integration test: delegate_to_mind registered in MCP server."""


class TestDelegateToMindRegistration:
    """Verify delegate_to_mind is registered as an MCP tool."""

    def test_delegate_to_mind_registered_in_mcp(self):
        from nervous_system.inter_mind_api.inter_mind import INTER_MIND_TOOLS

        tool_names = [f.__name__ for f in INTER_MIND_TOOLS]
        assert "delegate_to_mind" in tool_names
