"""Claude SDK + Claude models template.

Tested. Based on Bilby's implementation.
Uses the claude_code_sdk Python package — no subprocess management.
Requires: claude-code-sdk>=0.0.25
"""

import asyncio
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
    prompt_profile: str = "default",
    harness: str = "",
) -> dict:
    base = (
        build_base_prompt(
            allowed_directories=allowed_directories,
            soul_file=soul_file,
            mind_id=mind_id,
            harness=harness,
            prompt_profile=prompt_profile,
        )
        if build_base_prompt
        else ""
    )
    full_prompt = base if not surface_prompt else f"{base}\n\n{surface_prompt}"

    state = {
        "system_prompt": full_prompt,
        "claude_sid": resume_sid,
        "model": model,
        "mcp_config": mcp_config,
    }
    _sessions[session_id] = state
    log.info("Session %s initialised (resume=%s)", session_id, resume_sid or "new")
    return state


async def send(
    session_id: str,
    content: str,
    images: list[dict] | None = None,
    db: Any = None,
) -> AsyncGenerator[dict, None]:
    try:
        from claude_code_sdk import ClaudeCodeOptions, query
        from claude_code_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        log.error("claude_code_sdk not installed")
        yield {"type": "result", "is_error": True, "errors": ["claude_code_sdk not installed"]}
        return

    state = _sessions.get(session_id)
    if state is None:
        log.error("No state for session %s", session_id)
        yield {"type": "result", "is_error": True}
        return

    if images:
        log.warning("Session %s: image input not supported by SDK path, ignoring", session_id)

    options = ClaudeCodeOptions(
        append_system_prompt=state["system_prompt"],
        mcp_servers=state["mcp_config"] or {},
        permission_mode="bypassPermissions",
        model=state["model"],
        resume=state.get("claude_sid") or None,
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async for message in query(prompt=content, options=options):
                if isinstance(message, AssistantMessage):
                    content_blocks = []
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            content_blocks.append({"type": "text", "text": block.text})
                        else:
                            content_blocks.append(vars(block))
                    if content_blocks:
                        yield {
                            "type": "assistant",
                            "message": {"role": "assistant", "content": content_blocks},
                        }
                elif isinstance(message, ResultMessage):
                    if message.session_id:
                        state["claude_sid"] = message.session_id
                        if db:
                            await db.execute(
                                "UPDATE sessions SET claude_sid = ? WHERE id = ?",
                                (message.session_id, session_id),
                            )
                            await db.commit()
                    yield {
                        "type": "result",
                        "session_id": message.session_id,
                        "stop_reason": message.subtype,
                        "is_error": message.is_error,
                    }
                    return
        except Exception as exc:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning("SDK error session %s (attempt %d/%d): %s — retrying in %ds",
                            session_id, attempt + 1, max_retries, exc, wait)
                await asyncio.sleep(wait)
            else:
                log.exception("SDK error session %s — retries exhausted", session_id)
                yield {"type": "result", "is_error": True}


async def kill(session_id: str) -> None:
    _sessions.pop(session_id, None)
    log.info("Session %s killed", session_id)
