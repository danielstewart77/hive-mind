"""The two credentials lucent still reads out of the environment.

Per-holder credentials — which mind is asking, and which minds it may read —
live in `lucent_api.holder_access`, which is what every memory and graph route
is gated by. What is left here is the admin credential and the soul
credential, and both are deliberately still single values.

`LUCENT_ADMIN_BEARER_TOKEN` has to be: it is how the console reaches the
store, and it is the only way the credential-minting route is reachable on a
first boot, when the credentials table is necessarily empty. There is no
bypass — unset, admin routes answer 503 rather than opening.

`LUCENT_SOUL_BEARER_TOKEN` is already narrower than everything else by
design: it is held by one holder, the soul-review flow, and the admin token is
deliberately *not* accepted for it. Making it per-holder would buy no
isolation it does not already have.

The store no longer holds a `LUCENT_BEARER_TOKEN` of its own. One value held
by every mind, every hook and every background process on every host made its
theft indistinguishable from its use, and an unset one used to mean "serve
everything to everybody" — the failure `holder_access` exists to close.
Holders still present that variable; it now carries each holder's own value
and is checked against the credentials table rather than against a copy kept
here.
"""
from __future__ import annotations

import logging
import os

from fastapi import Header, HTTPException

log = logging.getLogger(__name__)


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


if not os.environ.get("LUCENT_ADMIN_BEARER_TOKEN"):
    log.warning(
        "LUCENT_ADMIN_BEARER_TOKEN is unset — admin endpoints (schema "
        "mutation) will return 503 until it is set."
    )


def require_soul_bearer(authorization: str = Header(default="")) -> None:
    """Bearer gate for the one route that can change a mind's soul.

    Checks ``LUCENT_SOUL_BEARER_TOKEN`` and nothing else. The service token
    is deliberately not accepted, and neither is the admin token: the point
    of this credential is that the ones every hook, cron job and background
    process on a mind's host already hold cannot reach a soul. Accepting a
    more privileged token "for convenience" would hand it straight back to
    hive-tools, which is the path the small model's verdict travelled.

    Unset means locked (503), not open. A soul that is writable because
    nobody configured a token is the failure mode this exists to close.
    """
    expected = os.environ.get("LUCENT_SOUL_BEARER_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Soul endpoints disabled: LUCENT_SOUL_BEARER_TOKEN unset")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    if authorization[7:] != expected:
        raise HTTPException(401, "Invalid soul token")


if not os.environ.get("LUCENT_SOUL_BEARER_TOKEN"):
    log.warning(
        "LUCENT_SOUL_BEARER_TOKEN is unset — soul endpoints will return 503 "
        "until it is set. Souls are unwritable, not unprotected."
    )
