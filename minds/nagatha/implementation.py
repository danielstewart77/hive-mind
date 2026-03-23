"""Nagatha mind implementation — Anthropic SDK-based spawn/send/kill.

Nagatha uses the Anthropic Python SDK (anthropic.AsyncAnthropic) for
direct API communication instead of the CLI subprocess model.
"""

import logging
from pathlib import Path
from typing import Any, AsyncGenerator

log = logging.getLogger("hive-mind.minds.nagatha")

# Model name mappings from short aliases to full API model IDs
_MODEL_MAP: dict[str, str] = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-20250514",
}

# Session state: session_id -> {client, model, system_prompt, messages}
_sessions: dict[str, dict[str, Any]] = {}


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
) -> object:
    """Create an Anthropic SDK client for this session.

    Args:
        session_id: Unique session identifier.
        model: Model name (e.g. 'sonnet', 'opus').
        build_base_prompt: Callable to build the system prompt.
        Other args: Accepted for interface compatibility with Ada.

    Returns:
        A sentinel object representing the session (not a subprocess).
    """
    import anthropic

    base = build_base_prompt(
        allowed_directories=allowed_directories,
        soul_file=soul_file,
        mind_id=mind_id,
    ) if build_base_prompt else ""
    full_prompt = base if not surface_prompt else f"{base}\n\n{surface_prompt}"

    client = anthropic.AsyncAnthropic()
    api_model = _MODEL_MAP.get(model, model)

    _sessions[session_id] = {
        "client": client,
        "model": api_model,
        "system_prompt": full_prompt,
        "messages": [],
    }

    log.info(
        "Spawned SDK session for %s (model=%s, mind=%s)",
        session_id, api_model, mind_id,
    )
    return _sessions[session_id]


async def send(
    session_id: str,
    content: str,
    images: list[dict] | None = None,
    **kwargs: Any,
) -> AsyncGenerator[dict, None]:
    """Send a message via the Anthropic SDK and yield response events.

    Yields events in the same NDJSON format as the CLI:
    - {"type": "assistant", "message": {"role": "assistant", "content": [...]}}
    - {"type": "result", "result": "...", "session_id": None}
    """
    state = _sessions.get(session_id)
    if not state:
        yield {
            "type": "result",
            "is_error": True,
            "errors": [f"No SDK session found for {session_id}"],
            "result": "",
            "session_id": None,
        }
        return

    client = state["client"]
    model = state["model"]
    system_prompt = state["system_prompt"]
    messages = state["messages"]

    # Build user message
    if images:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": content}]
        for img in images:
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            })
    else:
        user_content = [{"type": "text", "text": content}]

    messages.append({"role": "user", "content": user_content})

    try:
        full_text = ""
        async with client.messages.stream(
            model=model,
            system=system_prompt,
            messages=messages,
            max_tokens=8192,
        ) as stream:
            async for text in stream.text_stream:
                full_text += text
                yield {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }

        # Append assistant reply to conversation history
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": full_text}],
        })

        yield {
            "type": "result",
            "result": full_text,
            "session_id": None,
        }

    except Exception:
        log.exception("SDK send error for session %s", session_id)
        yield {
            "type": "result",
            "is_error": True,
            "errors": ["SDK communication error — see server logs"],
            "result": "",
            "session_id": None,
        }


async def kill(session_id: str) -> None:
    """Clean up SDK session state."""
    removed = _sessions.pop(session_id, None)
    if removed:
        log.info("Killed SDK session %s", session_id)
