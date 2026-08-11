"""The sanctioned door to a mind's soul.

Its own router rather than three more paths on the graph router, because
the graph router carries ``require_bearer`` as a router-level dependency
and a request presents exactly one ``Authorization`` header. Mounting these
routes there would mean the soul token is rejected before it is read, or
that the service token is accepted here — and the second of those is the
bug this whole boundary exists to remove.
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
        )
    )


@router.get("/history")
def soul_history_get(name: str, limit: int = 50) -> Any:
    """Return recorded changes to a mind's soul, newest first."""
    from lucent_api.soul import soul_history

    return _decode(soul_history(name=name, limit=limit))
