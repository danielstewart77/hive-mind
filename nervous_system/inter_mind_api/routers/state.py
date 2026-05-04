"""Broker state read endpoints — minds list and conversation history.

Opens its own aiosqlite connection to broker.db (separate from the gateway's,
which is fine — SQLite handles concurrent readers, and writes go through the
gateway's /broker/messages flow which owns the wakeup logic).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import aiosqlite
import yaml
from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger(__name__)

router = APIRouter(tags=["state"])

BROKER_DB_PATH = os.environ.get("BROKER_DB_PATH", "/usr/src/app/data/broker.db")
MINDS_DIR = Path(os.environ.get("HIVE_MINDS_DIR", "/usr/src/app/minds"))


def _name_to_mind_id(name: str) -> str | None:
    """Translate a short mind name to its canonical UUID via runtime.yaml.

    Returns None if no matching minds/<name>/runtime.yaml is found.
    """
    rt = MINDS_DIR / name / "runtime.yaml"
    if not rt.exists():
        return None
    try:
        data = yaml.safe_load(rt.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    mid = data.get("mind_id")
    return mid if isinstance(mid, str) else None


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
    """Return a single mind by short name (translated to canonical UUID)."""
    from core.broker import get_mind as _get_mind

    db = await get_db(request)
    mind_id = _name_to_mind_id(name) or name
    mind = await _get_mind(db, mind_id)
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
