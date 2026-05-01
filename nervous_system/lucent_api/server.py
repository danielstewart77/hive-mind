"""lucent-api — Hive Mind nervous-system service for graph + vector memory.

Bound to the internal Docker `hivemind` network. No host port published, no
auth. Reachable only by other services on the same network. Fronts the
existing tools.stateful.lucent_graph and tools.stateful.lucent_memory
modules with HTTP endpoints, removing the MCP coupling from callers.

Run: python -m nervous_system.lucent_api.server
"""

from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("lucent-api")


def create_app() -> FastAPI:
    app = FastAPI(title="lucent-api", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "lucent-api"}

    from nervous_system.lucent_api.routers.graph import router as graph_router
    from nervous_system.lucent_api.routers.memory import router as memory_router

    app.include_router(graph_router)
    app.include_router(memory_router)

    log.info("lucent-api routes registered")
    return app


app = create_app()


def main() -> None:
    port = int(os.environ.get("LUCENT_API_PORT", "8424"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
