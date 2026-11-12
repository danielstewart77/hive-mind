"""The sanctioned door to a mind's soul.

Its own router rather than three more paths on the graph router, because
the graph router carries ``require_holder`` as a router-level dependency
and a request presents exactly one ``Authorization`` header. Mounting these
routes there would mean the soul token is rejected before it is read, or
that the service token is accepted here — and the second of those is the
bug this whole boundary exists to remove.

The soul routes are deliberately outside the graph's owner partition, and
they answer to exactly two kinds of value. `LUCENT_SOUL_BEARER_TOKEN` is the
console's, held by the hive site alone for the operator's direct edits, and
reaches every soul. A mind's own lucent credential — the per-holder value its
`LUCENT_BEARER_TOKEN` carries, resolved exactly as `require_holder` resolves
it — reaches that mind's own soul and no other: read, write and history
alike. Every mind can therefore reach this door, and none can reach another
mind's soul through it. The rule is the identity guard's on the graph routes
(a Mind node changes only for the mind its `mind_id` names), held here a
little tighter — the node must also be named as the holder — and judged on
the same row the route then reads or writes. A mind's key that names another
mind, or a name with no Mind node, is a 403 before anything is written.

Every route here now answers the store's own refusals with a status code.
Against a file, a database error was so nearly impossible that `soul.py`
could wrap its whole body in `except Exception` and hand back
`internal_error` on an HTTP 200. Against a database over a socket, a busy
pool, a statement deadline and an unreachable server are routine operating
conditions — and a soul read that returns 200 with an empty body is how a
caller that sends back the whole list wipes a soul while every status check
it makes passes. See `_store_refusal`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import hive_data
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from lucent_api.auth import SoulCaller, require_soul_caller

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/graph/soul",
    tags=["soul"],
    # Router-level as well as per-route, so a route added here later is
    # gated whether or not it asks who is calling. FastAPI resolves the
    # dependency once per request either way.
    dependencies=[Depends(require_soul_caller)],
)


def _decode(payload: str) -> Any:
    """Parse the JSON-string return convention the soul functions inherited.

    A local copy rather than the graph router's, which also translates the
    soul-key refusal: that refusal belongs to the general write routes and
    cannot arise here, and importing it would tie this router — the one door
    a wedged identity is repaired through — to a module it has no other
    reason to load. What this one translates is its own refusal: a mind's
    key naming a soul that is not its own answers 403.
    """
    from lucent_api.soul import NOT_YOUR_SOUL

    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        return {"error": "invalid_json_from_underlying_function",
                "raw": payload}
    # A 403, never a 200 carrying a refusal: the review skill is curl piped
    # into jq, and a mind told 200 about another mind's soul learns nothing.
    if isinstance(body, dict) and body.get("code") == NOT_YOUR_SOUL:
        raise HTTPException(403, body)
    return body


#: What the store's own refusals mean as HTTP, and why each is not a 500.
#:
#: `Conflict` and `MissingReference` are the caller's row being gone or
#: already taken — the request cannot be retried unchanged, so 409.
#: `ValueRefused` is a value the column will not hold, which is the body's
#: fault: 422. The last four are the store, not the request, and the right
#: answer to all of them is "ask again shortly": 503.
_STORE_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (hive_data.Conflict, 409),
    (hive_data.MissingReference, 409),
    (hive_data.ValueRefused, 422),
    (hive_data.TimedOut, 503),
    (hive_data.NoConnectionAvailable, 503),
    (hive_data.Unreachable, 503),
    (hive_data.NotInitialised, 503),
)


def _store_refusal(error: BaseException) -> HTTPException:
    """One of the store's refusals, as a refusal that names the rule.

    The shape this replaces returned `{"ok": false, "code":
    "internal_error"}` at HTTP 200 for every one of these. Three of the
    callers on this path are shell — curl piped into jq with no status check
    — and the review skill builds its whole-list write from the read before
    it. A refusal that returns 200 is a refusal nobody notices, and on this
    particular route the thing nobody notices is a soul being overwritten
    with what a failed read produced.
    """
    for kind, status in _STORE_STATUS:
        if isinstance(error, kind):
            return HTTPException(status, {
                "ok": False,
                "code": "store_refused",
                "refusal": type(error).__name__,
                "detail": str(error),
            })
    return HTTPException(500, {
        "ok": False, "code": "internal_error", "detail": str(error)})


class SoulWriteBody(BaseModel):
    name: str
    soul_values: list[str]
    actor: str
    reason: str
    as_of: str | None = None
    expected_change_count: int | None = None
    allow_shrink: bool = False


@router.get("")
async def soul_get(
    name: str, who: SoulCaller = Depends(require_soul_caller)
) -> Any:
    """Return a mind's soul as it stands in the graph."""
    from lucent_api.soul import soul_read

    try:
        return _decode(await soul_read(name=name, own=who.own))
    except hive_data.HiveDataError as refused:
        log.warning("soul.read.store_refused: %s: %s",
                    type(refused).__name__, refused)
        raise _store_refusal(refused) from refused


@router.post("")
async def soul_post(
    body: SoulWriteBody, who: SoulCaller = Depends(require_soul_caller)
) -> Any:
    """Replace a mind's soul, recording before, after, actor and reason.

    The store's refusals reach the caller as refusals. A write that did not
    land must never answer with a 200: the caller's next read would show the
    old soul and it has no way to tell that from "my edit was a no-op".

    A mind's own key writes as ``mind`` and only as ``mind``: ``console`` in
    the record means the operator edited it from the hive site, and a mind
    that could write that word could make its own change look like his.
    """
    from lucent_api.soul import soul_write

    if not who.is_console and body.actor != "mind":
        raise HTTPException(403, {
            "ok": False, "code": "actor_refused",
            "detail": "a mind's own key writes as actor 'mind'",
        })

    try:
        return _decode(
            await soul_write(
                name=body.name,
                soul_values=body.soul_values,
                actor=body.actor,
                reason=body.reason,
                as_of=body.as_of,
                expected_change_count=body.expected_change_count,
                allow_shrink=body.allow_shrink,
                own=who.own,
            )
        )
    except hive_data.HiveDataError as refused:
        log.warning("soul.write.store_refused: %s: %s",
                    type(refused).__name__, refused)
        raise _store_refusal(refused) from refused


@router.get("/history")
async def soul_history_get(
    name: str, limit: int = 50,
    who: SoulCaller = Depends(require_soul_caller),
) -> Any:
    """Return recorded changes to a mind's soul, newest first."""
    from lucent_api.soul import soul_history

    try:
        return _decode(await soul_history(
            name=name, limit=limit, own=who.own))
    except hive_data.HiveDataError as refused:
        log.warning("soul.history.store_refused: %s: %s",
                    type(refused).__name__, refused)
        raise _store_refusal(refused) from refused
