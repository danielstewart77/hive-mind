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
