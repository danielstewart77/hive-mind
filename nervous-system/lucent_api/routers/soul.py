"""The sanctioned door to a mind's soul.

Its own router rather than three more paths on the graph router, because
the graph router carries ``require_holder`` as a router-level dependency
and a request presents exactly one ``Authorization`` header. Mounting these
routes there would mean the soul token is rejected before it is read, or
that the service token is accepted here — and the second of those is the
bug this whole boundary exists to remove.

The soul routes are deliberately outside the graph's owner partition. They
answer to `LUCENT_SOUL_BEARER_TOKEN`, which is held by the console and by the
operator's own review flow and by no mind — so there is no caller here to
scope, and the one credential that reaches them is the one that exists to fix
a wedged identity from outside. The partition's subject is a mind reaching
another person's graph; this route is not reachable by a mind at all.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from lucent_api.auth import require_soul_bearer
from lucent_api.routers.graph import _decode

router = APIRouter(
    prefix="/graph/soul",
    tags=["soul"],
    dependencies=[Depends(require_soul_bearer)],
)


class SoulWriteBody(BaseModel):
    name: str
    soul_values: list[str]
    actor: str
    reason: str
    as_of: str | None = None
    expected_change_count: int | None = None
    allow_shrink: bool = False


@router.get("")
def soul_get(name: str) -> Any:
    """Return a mind's soul as it stands in the graph."""
    from lucent_api.soul import soul_read

    return _decode(soul_read(name=name))


@router.post("")
def soul_post(body: SoulWriteBody) -> Any:
    """Replace a mind's soul, recording before, after, actor and reason."""
    from lucent_api.soul import soul_write

    return _decode(
        soul_write(
            name=body.name,
            soul_values=body.soul_values,
            actor=body.actor,
            reason=body.reason,
            as_of=body.as_of,
            expected_change_count=body.expected_change_count,
            allow_shrink=body.allow_shrink,
        )
    )


@router.get("/history")
def soul_history_get(name: str, limit: int = 50) -> Any:
    """Return recorded changes to a mind's soul, newest first."""
    from lucent_api.soul import soul_history

    return _decode(soul_history(name=name, limit=limit))
