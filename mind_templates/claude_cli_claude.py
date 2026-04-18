"""Claude CLI + Claude models template.

Tested. Based on Ada's implementation.
Spawns a `claude` CLI subprocess in stream-json mode.
"""

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


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
    logger: logging.Logger | None = None,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    _log = logger or log

    base = build_base_prompt(
        allowed_directories=allowed_directories,
        mind_id=mind_id,
        prompt_files=prompt_files,
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
    _log.info("Spawned CLI process for session %s (pid=%d, model=%s)", session_id, proc.pid, model)
    return proc


async def kill(proc: Any) -> None:
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
