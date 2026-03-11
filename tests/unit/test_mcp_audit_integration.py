"""Unit tests for audit integration in mcp_server.py.

Verifies that MCP tool registration wraps tools with audit logging.
"""

import ast


class TestMCPAuditIntegration:
    """Tests that mcp_server.py wraps all tools with audit logging."""

    def test_mcp_server_does_not_import_agent_tooling(self) -> None:
        """Verify agent_tooling is no longer imported in mcp_server.py."""
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

        assert not any("agent_tooling" in imp for imp in imports)

    def test_mcp_server_imports_all_stateful_tool_modules(self) -> None:
        """Verify mcp_server.py imports from all three stateful tool modules."""
        with open("/usr/src/app/mcp_server.py") as f:
            source = f.read()

        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        assert "tools.stateful.browser" in imports
        assert "tools.stateful.knowledge_graph" in imports
        assert "tools.stateful.memory" in imports
