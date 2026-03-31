"""Bilby mind implementation — Claude Code Python SDK.

Bilby uses the claude_code_sdk Python package for programmatic in-process
access to Claude Code. Same model as Ada, different harness: no raw subprocess
management, native Python async API.

Requires: claude-code-sdk>=0.0.25 (pip install claude-code-sdk)
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncGenerator

log = logging.getLogger("hive-mind.minds.bilby")

# Session state: session_id -> {system_prompt, claude_sid, model, mcp_config}
_sessions: dict[str, dict] = {}


async def spawn(
    session_id: str,
    model: str,
    autopilot: bool = False,
    resume_sid: str | None = None,
    surface_prompt: str | None = None,
    allowed_directories: list[str] | None = None,
    soul_file: Path | None = None,
    mind_id: str = "bilby",
    build_base_prompt: Any = None,
    mcp_config: str = "",
    registry: Any = None,
    config_obj: Any = None,
    is_group_session: bool = False,
) -> dict:
    """Initialise Bilby's per-session state. No subprocess — SDK manages the process."""
    base = (
        build_base_prompt(
            allowed_directories=allowed_directories,
            soul_file=soul_file,
            mind_id=mind_id,
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
    log.info("Bilby session %s initialised (resume=%s)", session_id, resume_sid or "new")
    return state


async def send(
    session_id: str,
    content: str,
    images: list[dict] | None = None,
    db: Any = None,
) -> AsyncGenerator[dict, None]:
    """Run one Claude Code SDK turn and yield internal session events.

    Maps SDK message types to our internal NDJSON event format:
      AssistantMessage -> {"type": "assistant", "message": {...}}
      ResultMessage    -> {"type": "result", "session_id": ..., "is_error": ...}
    """
    try:
        from claude_code_sdk import ClaudeCodeOptions, query
        from claude_code_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        log.error("claude_code_sdk not installed — add to requirements.txt and rebuild")
        yield {"type": "result", "is_error": True,
               "errors": ["claude_code_sdk not installed"]}
        return

    state = _sessions.get(session_id)
    if state is None:
        log.error("No state for Bilby session %s", session_id)
        yield {"type": "result", "is_error": True}
        return

    if images:
        log.warning("Bilby session %s: image input not supported by SDK path, ignoring", session_id)

    options = ClaudeCodeOptions(
        append_system_prompt=state["system_prompt"],
        mcp_servers=state["mcp_config"] or {},
        permission_mode="bypassPermissions",
        model=state["model"],
        resume=state.get("claude_sid") or None,
    )

    log.info(
        "Bilby session %s: sending via claude_code_sdk (resume=%s)",
        session_id,
        state.get("claude_sid") or "new",
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
                            # Pass through ToolUseBlock, ThinkingBlock, etc.
                            content_blocks.append(vars(block))

                    if content_blocks:
                        yield {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": content_blocks,
                            },
                        }

                elif isinstance(message, ResultMessage):
                    # Persist session ID for conversation resumption
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
                wait = 2 ** attempt  # 1s, 2s
                log.warning(
                    "Bilby SDK error session %s (attempt %d/%d): %s — retrying in %ds",
                    session_id, attempt + 1, max_retries, exc, wait,
                )
                await asyncio.sleep(wait)
            else:
                log.exception("Bilby SDK error session %s — retries exhausted", session_id)
                yield {"type": "result", "is_error": True}


async def kill(session_id: str) -> None:
    """Clean up Bilby session state."""
    _sessions.pop(session_id, None)
    log.info("Bilby session %s killed", session_id)
