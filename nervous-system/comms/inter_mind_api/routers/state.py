"""Broker state read endpoints — minds list and conversation history.

Reads through `hive_data`, the same named operations the gateway uses.
Writes go through the gateway's /broker/messages flow, which owns the wakeup
logic.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

log = logging.getLogger(__name__)

router = APIRouter(tags=["state"])


@router.get("/minds")
async def list_minds() -> Any:
    """Return all registered minds with their gateway URLs and metadata."""
    from comms.broker import get_registered_minds

    return await get_registered_minds()


@router.get("/minds/{name}")
async def get_mind(name: str) -> Any:
    """Return a single mind by name."""
    from comms.broker import get_mind as _get_mind

    mind = await _get_mind(name)
    if mind is None:
        raise HTTPException(status_code=404, detail=f"Mind '{name}' not registered")
    return mind


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> Any:
    """Return all messages in a conversation in order."""
    from comms.broker import get_messages

    messages = await get_messages(conversation_id)
    return {"conversation_id": conversation_id, "messages": messages}
