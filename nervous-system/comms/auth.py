"""Bearer-token auth for comms.

Mirrors ``lucent_api/auth.py`` (same env-var shape, same logic). Two
tokens:

- ``COMMS_BEARER_TOKEN`` — service token. Required for every route
  except health/root. Empty/unset = bypass mode (with a startup
  warning), so a fresh container can come up before the operator
  finishes wiring secrets.

There is no admin token: an administrator acts with their own person key,
which the per-holder gate in `user_access.UserAccess` resolves.
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
    route proves the service token *and* the rename token. 
    **Unset refuses.** The service gate bypasses when its token is unset so a
    fresh container can boot before the secrets are wired; doing that here
    would mean a deployment missing one variable silently lets anything on the
    LAN rename any mind's conversations. There is no state of this system in
    which that is the safer default.
    """
    expected = os.environ.get("COMMS_RENAME_TOKEN", "")
    offered = x_rename_token.strip()
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
    # Bytes, not str: `compare_digest` raises TypeError on a str holding
    # anything outside ASCII, and a 500 here would read as the route being
    # missing rather than the credential being refused.
    candidate = offered.encode("utf-8")
    if secrets.compare_digest(candidate, expected.encode("utf-8")):
        return
    raise HTTPException(401, "Invalid rename token")
