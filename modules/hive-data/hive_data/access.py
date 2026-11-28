"""Recognising a key: the one record every service reads.

A key is recorded once, in `core.credentials`, as a digest. The gateway, the
memory store and the tools all recognise a presented key by reading that row
on the request, so a key recorded, replaced or cut off once takes effect at
all three on their next request. Nothing here caches.

A mind's key has the role `mind`; a person's own key has the role `user` and
is held under the person's username. A person acts with their own key. An
administrator is a person in the `admins` group whose account is not
disabled, read on the call.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import core

ADMINS_GROUP = "admins"
MIND = "mind"
USER_KEY = "user"


@dataclass(frozen=True)
class Key:
    """Who a presented key belongs to, as the record says."""

    holder: str
    username: str
    role: str


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


async def key_for(token: str) -> Key | None:
    """The live record a presented key matches, or None (unknown or cut off)."""
    token = (token or "").strip()
    if not token:
        return None
    sha = digest(token)
    row = await core.credential_for_token(sha)
    if row is None:
        # A mind not yet moved to its one key still presents the memory-store
        # or tools key it had; those rows stay until it is moved (step 20).
        row = await core.legacy_key_for(sha)
    if row is None:
        return None
    return Key(holder=_norm(row["holder"]), username=_norm(row["username"]),
               role=str(row["role"] or ""))


async def live_person(username: str):
    """The person's account row, or None when there is none or it is disabled."""
    name = _norm(username)
    if not name:
        return None
    for row in await core.list_users():
        if _norm(row["username"]) == name:
            return None if row["disabled_at"] is not None else row
    return None


async def person_is_administrator(username: str) -> bool:
    """In the admins group and not disabled, read from the database now."""
    row = await live_person(username)
    if row is None:
        return False
    return ADMINS_GROUP in await core.groups_of(row["id"])


async def administrator_for(token: str) -> Key | None:
    """The key's person when it is a person's own key and that person is an
    administrator; None for a mind's key, a service's, a non-administrator's
    or an unknown one."""
    key = await key_for(token)
    if key is None or key.role != USER_KEY:
        return None
    return key if await person_is_administrator(key.username) else None


async def mind_for_name(name: str):
    """The registered mind a name or id stands for, or None."""
    return await core.get_mind_by_id_or_name(str(name or "").strip())
