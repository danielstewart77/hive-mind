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

import logging
import os
from contextlib import asynccontextmanager

import aiosqlite
import uvicorn
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("inter-mind-api")

BROKER_DB_PATH = os.environ.get("BROKER_DB_PATH", "/usr/src/app/data/broker.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await aiosqlite.connect(BROKER_DB_PATH)
    db.row_factory = aiosqlite.Row
    app.state.broker_db = db
    log.info("inter-mind-api connected to broker db at %s", BROKER_DB_PATH)
    try:
        yield
    finally:
        await db.close()
        log.info("inter-mind-api closed broker db")


def create_app() -> FastAPI:
    app = FastAPI(title="inter-mind-api", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "inter-mind-api"}

    from nervous_system.inter_mind_api.routers.messaging import router as messaging_router
    from nervous_system.inter_mind_api.routers.state import router as state_router

    app.include_router(messaging_router)
    app.include_router(state_router)

    log.info("inter-mind-api routes registered")
    return app


app = create_app()


def main() -> None:
    port = int(os.environ.get("INTER_MIND_API_PORT", "8425"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
