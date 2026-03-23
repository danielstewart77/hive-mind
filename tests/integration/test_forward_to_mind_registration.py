"""Integration test: forward_to_mind registered in MCP server."""


class TestForwardToMindRegistration:
    """Verify forward_to_mind is registered as an MCP tool."""

    def test_forward_to_mind_registered_in_mcp(self):
        from tools.stateful.group_chat import GROUP_CHAT_TOOLS

        tool_names = [f.__name__ for f in GROUP_CHAT_TOOLS]
        assert "forward_to_mind" in tool_names
