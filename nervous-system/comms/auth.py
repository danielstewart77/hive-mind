"""Bearer-token auth for comms.

Mirrors ``lucent_api/auth.py`` (same env-var shape, same logic). Two
tokens:

- ``COMMS_BEARER_TOKEN`` — service token. Required for every route
  except health/root. Empty/unset = bypass mode (with a startup
  warning), so a fresh container can come up before the operator
  finishes wiring secrets.
- ``COMMS_ADMIN_BEARER_TOKEN`` — admin token. Required for
  consequential routes (secret-scope grant/revoke, broker
  register/update/delete). Unset = admin endpoints return 503; we do
  not bypass admin.

``require_bearer`` accepts EITHER token, so admin callers can hold a
single token. ``require_admin_bearer`` accepts ONLY the admin token.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import Header, HTTPException, Request

log = logging.getLogger(__name__)

# Endpoints with their own request-level auth (HMAC, signature, etc.) that
# would be incorrectly gated by the global bearer middleware.
BEARER_EXEMPT_PATHS = frozenset({"/sms/inbound"})


def require_bearer(
    request: Request, authorization: str = Header(default="")
) -> None:
    if request.url.path in BEARER_EXEMPT_PATHS:
        return
    expected = os.environ.get("COMMS_BEARER_TOKEN", "")
    admin = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    if not expected:
        return  # bypass — startup warning emitted at module load
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization[7:]
    if token != expected and (not admin or token != admin):
        raise HTTPException(401, "Invalid token")


def require_rename_bearer(
    x_rename_token: str = Header(default=""),
    authorization: str = Header(default=""),
) -> None:
    """The one small credential that may change a conversation's name.

    Naming used to live in the browser terminal behind its own token, chosen
    deliberately so that the capability to rename a conversation was not
    guarded by the key that opens everything — a copy of it escaping into a
    log or a screenshot is worth exactly one renamed conversation. Moving the
    name onto the session row must not quietly hand that capability to
    ``COMMS_BEARER_TOKEN``, which every surface bot and every mind container
    on this hive already holds, the ones on the kids' machines included.

    So it rides its own header rather than ``Authorization``: the global
    bearer gate has already consumed that one, and a caller reaching this
    route proves the service token *and* the rename token. The admin token is
    accepted in the same header because the console and the operator have to
    be able to fix a name without being handed a third secret.

    **Unset refuses.** The service gate bypasses when its token is unset so a
    fresh container can boot before the secrets are wired; doing that here
    would mean a deployment missing one variable silently lets anything on the
    LAN rename any mind's conversations. There is no state of this system in
    which that is the safer default.
    """
    expected = os.environ.get("COMMS_RENAME_TOKEN", "")
    admin = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    offered = x_rename_token.strip()
    # An admin caller is answered whatever else is configured: the console and
    # the operator need a way in.
    if admin and secrets.compare_digest(
        (offered or authorization[7:].strip() if authorization.startswith("Bearer ")
         else offered).encode("utf-8"),
        admin.encode("utf-8"),
    ):
        return
    # Nothing to check against means renaming is not configured, and that is
    # what the answer has to say. Gating this on *both* tokens being absent
    # made the sentence unreachable on any hive that has an admin token — which
    # is all of them — so a deployment that simply never set the rename token
    # answered 401 to every surface and sent the operator looking at the
    # network instead of at one missing variable.
    if not expected:
        raise HTTPException(
            503, "Renaming disabled: COMMS_RENAME_TOKEN unset"
        )
    if not offered and authorization.startswith("Bearer "):
        # A caller holding only the admin token may present it the usual way.
        offered = authorization[7:].strip()
    # Bytes, not str: `compare_digest` raises TypeError on a str holding
    # anything outside ASCII, and a 500 here would read as the route being
    # missing rather than the credential being refused.
    candidate = offered.encode("utf-8")
    for accepted in (expected, admin):
        if accepted and secrets.compare_digest(candidate, accepted.encode("utf-8")):
            return
    raise HTTPException(401, "Invalid rename token")


def require_admin_bearer(authorization: str = Header(default="")) -> None:
    expected = os.environ.get("COMMS_ADMIN_BEARER_TOKEN", "")
    if not expected:
        raise HTTPException(
            503, "Admin endpoints disabled: COMMS_ADMIN_BEARER_TOKEN unset"
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    if authorization[7:] != expected:
        raise HTTPException(401, "Invalid admin token")


if not os.environ.get("COMMS_BEARER_TOKEN"):
    log.warning(
        "COMMS_BEARER_TOKEN is unset — auth bypass active. "
        "Set the env var to enforce bearer-token gating."
    )
if not os.environ.get("COMMS_ADMIN_BEARER_TOKEN"):
    log.warning(
        "COMMS_ADMIN_BEARER_TOKEN is unset — admin endpoints "
        "(secret-scope grant/revoke, broker register/update/delete) "
        "will return 503 until it is set."
    )
