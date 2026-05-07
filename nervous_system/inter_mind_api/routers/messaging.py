"""Sync inter-mind messaging endpoints — delegate and group-chat forward.

These wrap the existing tools.stateful.inter_mind.delegate_to_mind and
tools.stateful.group_chat.forward_to_mind functions. Those functions are
already HTTP-shaped (they call the gateway's /sessions endpoints under the
hood with `requests`) — we run them in a threadpool so they don't block
the FastAPI event loop.
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


class ForwardBody(BaseModel):
    mind_id: str
    message: str
    group_session_id: str


@router.post("/delegate")
async def delegate(body: DelegateBody) -> Any:
    """Synchronous delegation — caller mind asks another mind for a response."""
    from nervous_system.inter_mind_api.inter_mind import delegate_to_mind

    result = await asyncio.to_thread(
        delegate_to_mind,
        body.mind_id,
        body.message,
        body.mode,
        body.chain,
    )
    return _decode(result)


@router.post("/forward")
async def forward(body: ForwardBody) -> Any:
    """Group-chat forward — route a message to a specific mind in a group session."""
    from nervous_system.inter_mind_api.group_chat import forward_to_mind

    result = await asyncio.to_thread(
        forward_to_mind,
        body.mind_id,
        body.message,
        body.group_session_id,
    )
    return _decode(result)
