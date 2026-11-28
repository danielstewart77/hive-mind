"""lucent-api — Hive Mind nervous-system service for graph + vector memory.

Bound to the internal Docker `hivemind` network, and every memory and graph
route is gated on a per-holder credential: which mind a request speaks for is
read off the value it presented, never off the request. See
`lucent_api.holder_access`. Fronts the existing lucent_graph and lucent_memory
modules with HTTP endpoints, removing the MCP coupling from callers.

Run: python -m lucent_api.server
"""

from __future__ import annotations

import contextlib
import os
from typing import AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from lucent_api.auth import require_admin_bearer

import hive_logging
from hive_logging import configure_logging, install_fastapi_logging, log_event

log = configure_logging("lucent-api")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the store before anything serves, and close it once at the end.

    The app had no lifespan at all while the store was a file that opened
    itself on first use. `hive_data` opens no pool on demand, deliberately,
    so this is the only place the pool can be opened — and the ownership
    pass runs here because it is how a mind whose credential was minted
    since the last boot becomes visible in its own graph.
    """
    from lucent_api import lucent

    await lucent.open_store()
    try:
        await lucent.backfill_owners()
    except Exception:
        # A backfill that fails must not stop the store from serving: every
        # row it would have claimed is still readable by a service, and a
        # lucent that refuses to start takes every mind's memory with it.
        log.exception("lucent.owner.backfill: the pass did not complete")
    try:
        yield
    finally:
        await lucent.close_store()


def create_app() -> FastAPI:
    app = FastAPI(
        title="lucent-api",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    install_fastapi_logging(app, log, "lucent-api")

    # hive_logging.py is copied into this service's image while this file is
    # mounted from the checkout. A pull therefore brings this route before a
    # rebuild brings the feed, and the service must still start: the feed is
    # looked up when asked for, and an image without it answers 404, which the
    # console shows as a component to update.
    @app.get("/diagnostics/errors", dependencies=[Depends(require_admin_bearer)])
    async def diagnostic_errors():
        feed = getattr(hive_logging, "recent_errors", None)
        if feed is None:
            return JSONResponse(
                {"detail": "This image was built before the error feed; rebuild it"},
                status_code=404,
            )
        return feed()

    @app.get("/health")
    async def health() -> JSONResponse:
        """Whether the store answers, not whether the process is running.

        This was a hardcoded dict, which was defensible against a local file
        that essentially never failed. Against a database over a socket it
        made liveness unobservable: a restarted Postgres left every
        healthcheck green, comms' readiness passing, and every memory write
        returning two hundred with an error in the body — which is how a
        store that has been down for a month gets noticed.
        """
        from lucent_api import lucent

        live = await lucent.store_is_live()
        return JSONResponse(
            {"status": "ok" if live else "degraded", "service": "lucent-api",
             "store": "answering" if live else "not answering"},
            status_code=200 if live else 503)

    from lucent_api.routers.graph import router as graph_router
    from lucent_api.routers.memory import router as memory_router

    # Every memory and graph route resolves the holder that presented a
    # credential; the routes take it as a dependency because which mind is
    # asking is the answer they need. /health stays open.
    app.include_router(graph_router)
    app.include_router(memory_router)
    # No auth_dep: the soul router carries its own gate — a person's own
    # key, or a mind's own key scoped to that mind's own soul.
    from lucent_api.routers.soul import router as soul_router
    app.include_router(soul_router)

    log_event(log, "service.routes.registered", component="lucent-api")
    return app


app = create_app()


def main() -> None:
    port = int(os.environ.get("LUCENT_API_PORT", "8424"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", log_config=None, access_log=False)


if __name__ == "__main__":
    main()
