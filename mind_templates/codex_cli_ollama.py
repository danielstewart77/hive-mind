# UNTESTED — scaffold only. Validate before production use.
"""Codex CLI + Ollama models template.

Not yet tested. Scaffolded from codex_cli_codex.py.
Expected to work with Ollama-provided models via the Codex CLI,
but Codex's Ollama compatibility has not been verified.
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
        if build_base_prompt else ""
    )
    full_prompt = base if not surface_prompt else f"{base}\n\n{surface_prompt}"
    state = {"system_prompt": full_prompt, "thread_id": resume_sid}
    _sessions[session_id] = state
    log.info("Session %s initialised (resume=%s)", session_id, resume_sid or "new")
    return state


async def send(session_id: str, content: str, images: list[dict] | None = None, db: Any = None) -> AsyncGenerator[dict, None]:
    state = _sessions.get(session_id)
    if state is None:
        yield {"type": "result", "is_error": True}
        return

    thread_id = state.get("thread_id")
    if thread_id:
        cmd = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "resume", thread_id, "-"]
        stdin_content = content
    else:
        cmd = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-"]
        stdin_content = f"{state['system_prompt']}\n\n---\n\n{content}"

    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=10*1024*1024)
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
                await db.execute("UPDATE sessions SET claude_sid = ? WHERE id = ?", (current_thread_id, session_id))
        elif etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message" and item.get("text"):
                yield {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": item["text"]}]}}
        elif etype == "turn.completed":
            await proc.wait()
            yield {"type": "result", "session_id": current_thread_id, "stop_reason": "end_turn", "is_error": False}
            return
        elif etype == "turn.failed":
            await proc.wait()
            yield {"type": "result", "is_error": True}
            return
    await proc.wait()
    yield {"type": "result", "session_id": current_thread_id, "is_error": False}


async def kill(session_id: str) -> None:
    _sessions.pop(session_id, None)
