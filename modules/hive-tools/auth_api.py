"""Bearer token authentication for the API surface."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from typing import Any

import store
from fastapi import HTTPException, Request
from hive_data.errors import Conflict


#: The holder of the one credential that may decide an approval over the API.
#: Not a mind: the surface bots hold it, and send it when the operator taps
#: Approve or Deny. Minted like any other token, under this name, and it is
#: the name that gives it the power - so it does nothing else (see
#: `require_api_token`), and a mind's token, which may make gated calls,
#: never decides one.
APPROVER_HOLDER = "hitl-approver"

#: What a mind is told when it tries to decide a gate.
#: The sentence that matters first: the surface bot shows the first 120
#: characters of a refusal, and "still pending" is what the operator needs.
MIND_CANNOT_DECIDE = (
    "A mind cannot decide its own gate; the request is still pending. "
    "The token sent is {holder}'s, and a mind's token may make gated calls "
    "but never approve or deny one. Decide on the approval page, or from a "
    "bot holding the approver credential.")


#: Holders the credential routes never write, revoke or remove, whatever the
#: caller's authority: the approver (so no console credential can make one),
#: the internal replay secret, and the holder of the shared service token
#: every consumer still presents until it is retired (story 884).
RESERVED_HOLDERS = frozenset({APPROVER_HOLDER, "internal", "skippy"})

#: The holder the shared service token is registered under. Reserved until
#: that token is retired; then a mind of that name may hold a key of its own.
SHARED_HOLDER = "skippy"

#: What a holder's name may be once lowercased and trimmed.
_HOLDER_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

#: Shorter than any real key; stops a stray character becoming a credential.
MIN_VALUE_LENGTH = 8


class CredentialRefused(ValueError):
    """The credential routes would not do this. Nothing changed."""


def normalize_holder(holder: str) -> str:
    """A holder's name as every credential route and row spells it."""
    return str(holder or "").strip().lower()


def _checked_holder(holder: str, *, shared_retired: bool = False) -> str:
    name = normalize_holder(holder)
    if name in RESERVED_HOLDERS and not (name == SHARED_HOLDER and shared_retired):
        raise CredentialRefused(
            f"{name!r} is not a holder this route manages; its credential is "
            "made and kept another way")
    if not _HOLDER_NAME.fullmatch(name):
        raise CredentialRefused(
            "a holder is 1 to 64 letters, digits, dots, hyphens or underscores")
    return name


def bearer_value(auth_header: str) -> str:
    """The token in a `Bearer` header, as it was meant.

    Trimmed, as the gateway and the memory store trim it: a value read out of
    a CRLF `.env` carries a carriage return, and the same key must open all
    three services or a mind is accepted by two and refused by the third.
    """
    return auth_header[7:].strip()


async def register_credential(holder: str, value: str) -> dict[str, Any]:
    """Make `value` the holder's one working credential, as supplied.

    Registering a value another holder has is refused, naming that holder.
    Any value the holder had is cut off in the same step. A request it was
    waiting on stays decidable: the holder is the same mind, with a new key.
    """
    name = _checked_holder(holder, shared_retired=await shared_token_retired())
    raw = str(value or "").strip()
    if len(raw) < MIN_VALUE_LENGTH or re.search(r"\s", raw):
        raise CredentialRefused(
            f"a credential is at least {MIN_VALUE_LENGTH} characters with no "
            "spaces in it")
    digest = hashlib.sha256(raw.encode()).hexdigest()
    now = int(time.time())
    try:
        async with store.transaction():
            await store.register_token(name, digest, now)
    except Conflict as exc:
        raise CredentialRefused(str(exc)) from exc
    return {"holder": name, "created_at": now}


async def revoke_credential(holder: str, *, remove: bool = False) -> dict[str, Any]:
    """Cut one holder off, and expire what it was waiting on.

    Its approved-but-unrun requests are not touched (they already ran or are
    running); its pending ones are made undecidable, so approving one later
    cannot replay a call as a mind that no longer has a key, and its sudo
    grants are deleted so a mind later given the name starts with none.
    `remove` deletes
    the holder's rows so its digest may be given to a mind again: the undo of
    a creation.
    """
    name = _checked_holder(holder, shared_retired=await shared_token_retired())
    now = int(time.time())
    async with store.transaction():
        await store.hold_holder(name)
        revoked = await store.revoke_holder_tokens(name, now)
        expired = await store.expire_pending_requests(name, now)
        await store.clear_sudo_grants(name)
        removed = await store.remove_holder_tokens(name) if remove else 0
    answer: dict[str, Any] = {"holder": name, "revoked": revoked > 0,
                              "expired_requests": expired}
    if remove:
        answer["removed"] = removed > 0
    return answer


