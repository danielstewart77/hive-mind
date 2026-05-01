"""Broker state read endpoints — minds list and conversation history.

Opens its own aiosqlite connection to broker.db (separate from the gateway's,
which is fine — SQLite handles concurrent readers, and writes go through the
gateway's /broker/messages flow which owns the wakeup logic).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger(__name__)

router = APIRouter(tags=["state"])

BROKER_DB_PATH = os.environ.get("BROKER_DB_PATH", "/usr/src/app/data/broker.db")


async def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.broker_db


@router.get("/minds")
async def list_minds(request: Request) -> Any:
    """Return all registered minds with their gateway URLs and metadata."""
    from core.broker import get_registered_minds

    db = await get_db(request)
    return await get_registered_minds(db)


@router.get("/minds/{name}")
async def get_mind(request: Request, name: str) -> Any:
    """Return a single mind by name."""
    from core.broker import get_mind as _get_mind

    db = await get_db(request)
    mind = await _get_mind(db, name)
    if mind is None:
        raise HTTPException(status_code=404, detail=f"Mind '{name}' not registered")
    return mind


@router.get("/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str) -> Any:
    """Return all messages in a conversation in order."""
    from core.broker import get_messages

    db = await get_db(request)
    messages = await get_messages(db, conversation_id)
    return {"conversation_id": conversation_id, "messages": messages}
