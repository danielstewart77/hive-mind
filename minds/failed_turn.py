"""Running a mind's rotation check for a turn its harness fired no Stop for.

A per-turn harness that fails — codex reporting `turn.failed`, a dsh process
that timed out, was killed, crashed or wrote no readable report — fires no Stop
hook, so the turn is never measured and a conversation that keeps failing never
rotates. The adapter runs the check itself, as the Stop hook the mind
registered for it, with the payload a Stop would carry plus how the turn ended.
The command is the mind's own, read from its hook configuration rather than
named here, because every mind owns its copy of the check.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any, Iterable

#: How long the check may take to read its input. It detaches its own work.
CHECK_TIMEOUT_SECONDS = 30


def rotation_check_command(stop_groups: Iterable[Any]) -> list[str] | None:
    """The registered Stop command that is the rotation check, as argv."""
    for group in stop_groups or []:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            command = str((hook or {}).get("command") or "") if isinstance(hook, dict) else ""
            if "rotation_check" in command:
                return shlex.split(command)
    return None


async def run(
    command: list[str] | None, *, session_id: str, transcript: Path | None,
    cwd: Path, model: str, error_code: str, env: dict[str, str], log: Any,
) -> None:
    """Hand a failed turn to the check. Never raises: a check that could not
    run costs one measurement, not the turn's report."""
    if not command or not session_id:
        return
    payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": session_id,
        "transcript_path": str(transcript) if transcript else "",
        "cwd": str(cwd),
        "model": model,
        "stop_reason": "error",
        "error_code": error_code,
    }).encode()
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=str(cwd),
        )
        if proc.stdin is not None:
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
        await asyncio.wait_for(proc.wait(), timeout=CHECK_TIMEOUT_SECONDS)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("rotation check for %s did not run: %s", session_id, exc)
