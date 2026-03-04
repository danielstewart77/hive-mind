"""Unit tests for audit integration in mcp_server.py.

Verifies that MCP tool registration wraps tools with audit logging.
"""

import logging
import sys
from unittest.mock import MagicMock, patch


class TestMCPAuditIntegration:
    """Tests that mcp_server.py wraps tools with audit logging."""

    def test_mcp_wraps_tools_with_audit(self) -> None:
        """Verify that audit_wrap is called for each discovered tool during registration."""
        mock_schemas = [
            {"name": "tool_a"},
            {"name": "tool_b"},
        ]

        def fake_func_a() -> str:
            return "a"

        def fake_func_b() -> str:
            return "b"

        func_map = {"tool_a": fake_func_a, "tool_b": fake_func_b}

        # Remove mcp_server from sys.modules so it gets freshly imported
        sys.modules.pop("mcp_server", None)

        with (
            patch("agent_tooling.discover_tools"),
            patch("agent_tooling.get_tool_schemas", return_value=mock_schemas),
            patch(
                "agent_tooling.get_tool_function",
                side_effect=lambda name: func_map.get(name),
            ),
            patch("mcp.server.fastmcp.FastMCP"),
            patch("core.audit.get_audit_logger") as mock_get_logger,
            patch("core.audit.audit_wrap") as mock_audit_wrap,
        ):
            mock_audit_wrap.side_effect = lambda f, logger: f  # passthrough
            mock_get_logger.return_value = MagicMock(spec=logging.Logger)

            import mcp_server  # noqa: F401

            # audit_wrap should have been called once per tool
            assert mock_audit_wrap.call_count == 2
            call_funcs = [c[0][0] for c in mock_audit_wrap.call_args_list]
            assert fake_func_a in call_funcs
            assert fake_func_b in call_funcs