async def shared_token_retired() -> bool:
    """Whether the shared service token has been retired: its holder has a
    revoked row. Nothing but `retire_shared_token` revokes that holder while
    the name is reserved."""
    for row in await store.holder_tokens():
        if str(row["holder"]) == SHARED_HOLDER:
            return row["revoked_at"] is not None
    return False


async def retire_shared_token() -> dict[str, Any]:
    """Revoke the shared service token every consumer presented before each
    held a credential of its own, and expire what its holder was waiting on.

    Nothing else is touched: every other holder, the approver credential and
    the internal secret stand as they were. Repeating it changes nothing and
    says so. A mind of the shared holder's name may register its own key from
    then on."""
    now = int(time.time())
    async with store.transaction():
        await store.hold_holder(SHARED_HOLDER)
        revoked = await store.revoke_holder_tokens(SHARED_HOLDER, now)
        expired = await store.expire_pending_requests(SHARED_HOLDER, now)
    return {"holder": SHARED_HOLDER, "revoked": revoked > 0,
            "expired_requests": expired}


async def list_credentials() -> list[dict[str, Any]]:
    """Who holds a credential: name, live or not, whose mind it is. Never a value."""
    owners: dict[str, str] = {}
    for mind in await store.mind_names():
        owners[mind] = (await store.mind_owner(mind)) or ""
    rows = []
    for row in await store.holder_tokens():
        holder = str(row["holder"])
        is_mind = holder in owners
        rows.append({
            "holder": holder,
            "username": owners.get(holder) or None,
            "role": "mind" if is_mind else "service",
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
            "live": bool(row["live"]),
        })
    return rows


async def require_admin_bearer(request: Request) -> None:
    """FastAPI dependency: the one credential that may manage holders.

    Held by the console and nothing else. Never a holder's token, a mind's
    included, and never the internal secret.
    """
    expected = os.environ.get("TOOLS_ADMIN_BEARER_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Credential routes are disabled: TOOLS_ADMIN_BEARER_TOKEN is unset")
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    if not secrets.compare_digest(bearer_value(header).encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid admin token")


async def generate_token(name: str) -> str:
    """Generate a new API token, store its SHA-256 hash, and return the raw token."""
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    await store.create_token(name, token_hash, int(time.time()))
    return raw


async def validate_token(raw_token: str) -> str | None:
    """Validate a raw token and return the mind name, or None if invalid/revoked."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = await store.live_token(token_hash)
    if row is None:
        return None

    await store.record_token_use(int(row["id"]), int(time.time()))
    return str(row["name"])


async def generate_approver_token() -> str:
    """Mint the approver credential: a token held by `APPROVER_HOLDER`."""
    return await generate_token(APPROVER_HOLDER)


async def revoke_token(token_id: int) -> bool:
    """Revoke a token, reporting whether this call was the one that did it.

    A token already revoked answers False rather than True: the count is the
    arbitration, and a caller told it succeeded twice cannot tell a
    revocation it performed from one it merely repeated.
    """
    return bool(await store.revoke_token(token_id, int(time.time())))


async def list_tokens() -> list[dict[str, Any]]:
    """Return all tokens (active and revoked), never their digests."""
    return await store.tokens()


async def require_api_token(request: Request) -> str:
    """FastAPI dependency: validate bearer token from Authorization header.

    Returns the caller mind name on success, raises 401 on failure.
    Accepts 'Internal <INTERNAL_SECRET>' for HITL replay calls.

    The approver credential is refused here with 403. It exists to decide
    approvals and for nothing else: a credential that could also make gated
    calls could ask for something and then approve its own request.
    """
    from config import INTERNAL_SECRET

    auth_header = request.headers.get("Authorization", "")

    # Internal HITL replay bypass
    if auth_header == f"Internal {INTERNAL_SECRET}":
        return "internal"

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    name = await validate_token(bearer_value(auth_header))
    if name is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    if name == APPROVER_HOLDER:
        raise HTTPException(
            status_code=403,
            detail="The approver credential decides approvals "
                   "(POST /hitl/{id}/respond) and does nothing else.")

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


async def require_approver(request: Request) -> str:
    """FastAPI dependency for deciding an approval over the API.

    Only the approver credential passes. A mind's token - any mind's, the
    operator mind's included - is refused with 403 before anything is
    decided, so the request stays pending; so is hive-tools' own internal
    replay secret, which replays a decision and never makes one. The other
    way to decide is Daniel's sign-in on the approval page, which does not
    come through here.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        if auth_header.startswith("Internal "):
            raise HTTPException(
                status_code=403,
                detail="hive-tools' internal secret replays a decision and "
                       "never makes one; the request is still pending.")
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    name = await validate_token(bearer_value(auth_header))
    if name is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    if name != APPROVER_HOLDER:
        raise HTTPException(status_code=403,
                            detail=MIND_CANNOT_DECIDE.format(holder=name))
    return name
