# mcp_server.py
import os

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

from agent_tooling import discover_tools, get_tool_function, get_tool_schemas
discover_tools(['agents', 'workflows', 'utilities'])
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP
mcp = FastMCP("agent-tool-server")

# Register all tools with FastMCP
for schema in get_tool_schemas():
    name = schema['name']
    func = get_tool_function(name)
    if func:
        log.info(f"[MCP REGISTER] {name}")
        mcp.tool()(func)

# Run the server
if __name__ == "__main__":
    if os.environ.get("RUN_DIRECT") == "1":
        log.info("Starting MCP server on 0.0.0.0:7777")
        mcp.run()
    else:
        log.info("Starting MCP server in stdio mode (used by mcpo)")
        mcp.run()  # uses stdin/stdout for mcpo