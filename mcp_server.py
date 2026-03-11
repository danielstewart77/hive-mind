"""
Hive Mind MCP Server.

Exposes tools via FastMCP with direct registration from tools/stateful/.
No agent_tooling dependency — all tools are imported and registered explicitly.

Tool categories:
  - Browser tools (async Playwright) from tools/stateful/browser.py
  - Knowledge graph tools (Neo4j) from tools/stateful/knowledge_graph.py
  - Memory tools (Neo4j + embeddings) from tools/stateful/memory.py

Claude Code SDK connects to this server via stdio to access external integrations.
"""

import logging

from mcp.server.fastmcp import FastMCP

from core.audit import audit_wrap, get_audit_logger
from tools.stateful.browser import BROWSER_TOOLS
from tools.stateful.knowledge_graph import KG_TOOLS
from tools.stateful.memory import MEMORY_TOOLS

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

mcp = FastMCP("hive-mind-tools")
audit_logger = get_audit_logger()

# Register all stateful tools directly
for func in BROWSER_TOOLS + KG_TOOLS + MEMORY_TOOLS:
    log.info("[MCP] %s", func.__name__)
    wrapped = audit_wrap(func, audit_logger)
    mcp.tool()(wrapped)

if __name__ == "__main__":
    log.info("Starting MCP server (stdio mode)")
    mcp.run()
