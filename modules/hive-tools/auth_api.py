"""Bearer token authentication for the API surface.

A key is recognised from the one record of keys in the hive database
(`core.credentials`) on every request: a key recorded, replaced or cut off
once there takes effect here on the next request. The tools register no keys
and hold no administrator credential.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from hive_data import access

import store


#: What a mind is told when it tries to decide a gate.
#: The sentence that matters first: the person needs to know the request is
#: still pending.
MIND_CANNOT_DECIDE = (
    "A mind cannot decide a gate; the request is still pending. "
    "The key sent is {holder}'s, and a mind's key may make gated calls "
    "but never approve or deny one. A person decides, on the hive site, "
    "signed in as themselves.")

#: Holders no mind may be named after: the tools keep these for their own use.
RESERVED_HOLDERS = frozenset({"internal", "skippy", "shared-token-retired"})


def bearer_value(auth_header: str) -> str:
    """The token in a `Bearer` header, as it was meant.

    Trimmed, as the gateway and the memory store trim it: a value read out of
    a CRLF `.env` carries a carriage return, and the same key must open all
    three services or a mind is accepted by two and refused by the third.
    """
    return auth_header[7:].strip()


async def require_admin_bearer(request: Request) -> str:
    """FastAPI dependency: an administrator's own person key, and nothing else.

    The key is recognised from the one record of keys the gateway and the
    memory store read (`core.credentials`): a person's own key, the person an
    administrator and not disabled. A person who is not an administrator is
    refused, and so is a mind's key. The tools hold no administrator value.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    admin = await access.administrator_for(bearer_value(header))
    if admin is None:
        raise HTTPException(status_code=401, detail="This needs an administrator's own key")
    return admin.username


async def validate_token(raw_token: str) -> str | None:
    """The holder (a mind) a presented key belongs to, read from the one
    record of keys on this request, or None for an unknown or cut-off key."""
    key = await access.key_for(raw_token)
    if key is None or key.role == access.USER_KEY:
        # A person's own key is for administrator actions, never a tool call.
        return None
    return key.holder


async def require_api_token(request: Request) -> str:
    """FastAPI dependency: validate bearer token from Authorization header.

    Returns the caller mind name on success, raises 401 on failure.
    Accepts 'Internal <INTERNAL_SECRET>' for HITL replay calls.

    A person's own key is refused here (validate_token answers None for it):
    a person decides approvals and makes no gated call as a mind.
    """
    from config import INTERNAL_SECRET

    auth_header = request.headers.get("Authorization", "")

    # Internal HITL replay bypass
    if auth_header == f"Internal {INTERNAL_SECRET}":
        return "internal"

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    raw = bearer_value(auth_header)
    name = await validate_token(raw)
    if name is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")

    await _refuse_a_mind_of_someone_else(name)
    return name


#: What a mind owned by someone other than this hive-tools' admin is told.
#: Refused outright, before any tool runs or any approval is raised: the
#: approval would go to the person whose accounts these are, who is not the
#: mind's owner.
NOT_THE_OWNERS_TOOLS = (
    "{holder} belongs to {owner}, and the accounts this hive-tools holds are "
    "not {owner}'s, so the call is refused. {owner} has not connected "
    "integrations of their own yet.")


async def owner_refusal(holder: str) -> str | None:
    """Why a call from `holder` may not use these integrations, or None.

    Raises HTTPException(503) when whose mind it is cannot be read.
    """
    try:
        owner = await store.mind_owner(holder)
        if owner is None:
            return None
        admins = await store.admin_usernames()
    except Exception as exc:  # noqa: BLE001 - fail closed, said why
        raise HTTPException(
            status_code=503,
            detail="hive-tools could not read whose mind this is, so the call "
                   "is refused") from exc
    if owner in admins:
        return None
    return NOT_THE_OWNERS_TOOLS.format(holder=holder, owner=owner or "nobody")


async def _refuse_a_mind_of_someone_else(holder: str) -> None:
    """Refuse a mind whose owner is not the person whose integrations these are.

    DECIDED 2026-11-15 (Daniel): Daniel's Google tools and every other
    credential he set up are usable by his minds only; until a user can
    connect their own (hive story 2.1.16), a call from a mind whose owner is
    not the owner of the tool credentials is refused outright, never routed
    to Daniel for approval. A holder that is no mind - one minted for a
    service - is unchanged. A database that cannot answer refuses.
    """
    refused = await owner_refusal(holder)
    if refused:
        raise HTTPException(status_code=403, detail=refused)


async def require_person(request: Request) -> str:
    """FastAPI dependency for everything an approval asks of a person.

    Only a person's own key passes, and the answer is that person's
    username. A mind's key - any mind's, the operator mind's included - is
    refused with 403 before anything is read or decided, so the request stays
    pending; so is hive-tools' own internal replay secret, which replays a
    decision and never makes one. Nothing else (no bot credential, no
    service value) is a way to decide.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        if auth_header.startswith("Internal "):
            raise HTTPException(
                status_code=403,
                detail="hive-tools' internal secret replays a decision and "
                       "never makes one; the request is still pending.")
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    key = await access.key_for(bearer_value(auth_header))
    if key is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked key")
    if key.role != access.USER_KEY:
        raise HTTPException(status_code=403,
                            detail=MIND_CANNOT_DECIDE.format(holder=key.holder))
    if await access.live_person(key.username) is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked key")
    return key.username


async def may_decide(person: str, caller_mind: str) -> bool:
    """Whether `person` may decide a request `caller_mind` asked for: the
    owner of that mind, or any administrator. Read on the call."""
    owner = await store.mind_owner(caller_mind)
    if owner and owner == person.strip().lower():
        return True
    return await access.person_is_administrator(person)
