"""The GitHub token routes a mind in this stack serves.

The console cannot write this itself. It has to land where *this* mind's own
`git` and `gh` look, and only the mind's own filesystem can see either — the
console holds no mount into a container's home directory and a mind on
another machine has none to offer. So the mind writes.

Where, and the verify-before-store, live in `hive_surfaces.github_token`: one
rule in the module both hosts install, because a writer that disagreed with
the reader would store a token in a place nothing consults. What is here is
the route in front of it, admin-guarded like `/runtime`, `/skills`, `/files`,
`/models` and `/surface-token`.

A named keyring key is **required** here and not merely preferred. Every mind
in this stack resolves `HIVE_PROJECT_DIR` to the same `/usr/src/app` and the
same keyring file, separated only by key name, so a mind falling back to the
default name would store its token over another mind's and both rows would
then report the same tail and the same account, working and wrong. A mind
whose compose does not name one refuses rather than storing anywhere.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from hive_surfaces import github_token

from core.hive_logging import log_event
from minds.runtime_api import authorize_admin

#: What `gh` reads out of the environment, and what every harness process
#: inherits from this one. It takes precedence over `hosts.yml`, so a mind
#: holding its own token sets it here — otherwise a token fetched from the
#: hive's shared secret store at boot shadows the per-mind one on every
#: spawn, and the page reports working over a `gh` using the other value.
GH_ENV_VAR = "GH_TOKEN"  # secret-guard: allow — a variable name


def _no_per_mind_key() -> JSONResponse:
    return JSONResponse(
        {
            "error": (
                "this mind names no GITHUB_TOKEN_KEYRING_KEY, and every mind "
                "in this stack shares one keyring file — storing would "
                "overwrite another mind's token"
            )
        },
        status_code=503,
    )


def adopt_stored_token() -> list[str]:
    """Make this mind's own stored token the one its harnesses will use.

    Called at boot, after the shared-secret fetch that may have put another
    value in `GH_TOKEN`: this mind's own token wins over the hive's. Writes
    the files `git` reads at the same time, because a rebuilt container has
    the keyring and none of them.
    """
    if github_token.storage_location()[0] != "keyring":
        return []
    token = github_token.stored_token()
    if not token:
        return []
    os.environ[GH_ENV_VAR] = token
    return github_token.apply_stored()


def install_github_token_routes(app: FastAPI, *, mind_id: str, log) -> None:
    """`GET` and `PUT /github-token`, both admin-guarded.

    The read is guarded as well as the write: it names the account this mind
    pushes as, on a port that answers across the LAN.
    """

    @app.get("/github-token")
    async def get_github_token(request: Request) -> Any:
        """Whether this mind has a GitHub token the API accepts.

        Never the token. The operator does not need the value — they need to
        know whether this mind has one that works and which account it
        belongs to, so they can see it is the one they meant.
        """
        refusal = authorize_admin(request)
        if refusal is not None:
            return refusal
        state = await github_token.status()
        return {
            "stored": state.stored,
            "accepted": state.accepted,
            "login": state.login,
            "where": state.where,
            "detail": state.detail,
            "settable": github_token.storage_location()[0] == "keyring",
        }

    @app.put("/github-token")
    async def put_github_token(request: Request) -> Any:
        """Replace the token this mind pushes with.

        Verified against GitHub before anything is stored, so a paste with a
        character missing leaves the working token in place rather than
        surfacing later as a push that fails for a mind nobody is watching.
        """
        refusal = authorize_admin(request)
        if refusal is not None:
            return refusal
        if github_token.storage_location()[0] != "keyring":
            return _no_per_mind_key()
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)
        token = str(body.get("token") or "").strip()
        if not token:
            return JSONResponse({"error": "token required"}, status_code=400)
        try:
            state = await github_token.replace(token)
        except github_token.TokenRefused as exc:
            # 400, not 401: the credential that reached this route was fine.
            # What was refused is the payload, and a 401 here would send the
            # operator off to check the admin token they just used.
            log_event(
                log, "mind.github_token.refused", level=logging.WARNING,
                mind_id=mind_id, reason=str(exc),
            )
            return JSONResponse({"error": str(exc), "stored": False}, status_code=400)
        except OSError as exc:
            return JSONResponse(
                {"error": f"could not store the token: {exc}"}, status_code=500
            )
        # Every harness spawned from here on inherits this value; one already
        # running keeps what it booted with, which the response says.
        os.environ[GH_ENV_VAR] = token
        log_event(
            log, "mind.github_token.replaced", mind_id=mind_id,
            login=state.login, where=state.where,
        )
        return {
            "saved": True,
            "stored": True,
            "accepted": True,
            "login": state.login,
            "where": state.where,
            "configured": state.configured,
            # `git` and `gh` read their files per invocation, so a new
            # subprocess has it. A harness process already running does not.
            "restart_required": False,
        }
