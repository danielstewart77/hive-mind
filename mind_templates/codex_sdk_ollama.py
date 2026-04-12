# UNTESTED — scaffold only. Validate before production use.
"""Codex SDK + Ollama models template.

Not yet tested. Codex does not currently have a Python SDK, and
Ollama compatibility is unverified. This scaffold assumes a future
SDK with a similar interface. Do not use until a Codex SDK exists.
"""

import logging
from pathlib import Path
from typing import Any, AsyncGenerator

log = logging.getLogger(__name__)

_sessions: dict[str, dict] = {}


async def spawn(
    session_id: str,
    model: str,
    autopilot: bool = False,
    resume_sid: str | None = None,
    surface_prompt: str | None = None,
    allowed_directories: list[str] | None = None,
    soul_file: Path | None = None,
    mind_id: str = "MIND_NAME",
    build_base_prompt: Any = None,
    mcp_config: str = "",
    registry: Any = None,
    config_obj: Any = None,
    is_group_session: bool = False,
) -> dict:
    base = (
        build_base_prompt(allowed_directories=allowed_directories, soul_file=soul_file, mind_id=mind_id)
        if build_base_prompt else ""
    )
    full_prompt = base if not surface_prompt else f"{base}\n\n{surface_prompt}"
    state = {"system_prompt": full_prompt, "thread_id": resume_sid, "model": model, "mcp_config": mcp_config}
    _sessions[session_id] = state
    log.info("Session %s initialised (resume=%s)", session_id, resume_sid or "new")
    return state


async def send(session_id: str, content: str, images: list[dict] | None = None, db: Any = None) -> AsyncGenerator[dict, None]:
    raise NotImplementedError("Codex SDK does not exist yet. Use codex_cli_ollama template instead.")


async def kill(session_id: str) -> None:
    _sessions.pop(session_id, None)
