"""Per-process bot helpers — chat-id serialization and UI formatting.

In-process ``asyncio`` primitives keyed by chat-id, so two incoming
messages from the same chat can't both kick off Claude in parallel.
Lives in the bot's Python process; not shared via NS.
"""

import asyncio
import json
import os
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

_locks: dict[int, asyncio.Lock] = {}
_chat_queues: dict[int, asyncio.Queue] = {}

# Picker messages whose one action has already been taken.
#
# A picker is single use. The enforcement the operator actually sees is the
# keyboard being removed — a message with no buttons cannot produce a tap at
# all — but that removal is an HTTP call to Telegram, and the whole reason
# this exists is that those calls fail. So the claim is taken *here* first,
# synchronously, before anything is awaited: two taps that arrive before the
# edit lands are ordered by this dict, not by the network.
#
# **It is written to disk**, because the incident it exists to prevent runs
# straight through a restart. Polling goes down, taps queue at Telegram's end,
# and the usual way polling comes back is the mind's own service restarting —
# whereupon `getUpdates` redelivers every queued tap to a fresh process. With
# the claims in memory only, both taps claim successfully and the second ends
# the conversation the first just created, which is exactly 2026-09-17.
_MAX_REMEMBERED_PICKERS = 512


def _state_path() -> Path:
    """Where the claims live. Read per call, not frozen at import.

    `PICKER_STATE_PATH` points it elsewhere for an install that keeps state
    outside the checkout — and resolving it each time is what lets a test aim
    it at a temp directory without depending on when this module happened to
    be imported.

    Otherwise it is the host's configured `state_dir`, never a path derived
    from this module's own location: installed as a package that is inside
    site-packages, which the next reinstall wipes — so every claim the file
    held is forgotten and a tap Telegram redelivers across a restart claims
    successfully a second time, which is the whole incident above.
    """
    override = os.environ.get("PICKER_STATE_PATH")
    if override:
        return Path(override)
    from hive_surfaces.config import state_root

    return state_root() / "spent_pickers.json"
_spent_pickers: "OrderedDict[tuple[int, int], None]" = OrderedDict()
_loaded = False


def _load() -> None:
    """Read the claims left by the previous process, once.

    Unreadable, absent or corrupt all mean the same thing here and are not
    fatal: the claim degrades to in-memory, which is what it was before. A
    picker failing to be remembered across a restart is the old behaviour; a
    bot that will not start is worse than either.
    """
    global _loaded
    _loaded = True
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if not isinstance(raw, list):
        return
    for entry in raw[-_MAX_REMEMBERED_PICKERS:]:
        try:
            chat_id, message_id = entry
            _spent_pickers[(int(chat_id), int(message_id))] = None
        except Exception:  # noqa: BLE001
            continue


def _persist() -> None:
    """Write the claims out, atomically and never fatally."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps([[c, m] for c, m in _spent_pickers]), encoding="utf-8"
        )
        # Replaced rather than truncated, so a crash mid-write cannot leave a
        # half-written file that reads as "nothing is claimed".
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def claim_picker(chat_id: int, message_id: int) -> bool:
    """Take the single action a picker message is allowed, if it is still there.

    ``True`` means this caller owns the tap and should act on it. ``False``
    means the picker was already used and this tap must do nothing but say so.

    Contains no ``await`` by construction. An async claim would let two taps
    on one picker both read "unclaimed" before either wrote, which is the
    precise race that let a second ``New session`` tap destroy the
    conversation the first one had just created.
    """
    if not _loaded:
        _load()
    key = (int(chat_id), int(message_id))
    if key in _spent_pickers:
        return False
    _spent_pickers[key] = None
    while len(_spent_pickers) > _MAX_REMEMBERED_PICKERS:
        _spent_pickers.popitem(last=False)
    _persist()
    return True


def _reset_pickers() -> None:
    """Test helper — forget every claim so a test starts from a clean slate."""
    global _loaded
    _spent_pickers.clear()
    _loaded = True


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


def get_queue(chat_id: int) -> asyncio.Queue:
    if chat_id not in _chat_queues:
        _chat_queues[chat_id] = asyncio.Queue()
    return _chat_queues[chat_id]


def time_ago(ts: float) -> str:
    """Render a unix timestamp as a relative string ("5 min ago"). UI helper."""
    delta = datetime.now().timestamp() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)} min ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"
