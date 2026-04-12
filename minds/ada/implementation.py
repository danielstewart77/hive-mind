"""Ada mind implementation -- CLI-based spawn/send/kill.

Ada uses the Claude CLI in stream-json mode. Fully standalone -- all
spawn/kill logic is inlined (no shared imports between minds).
"""

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any

log = logging.getLogger("hive-mind.minds.ada")


async def spawn(
    session_id: str,
    model: str,
    autopilot: bool = False,
    resume_sid: str | None = None,
    surface_prompt: str | None = None,
    allowed_directories: list[str] | None = None,
    soul_file: Path | None = None,
    mind_id: str = "ada",
    build_base_prompt: Any = None,
    mcp_config: str = "",
    registry: Any = None,
    config_obj: Any = None,
    is_group_session: bool = False,
    logger: logging.Logger | None = None,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Spawn a Claude CLI subprocess for Ada.

    Args:
        session_id: Unique session identifier.
        model: Model name to use (e.g. 'sonnet', 'opus').
        autopilot: Whether to enable dangerously-skip-permissions.
        resume_sid: Claude session ID to resume.
        surface_prompt: Additional prompt for the surface (client type).
        allowed_directories: Directories to grant access to.
        soul_file: Path to the soul file for this mind.
        mind_id: Mind identifier.
        build_base_prompt: Callable to build the base system prompt.
        mcp_config: Path to the MCP config file.
        registry: ModelRegistry instance.
        config_obj: HiveMindConfig instance.
        is_group_session: Whether this is a group chat session.
        logger: Logger instance to use (defaults to module logger).

    Returns:
        The spawned asyncio subprocess.
    """
    _log = logger or log

    base = build_base_prompt(
        allowed_directories=allowed_directories,
        mind_id=mind_id,
    ) if build_base_prompt else ""
    full_prompt = base if not surface_prompt else f"{base}\n\n{surface_prompt}"

    cmd = [
        "claude", "-p",
        "--verbose",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--model", model,
        "--mcp-config", mcp_config,
        "--append-system-prompt", full_prompt,
    ]
    if autopilot:
        if config_obj:
            cmd.extend(["--max-budget-usd", str(config_obj.autopilot_guards.max_budget_usd)])
    for d in allowed_directories or []:
        cmd.extend(["--allowedDirectory", d])
    if resume_sid:
        cmd.extend(["--resume", resume_sid])

    provider = registry.get_provider(model) if registry else None
    env = os.environ.copy()
    if provider:
        env.update(provider.env_overrides)
    if is_group_session:
        env["HIVEMIND_GROUP_SESSION"] = "1"

    from config import PROJECT_DIR

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
        env=env,
        cwd=str(PROJECT_DIR),
    )
    _log.info(
        "Spawned CLI process for session %s (pid=%d, model=%s)",
        session_id, proc.pid, model,
    )
    return proc


async def kill(proc: Any) -> None:
    """Kill a CLI subprocess with SIGTERM, falling back to SIGKILL after 5s."""
    if proc and proc.returncode is None:
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass


# Ada does not have a custom send function -- the session manager
# drives I/O via stdin/stdout on the subprocess directly.
