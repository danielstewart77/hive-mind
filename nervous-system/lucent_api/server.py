"""lucent-api — Hive Mind nervous-system service for graph + vector memory.

Bound to the internal Docker `hivemind` network, and every memory and graph
route is gated on a per-holder credential: which mind a request speaks for is
read off the value it presented, never off the request. See
`lucent_api.holder_access`. Fronts the existing lucent_graph and lucent_memory
modules with HTTP endpoints, removing the MCP coupling from callers.

Run: python -m lucent_api.server
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lucent_api import holder_access
from lucent_api.auth import require_admin_bearer

from hive_logging import configure_logging, install_fastapi_logging, log_event

log = configure_logging("lucent-api")


def create_app() -> FastAPI:
    app = FastAPI(
        title="lucent-api",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_fastapi_logging(app, log, "lucent-api")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "lucent-api"}

    from lucent_api.routers.graph import router as graph_router
    from lucent_api.routers.memory import router as memory_router

    # Every memory and graph route resolves the holder that presented a
    # credential; the routes take it as a dependency because which mind is
    # asking is the answer they need. /health stays open.
    app.include_router(graph_router)
    app.include_router(memory_router)
    # No auth_dep: the soul router carries its own, stricter credential.
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
    def put_credential(holder: str, body: CredentialRequest):
        try:
            conn = holder_access.connection()
            # The table may not exist yet: this is the first call of the
            # rollout, on a store that has never served a gated request, and
            # going straight to a SELECT made the documented first step answer
            # 500 on `no such table: credentials`.
            holder_access.init_credentials(conn)
            return holder_access.put_credential(
                conn,
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
    def revoke_credential(holder: str):
        """Lands on that holder's next request: the store reads the table per
        call, so there is nothing to restart and no cache to clear."""
        conn = holder_access.connection()
        holder_access.init_credentials(conn)
        revoked = holder_access.revoke_credential(conn, holder)
        return {"holder": holder.strip().lower(), "revoked": revoked}

    @app.get("/credentials", dependencies=[Depends(require_admin_bearer)])
    def list_credentials():
        """Which holders have a credential and whether it is live. Never the
        value: an operator does not need it, they need to know whether the
        holder has one that works."""
        conn = holder_access.connection()
        holder_access.init_credentials(conn)
        rows = conn.execute(
            "SELECT holder, username, role, mind_id, created_at, revoked_at "
            "FROM credentials ORDER BY username, holder"
        ).fetchall()
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
