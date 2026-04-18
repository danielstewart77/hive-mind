"""Codex CLI + Codex models template.

Tested. Based on Nagatha's implementation.
Spawns `codex exec --json --full-auto -` per turn. Stores thread_id
for conversation resumption.
"""

import asyncio
import json
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
    prompt_files: list[str] | None = None,
) -> dict:
    base = (
        build_base_prompt(
            allowed_directories=allowed_directories,
            soul_file=soul_file,
            mind_id=mind_id,
            prompt_files=prompt_files,
        )
        if build_base_prompt
        else ""
    )
    full_prompt = base if not surface_prompt else f"{base}\n\n{surface_prompt}"

    state = {
        "system_prompt": full_prompt,
        "thread_id": resume_sid,
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
    state = _sessions.get(session_id)
    if state is None:
        log.error("No state for session %s", session_id)
        yield {"type": "result", "is_error": True}
        return

    thread_id = state.get("thread_id")

    if thread_id:
        cmd = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "resume", thread_id, "-"]
        stdin_content = content
    else:
        cmd = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-"]
        stdin_content = f"{state['system_prompt']}\n\n---\n\n{content}"

    if images:
        log.warning("Session %s: image input not supported, ignoring", session_id)

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
                        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                    }

        elif etype == "turn.completed":
            await proc.wait()
            yield {"type": "result", "session_id": current_thread_id, "stop_reason": "end_turn", "is_error": False}
            return

        elif etype == "turn.failed":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            log.error("Session %s: turn failed: %s", session_id, error_msg)
            await proc.wait()
            yield {"type": "result", "is_error": True}
            return

    await proc.wait()
    yield {"type": "result", "session_id": current_thread_id, "is_error": False}


async def kill(session_id: str) -> None:
    _sessions.pop(session_id, None)
    log.info("Session %s killed", session_id)
