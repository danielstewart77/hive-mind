"""Bearer-token auth middleware (F9).

Validates ``Authorization: Bearer <token>`` against ``LUCENT_BEARER_TOKEN``
env var. Health endpoint and root stay open; every other route uses this
as a dependency.

Empty/unset env var = bypass with a startup warning. This is a deployment
safety: a fresh container won't accidentally lock itself out before the
operator gets a chance to set the token. Once ``LUCENT_BEARER_TOKEN`` is
set, validation is enforced and unauthenticated requests get 401.
"""
from __future__ import annotations

import logging
import os

from fastapi import Header, HTTPException

log = logging.getLogger(__name__)


def require_bearer(authorization: str = Header(default="")) -> None:
    expected = os.environ.get("LUCENT_BEARER_TOKEN", "")
    admin = os.environ.get("LUCENT_ADMIN_BEARER_TOKEN", "")
    if not expected:
        # Bypass mode. Logged once at module load time below.
        return
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization[7:]
    # Admin token is strictly more privileged than the regular bearer and
    # therefore also valid for routes gated by require_bearer. This matters
    # because admin endpoints (e.g. /graph/schema/types) live inside the
    # graph router, which has require_bearer as a router-level dep — without
    # this acceptance, hive-tools would have to send two different headers.
    if token != expected and (not admin or token != admin):
        raise HTTPException(401, "Invalid token")


def require_admin_bearer(authorization: str = Header(default="")) -> None:
    """Bearer gate for admin endpoints (schema mutation).

    Distinct from ``require_bearer`` — checks ``LUCENT_ADMIN_BEARER_TOKEN``
    instead of the regular service token. Only hive-tools holds this; minds
    do not. If the admin token is unset, admin endpoints are locked (NOT
    bypass-open, unlike the regular bearer) — schema mutation is too
    consequential to leave unauthenticated.
    """
    expected = os.environ.get("LUCENT_ADMIN_BEARER_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Admin endpoints disabled: LUCENT_ADMIN_BEARER_TOKEN unset")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    if authorization[7:] != expected:
        raise HTTPException(401, "Invalid admin token")


# Startup warning when bypass is active.
if not os.environ.get("LUCENT_BEARER_TOKEN"):
    log.warning(
        "LUCENT_BEARER_TOKEN is unset — auth bypass active. "
        "Set the env var to enforce bearer-token gating."
    )
if not os.environ.get("LUCENT_ADMIN_BEARER_TOKEN"):
    log.warning(
        "LUCENT_ADMIN_BEARER_TOKEN is unset — admin endpoints (schema "
        "mutation) will return 503 until it is set."
    )
