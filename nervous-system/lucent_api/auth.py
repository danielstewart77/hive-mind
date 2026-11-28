"""Who may reach the memory store's administrator and soul routes.

Per-holder keys — which mind is asking, and which minds it may read — are
recognised in `lucent_api.holder_access`, from the one record of keys in the
hive database (`core.credentials`) that the gateway and the tools read too.
This store holds no credential of its own: no administrator value, no soul
value, and no route that registers a key.

What an administrator does here (schema changes, reading the error feed,
editing any mind's soul) is done with the administrator's own person key:
a person recorded in `core.credentials` with the role `user`, who is in the
admins group and is not disabled, read on the request (`hive_data.access`).
A person who is not an administrator is refused. A mind's key is refused for
administrator actions.

A mind's owner, and an administrator, may edit that mind's soul with their own
person key. A mind reaches its own soul with its own key, scoped to the one
Mind node it represents (`require_soul_caller`, `soul.OwnSoul`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import hive_data
from fastapi import Header, HTTPException
from hive_data import access

from lucent_api.holder_access import MIND, Caller, _bearer, resolve_caller
from lucent_api.soul import OwnSoul

log = logging.getLogger(__name__)

STORE_DID_NOT_ANSWER = "the credential store did not answer; try again shortly"


async def require_admin_bearer(authorization: str = Header(default="")) -> None:
    """Gate for administrator routes (schema mutation, the error feed): an
    administrator's own person key and nothing else."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(401, "Missing or invalid Authorization header")
    try:
        admin = await access.administrator_for(token)
    except hive_data.HiveDataError as refused:
        log.warning("admin.auth.store_refused: %s: %s",
                    type(refused).__name__, refused)
        raise HTTPException(503, STORE_DID_NOT_ANSWER) from refused
    if admin is None:
        raise HTTPException(
            401, "This needs an administrator's own key")


@dataclass(frozen=True)
class SoulCaller:
    """Who is at the soul door: a person (any soul they may edit) or one mind
    about itself.

    ``mind`` is None for a person. For a mind it is the holder its own key
    resolved to, and ``own`` is the one soul that key reaches
    (``soul.OwnSoul``). ``person`` is the username of an administrator or of
    the owner of the mind whose soul is named; the route checks which.
    """

    mind: Caller | None = None
    person: str = ""
    administrator: bool = False

    @property
    def is_console(self) -> bool:
        """A person acting from the hive site (recorded as actor ``console``)."""
        return self.mind is None

    @property
    def own(self) -> OwnSoul | None:
        """None for a person (the route narrows an owner to their minds);
        otherwise this mind's own."""
        if self.mind is None:
            return None
        return OwnSoul(
            name=self.mind.holder.lower(),
            mind_ids=frozenset(n.lower() for n in self.mind.own_names))


async def require_soul_caller(authorization: str = Header(default="")) -> SoulCaller:
    """Gate for the routes that read and change a mind's soul.

    Two kinds of key open it. A mind's own key reaches that mind's soul and no
    other. A person's own key reaches the soul of a mind they own, and an
    administrator's reaches any; the route decides which mind it is, because
    that is named on the request. A service's key and an unknown one are
    refused.
    """
    token = _bearer(authorization)
    if not token:
        raise HTTPException(401, "Missing or invalid Authorization header")
    try:
        key = await access.key_for(token)
        if key is not None and key.role == access.USER_KEY:
            if await access.live_person(key.username) is None:
                raise HTTPException(401, "Invalid soul token")
            return SoulCaller(
                person=key.username,
                administrator=await access.person_is_administrator(key.username))
        caller = await resolve_caller(token)
    except hive_data.HiveDataError as refused:
        # The store did not answer, which says nothing about the caller.
        log.warning("soul.auth.store_refused: %s: %s",
                    type(refused).__name__, refused)
        raise HTTPException(503, STORE_DID_NOT_ANSWER) from refused
    if caller.role == MIND and caller.holder:
        return SoulCaller(mind=caller)
    raise HTTPException(401, "Invalid soul token")


async def person_may_edit_soul(who: SoulCaller, name: str) -> bool:
    """Whether this person may edit the soul of the mind called ``name``: an
    administrator any, anyone else only a mind they own."""
    if who.mind is not None:
        return True  # judged by `own` on the node itself
    if who.administrator:
        return True
    known = await access.mind_for_name(name)
    if known is None:
        return False
    owner = str(known["owner"] or "").strip().lower()
    return bool(owner) and owner == who.person
