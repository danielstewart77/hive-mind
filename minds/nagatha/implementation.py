"""Nagatha mind implementation — Codex CLI, one subprocess per turn.

Nagatha uses `codex exec --json --full-auto -` for each turn.
The Codex thread_id (from `thread.started`) is stored as claude_sid so
`codex exec resume <thread_id>` can continue the conversation on respawn.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncGenerator

log = logging.getLogger("hive-mind.minds.nagatha")

# Module-level state: session_id -> {"system_prompt": str, "thread_id": str | None}
_sessions: dict[str, dict] = {}


async def spawn(
    session_id: str,
    model: str,
    autopilot: bool = False,
    resume_sid: str | None = None,
    surface_prompt: str | None = None,
    allowed_directories: list[str] | None = None,
    soul_file: Path | None = None,
    mind_id: str = "nagatha",
    build_base_prompt: Any = None,
    mcp_config: str = "",
    registry: Any = None,
    config_obj: Any = None,
) -> dict:
    """Initialise Nagatha's per-session state. No persistent subprocess."""
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
        "thread_id": resume_sid,  # None on first spawn, populated after first turn
    }
    _sessions[session_id] = state
    log.info(
        "Nagatha session %s initialised (resume=%s)",
        session_id,
        resume_sid or "new",
    )
    return state


async def send(
    session_id: str,
    content: str,
    images: list[dict] | None = None,
    db: Any = None,
) -> AsyncGenerator[dict, None]:
    """Run one Codex CLI turn and yield internal session events."""
    state = _sessions.get(session_id)
    if state is None:
        log.error("No state for Nagatha session %s", session_id)
        yield {"type": "result", "is_error": True}
        return

    thread_id = state.get("thread_id")

    # Build command — resume if we have a prior thread_id
    if thread_id:
        cmd = ["codex", "exec", "--json", "--full-auto", "resume", thread_id, "-"]
        stdin_content = content
    else:
        cmd = ["codex", "exec", "--json", "--full-auto", "-"]
        # Inject system prompt on the first turn only
        stdin_content = f"{state['system_prompt']}\n\n---\n\n{content}"

    if images:
        log.warning("Nagatha session %s: image input not supported, ignoring", session_id)

    log.info(
        "Nagatha session %s: spawning codex turn (thread=%s)",
        session_id,
        thread_id or "new",
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
    )

    proc.stdin.write(stdin_content.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    current_thread_id = thread_id

    async for raw_line in proc.stdout:
        line = raw_line.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")

        if etype == "thread.started":
            current_thread_id = event.get("thread_id")
            state["thread_id"] = current_thread_id
            if db and current_thread_id:
                await db.execute(
                    "UPDATE sessions SET claude_sid = ? WHERE id = ?",
                    (current_thread_id, session_id),
                )

        elif etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    yield {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": text},
                    }

        elif etype == "turn.completed":
            await proc.wait()
            yield {
                "type": "result",
                "session_id": current_thread_id,
                "stop_reason": "end_turn",
                "is_error": False,
            }
            return

        elif etype == "turn.failed":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            log.error("Nagatha session %s: turn failed: %s", session_id, error_msg)
            await proc.wait()
            yield {"type": "result", "is_error": True}
            return

    await proc.wait()
    # Fallback — process exited without a turn.completed event
    yield {"type": "result", "session_id": current_thread_id, "is_error": False}


async def kill(session_id: str) -> None:
    """Clean up Nagatha session state."""
    _sessions.pop(session_id, None)
    log.info("Nagatha session %s killed", session_id)
