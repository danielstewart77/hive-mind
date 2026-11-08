"""inter-mind-api — Hive Mind nervous-system service for inter-mind messaging.

Bound to the internal Docker `hivemind` network. No host port published, no
auth. Reachable only by other services on the same network.

Wraps:
  - sync delegation: tools.stateful.inter_mind.delegate_to_mind
  - group forward:   tools.stateful.group_chat.forward_to_mind
  - broker state:    core.broker.get_registered_minds / get_messages

Run: python -m nervous_system.inter_mind_api.server
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import hive_data
import uvicorn
from fastapi import FastAPI
from hive_logging import configure_logging, install_fastapi_logging, log_event

log = configure_logging("inter-mind-api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the hive's one database before serving, and close it at the end.

    This service used to open its own SQLite connection from a module-level
    path of its own — a third writer that repointing the gateway did not
    repoint. It reads through `hive_data` like everything else now, and the
    database it reads is `HIVE_DATABASE_URL`, the same value the gateway
    reads, so there is no second path to forget.
    """
    await hive_data.connect()
    log_event(log, "service.started", component="inter-mind-api")
    try:
        yield
    finally:
        await hive_data.close(drain_timeout=5.0)
        log_event(log, "service.stopped", component="inter-mind-api")


def create_app() -> FastAPI:
    app = FastAPI(
        title="inter-mind-api",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_fastapi_logging(app, log, "inter-mind-api")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "inter-mind-api"}

    from comms.inter_mind_api.routers.messaging import router as messaging_router
    from comms.inter_mind_api.routers.state import router as state_router

    app.include_router(messaging_router)
    app.include_router(state_router)

    log_event(log, "service.routes.registered", component="inter-mind-api")
    return app


app = create_app()


def main() -> None:
    port = int(os.environ.get("INTER_MIND_API_PORT", "8425"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", log_config=None, access_log=False)


if __name__ == "__main__":
    main()
