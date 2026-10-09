"""The surface token routes a mind in this stack serves.

The console cannot write a bot token itself. It has to land where *this*
mind's surface looks for it, which here is a per-mind keyring key — several
surfaces run from one image on one machine, and the environment cannot hold
several values under one name. Only the mind's own filesystem can see that
keyring, so the mind writes.

The rule about *where*, and the verify-before-store, live in
`hive_surfaces.token_store`: it is the same precedence the surfaces read by,
and a writer that disagreed with the reader would store a token in a place
nothing consults. What is here is the route in front of it, admin-guarded
like `/runtime`, `/skills`, `/files` and `/models`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from hive_surfaces import token_store

from core.hive_logging import log_event
from minds.runtime_api import authorize_admin


def install_surface_token_routes(app: FastAPI, *, mind_id: str, log) -> None:
    """`GET` and `PUT /surface-token`, both admin-guarded.

    The read is guarded as well as the write: it names the bot this mind
    authenticates as, on a port that answers across the LAN.
    """

    @app.get("/surface-token")
    async def get_surface_token(request: Request) -> Any:
        """Whether this mind's surface has a token the bot API accepts.

        Never the token. A route that returned one would put every bot in the
        hive one admin credential away from being impersonated, and the value
        is not what the operator needs — they need to know whether the
        surface has one that works, and which bot it belongs to, so they can
        see it is the mind they meant.
        """
        refusal = authorize_admin(request)
        if refusal is not None:
            return refusal
        state = await token_store.status()
        return {
            "stored": state.stored,
            "accepted": state.accepted,
            "bot_username": state.bot_username,
            "where": state.where,
            "detail": state.detail,
            "preview": state.preview,
        }

    @app.put("/surface-token")
    async def put_surface_token(request: Request) -> Any:
        """Replace the token this mind's surface authenticates with.

        Verified against the bot API before anything is stored, so a paste
        with a character missing leaves the working token in place instead of
        arriving at the next restart as a surface that 401s on every poll and
        reads as an outage. Storing it does not touch the running surface,
        which read its token at startup and keeps it until recreated.
        """
        refusal = authorize_admin(request)
        if refusal is not None:
            return refusal
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)
        token = str(body.get("token") or "").strip()
        if not token:
            return JSONResponse({"error": "token required"}, status_code=400)
        try:
            state = await token_store.replace(token)
        except token_store.TokenRefused as exc:
            # 400, not 401: the credential that reached this route was fine.
            # What was refused is the payload, and a 401 here would send the
            # operator off to check the admin token they just used.
            log_event(
                log, "mind.surface_token.refused", level=logging.WARNING,
                mind_id=mind_id, reason=str(exc),
            )
            return JSONResponse({"error": str(exc), "stored": False}, status_code=400)
        except OSError as exc:
            return JSONResponse(
                {"error": f"could not store the token: {exc}"}, status_code=500
            )
        log_event(
            log, "mind.surface_token.replaced", mind_id=mind_id,
            bot_username=state.bot_username, where=state.where,
        )
        return {
            "saved": True,
            "stored": True,
            "accepted": True,
            "bot_username": state.bot_username,
            "where": state.where,
            "preview": state.preview,
            "restart_required": True,
        }
