"""
Hive Mind MCP Server.

Exposes tools via FastMCP. Browser tools are registered directly from
tools/stateful/browser.py (async Playwright). Other tools still use
agent_tooling discovery during the migration transition.

Claude Code SDK connects to this server via stdio to access external integrations.
"""

import logging

from agent_tooling import discover_tools, get_tool_function, get_tool_schemas
from mcp.server.fastmcp import FastMCP

from core.audit import audit_wrap, get_audit_logger
from tools.stateful.browser import BROWSER_TOOLS

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# Discover tools from agents/ (excluding browser tools which are now direct)
discover_tools(["agents"])

mcp = FastMCP("hive-mind-tools")
audit_logger = get_audit_logger()

# Browser tool names to exclude from agent_tooling registration
_BROWSER_TOOL_NAMES = {f.__name__ for f in BROWSER_TOOLS}

# Register browser tools directly (async Playwright, no @tool decorator)
for func in BROWSER_TOOLS:
    log.info("[MCP] %s (direct)", func.__name__)
    wrapped = audit_wrap(func, audit_logger)
    mcp.tool()(wrapped)

# Register remaining agent_tooling tools (excludes browser to avoid duplicates)
for schema in get_tool_schemas():
    name = schema["name"]
    if name in _BROWSER_TOOL_NAMES:
        continue  # Already registered directly above
    func = get_tool_function(name)
    if func:
        log.info("[MCP] %s", name)
        func = audit_wrap(func, audit_logger)
        mcp.tool()(func)

if __name__ == "__main__":
    log.info("Starting MCP server (stdio mode)")
    mcp.run()
