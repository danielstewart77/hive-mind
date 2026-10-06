"""The configuration this stack hands the shared surfaces.

A surface in this stack differs from an edge mind's in two ways that matter,
and both are expressed here rather than in a fork of the bots:

It runs in its own container, so it shares no memory with its mind backend
and cannot be handed unsolicited turns in process. `proactive_poll_url`
points it at the backend holding them.

Its resident Discord channels are not a list anybody maintains — they are
derived from the scheduled skills that post into them, because the skill is
the only thing that knows its own channel and a second list could disagree
with it. That derivation needs `core.scheduled_skills`, which lives here, so
it is handed over as a callable.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from config import config as stack_config
from core.scheduled_skills import discover_scheduled_skills
from hive_surfaces import SurfaceConfig

MINDS_ROOT = Path(os.environ.get("MINDS_ROOT", "/usr/src/app/minds"))

# Discovery walks every mind's skills directory. That is milliseconds, but it
# would run inside the event loop on every inbound message, so the answer is
# held briefly. A channel added to a skill starts working within the window
# rather than needing a restart.
TASK_CHANNEL_TTL_SECONDS = 60.0

# Keyed on the arguments, not just the time: a cache ignoring them would
# answer a question about one mind's channels with another's.
_task_channel_cache: dict[tuple[str, str], tuple[float, set[int]]] = {}


def resident_channels(minds_root: Path | None = None, mind_id: str | None = None) -> set[int]:
    """Channel ids claimed by a scheduled task of this mind.

    Scoped to this mind when `MIND_ID` is set. Every mind's skills live under
    one root, and a channel claimed by another mind would otherwise become
    mention-free here — so this bot would be nudged into running a skill it
    does not have, and would answer something plausible into that channel
    daily.
    """
    root = MINDS_ROOT if minds_root is None else minds_root
    mind = os.getenv("MIND_ID", "") if mind_id is None else mind_id

    now = time.monotonic()
    key = (str(root), mind)
    cached = _task_channel_cache.get(key)
    if cached and now - cached[0] < TASK_CHANNEL_TTL_SECONDS:
        return cached[1]

    found: set[int] = set()
    for skill in discover_scheduled_skills(root):
        if not skill.discord_channel:
            continue
        if mind and skill.mind_id != mind:
            continue
        found.add(int(skill.discord_channel))
    _task_channel_cache[key] = (now, found)
    return found


def state_dir() -> str:
    """Where this surface keeps its own state, per mind.

    Every surface container in this stack mounts the same project directory,
    so one shared path would have four processes writing one claim file — and
    the claims are what stop a tap Telegram redelivers from acting twice. The
    mind is what separates them, since a surface serves exactly one.

    Not a path relative to the installed core: that lives in site-packages,
    which the next image build replaces, and a forgotten claim is the
    double-tap incident the file exists to prevent.
    """
    override = os.environ.get("SURFACE_STATE_DIR")
    if override:
        return override
    mind = os.getenv("MIND_ID", "") or "unassigned"
    return str(Path("/usr/src/app/data/surfaces") / mind)


def surface_config() -> SurfaceConfig:
    """What this stack's surfaces read, assembled from its own config."""
    return SurfaceConfig(
        default_model=stack_config.default_model,
        telegram_allowed_users=stack_config.telegram_allowed_users,
        telegram_owner_chat_id=getattr(stack_config, "telegram_owner_chat_id", 0),
        discord_allowed_users=stack_config.discord_allowed_users,
        discord_allowed_channels=stack_config.discord_allowed_channels,
        discord_task_channels=resident_channels,
        photo_dir=os.environ.get("TELEGRAM_PHOTO_DIR", "/usr/src/app/data/telegram_photos"),
        state_dir=state_dir(),
        proactive_poll_url=os.environ.get("MIND_BACKEND_URL", ""),
        proactive_poll_interval_s=float(os.environ.get("PROACTIVE_POLL_INTERVAL", "5.0")),
    )
