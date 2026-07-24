"""Sync inter-mind messaging endpoints — delegate.

Wraps tools.stateful.inter_mind.delegate_to_mind. That function is already
HTTP-shaped (it calls the gateway's /sessions endpoints under the hood
with `requests`) — we run it in a threadpool so it doesn't block the
FastAPI event loop.

Group-chat forwarding has been deprecated and is no longer migrated.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(tags=["messaging"])


def _decode(payload: str) -> Any:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {"error": "invalid_json_from_underlying_function", "raw": payload}


class DelegateBody(BaseModel):
    mind_id: str
    message: str
    mode: str = "verbatim"
    chain: list[str] | None = None


@router.post("/delegate")
async def delegate(body: DelegateBody) -> Any:
    """Synchronous delegation — caller mind asks another mind for a response."""
    from comms.inter_mind_api.inter_mind import delegate_to_mind

    result = await asyncio.to_thread(
        delegate_to_mind,
        body.mind_id,
        body.message,
        body.mode,
        body.chain,
    )
    return _decode(result)
