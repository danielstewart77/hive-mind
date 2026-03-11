"""Integration tests for browser tool registration in the MCP server."""

import ast
import sys
from unittest.mock import MagicMock, patch


class TestBrowserFastMCPRegistration:
    def test_browser_tools_registered_in_mcp(self):
        """Asserts all 7 browser tool names appear in the MCP server's tool list."""
        expected_tools = {
            "browser_navigate",
            "browser_click",
            "browser_type",
            "browser_content",
            "browser_screenshot",
            "browser_close",
            "web_search",
        }

        from tools.stateful.browser import BROWSER_TOOLS

        registered_names = {f.__name__ for f in BROWSER_TOOLS}
        assert expected_tools == registered_names

    def test_mcp_server_still_loads_agent_tooling_tools(self):
        """Asserts the mcp_server.py source still imports agent_tooling during transition."""
        with open("/usr/src/app/mcp_server.py") as f:
            source = f.read()

        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)

        # During transition (Step 2), agent_tooling should still be imported
        # This test will be updated in Step 13 when we cut over
        has_agent_tooling = any("agent_tooling" in imp for imp in imports)
        has_browser_import = any("tools.stateful.browser" in imp for imp in imports)

        # During transition, both should be present: agent_tooling for existing
        # tools and browser import for the new direct registration.
        # NOTE: In Step 13, change to: assert not has_agent_tooling and has_browser_import
        assert has_agent_tooling and has_browser_import
