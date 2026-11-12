"""The two credentials lucent still reads out of the environment.

Per-holder credentials — which mind is asking, and which minds it may read —
live in `lucent_api.holder_access`, which is what every memory and graph route
is gated by. What is left here is the admin credential and the soul
credential, and both are deliberately still single values.

`LUCENT_ADMIN_BEARER_TOKEN` has to be: it is how the console reaches the
store, and it is the only way the credential-minting route is reachable on a
first boot, when the credentials table is necessarily empty. There is no
bypass — unset, admin routes answer 503 rather than opening.

`LUCENT_SOUL_BEARER_TOKEN` is the operator's key to every soul, and it is
held by the hive site alone — the console's soul editor, actor `console`. No
mind holds it. A mind reaches its own soul with its own per-holder value, the
one its `LUCENT_BEARER_TOKEN` carries, and `require_soul_caller` scopes that
value to the one Mind node it represents. One shared value that could write
any mind's soul, held by a mind, was a mind holding every other mind's
identity; a second per-mind key would have been a second value to mint,
rotate and lose for no isolation the first does not already give.

The store no longer holds a `LUCENT_BEARER_TOKEN` of its own. One value held
by every mind, every hook and every background process on every host made its
theft indistinguishable from its use, and an unset one used to mean "serve
everything to everybody" — the failure `holder_access` exists to close.
Holders still present that variable; it now carries each holder's own value
and is checked against the credentials table rather than against a copy kept
here.
"""
from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass

import hive_data
from fastapi import Header, HTTPException

from lucent_api.holder_access import MIND, Caller, _bearer, resolve_caller
from lucent_api.soul import OwnSoul

log = logging.getLogger(__name__)


def require_admin_bearer(authorization: str = Header(default="")) -> None:
    """Bearer gate for admin endpoints (schema mutation).

    Checks ``LUCENT_ADMIN_BEARER_TOKEN`` and nothing else
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


@dataclass(frozen=True)
class SoulCaller:
    """Who is at the soul door: the console, or one mind about itself.

    ``mind`` is None for the console's key, which reaches every soul. For a
    mind it is the holder its own value resolved to, and ``own`` is the one
    soul that value reaches (``soul.OwnSoul``).
    """

    mind: Caller | None = None

    @property
    def is_console(self) -> bool:
        return self.mind is None

    @property
    def own(self) -> OwnSoul | None:
        """None for the console (every soul); otherwise this mind's own."""
        if self.mind is None:
            return None
        return OwnSoul(
            name=self.mind.holder.lower(),
            mind_ids=frozenset(n.lower() for n in self.mind.own_names))


async def require_soul_caller(authorization: str = Header(default="")) -> SoulCaller:
    """Bearer gate for the routes that read and change a mind's soul.

    Two values open it and nothing else does. ``LUCENT_SOUL_BEARER_TOKEN``
    is the console's, and reaches every soul. A mind's own per-holder value —
    the same one the graph routes resolve through ``require_holder`` — reaches
    that mind's soul and no other; the route compares the Mind node it found
    against the holder, on the same row it then writes.

    The admin value is deliberately not accepted, and neither is a service's
    or a person's: the point of this door is that the credentials every hook,
    cron job and background service already hold cannot reach a soul.
    Accepting a more privileged token "for convenience" would hand it
    straight back to hive-tools, which is the path the small model's verdict
    travelled.

    An unset console key disables the console's door, not the minds'. A
    value that is neither a mind's nor a configured console key answers 503
    when the console key is unset, as it always did — locked, never open.
    """
    token = _bearer(authorization)
    if not token:
        raise HTTPException(401, "Missing or invalid Authorization header")
    expected = os.environ.get("LUCENT_SOUL_BEARER_TOKEN", "")
    if expected and hmac.compare_digest(
        token.encode("utf-8"), expected.encode("utf-8")
    ):
        return SoulCaller()
    try:
        caller = await resolve_caller(token)
    except hive_data.HiveDataError as refused:
        # The store did not answer, which says nothing about the caller. A
        # 503 the review skill reports as "ask again", not a bare 500.
        log.warning("soul.auth.store_refused: %s: %s",
                    type(refused).__name__, refused)
        raise HTTPException(
            503, "the credential store did not answer; try again shortly"
        ) from refused
    if caller.role == MIND and caller.holder and not caller.is_admin:
        return SoulCaller(mind=caller)
    if not expected:
        raise HTTPException(
            503, "Soul endpoints disabled: LUCENT_SOUL_BEARER_TOKEN unset "
            "and this is not a mind's own credential")
    raise HTTPException(401, "Invalid soul token")


if not os.environ.get("LUCENT_SOUL_BEARER_TOKEN"):
    log.warning(
        "LUCENT_SOUL_BEARER_TOKEN is unset — the console cannot edit souls "
        "until it is set. Each mind still reaches its own with its own key."
    )
