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
from pydantic import BaseModel

from lucent_api import holder_access
from lucent_api.auth import require_admin_bearer

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
    # No auth_dep: the soul router carries its own gate — the console's soul
    # key, or a mind's own key scoped to that mind's own soul.
    from lucent_api.routers.soul import router as soul_router
    app.include_router(soul_router)

    _register_credential_routes(app)

    log_event(log, "service.routes.registered", component="lucent-api")
    return app


class CredentialRequest(BaseModel):
    #: The user this holder answers for. Provenance is decided by holder; the
    #: user is who the holder belongs to.
    username: str
    token: str
    #: `mind`, `service` or `person`. Required rather than defaulted: a
    #: default of `mind` silently narrows comms — which must read every mind's
    #: soul and recent memory to compose a prompt — to its own records alone,
    #: and the symptom is minds answering with no identity and no standing
    #: rules while every surface stays green. A default of `person` is no
    #: better: a person holder may write nothing, so a mind minted that way
    #: authenticates and then refuses its own memory.
    role: str
    #: The provenance this holder's rows are keyed by, when it differs from
    #: the holder name. Memory and graph rows carry a mind's UUID because
    #: every hook sends `$MIND_ID`, so a mind minted under its readable name
    #: names its UUID here and both spellings are then accepted.
    mind_id: str | None = None


def _register_credential_routes(app: FastAPI) -> None:
    """Minting and revoking, admin-guarded.

    Admin-guarded on the shared `LUCENT_ADMIN_BEARER_TOKEN` rather than on a
    holder credential, and deliberately: a route that issues credentials
    cannot answer to one it issued, and on a first boot the credentials table
    is necessarily empty, so a store whose minting route needed a minted
    credential could never be given its first one.
    """

    @app.put(
        "/credentials/{holder}", dependencies=[Depends(require_admin_bearer)]
    )
    async def put_credential(holder: str, body: CredentialRequest):
        """Mint or replace one holder's credential.

        No schema creation here any more. The table is applied to the
        database as an artifact, and the `CREATE TABLE IF NOT EXISTS` that
        used to run on this path — and on every gated request — would
        resolve against `search_path` and build an empty `public.credentials`
        while the real holders sat in `memory.credentials`, which reads as
        working while every mind gets a four-oh-one.
        """
        try:
            return await holder_access.put_credential(
                holder,
                body.username,
                body.token,
                role=body.role,
                mind_id=body.mind_id,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.delete(
        "/credentials/{holder}", dependencies=[Depends(require_admin_bearer)]
    )
    async def revoke_credential(holder: str):
        """Lands on that holder's next request: the store reads the table per
        call, so there is nothing to restart and no cache to clear."""
        revoked = await holder_access.revoke_credential(holder)
        return {"holder": holder.strip().lower(), "revoked": revoked}

    @app.get("/credentials", dependencies=[Depends(require_admin_bearer)])
    async def list_credentials():
        """Which holders have a credential and whether it is live. Never the
        value: an operator does not need it, they need to know whether the
        holder has one that works."""
        rows = await holder_access.all_credentials()
        return [
            {
                "holder": r["holder"],
                "username": r["username"],
                "role": r["role"],
                "mind_id": r["mind_id"],
                "created_at": r["created_at"],
                "revoked_at": r["revoked_at"],
                "live": r["revoked_at"] is None,
            }
            for r in rows
        ]


app = create_app()


def main() -> None:
    port = int(os.environ.get("LUCENT_API_PORT", "8424"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", log_config=None, access_log=False)


if __name__ == "__main__":
    main()
