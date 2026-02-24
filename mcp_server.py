"""
Hive Mind MCP Server.

Exposes all @tool() decorated functions from agents/ as MCP resources.
Claude Code SDK connects to this server via stdio to access external integrations.
"""

import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

from agent_tooling import discover_tools, get_tool_function, get_tool_schemas
from mcp.server.fastmcp import FastMCP

# Discover tools from agents/ only (workflows/utilities removed)
discover_tools(["agents"])

mcp = FastMCP("hive-mind-tools")

for schema in get_tool_schemas():
    name = schema["name"]
    func = get_tool_function(name)
    if func:
        log.info(f"[MCP] {name}")
        mcp.tool()(func)

if __name__ == "__main__":
    log.info("Starting MCP server (stdio mode)")
    mcp.run()
