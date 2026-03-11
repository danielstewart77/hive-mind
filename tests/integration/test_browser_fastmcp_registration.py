"""Integration tests for browser tool registration in the MCP server."""

import ast


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

    def test_mcp_server_no_agent_tooling(self):
        """Asserts mcp_server.py no longer imports agent_tooling after migration."""
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

        has_agent_tooling = any("agent_tooling" in imp for imp in imports)
        has_browser_import = any("tools.stateful.browser" in imp for imp in imports)

        assert not has_agent_tooling and has_browser_import
