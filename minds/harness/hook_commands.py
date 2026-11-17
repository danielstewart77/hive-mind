"""Recognize directly registered, mind-owned Python hook commands."""

from __future__ import annotations

import shlex
from pathlib import Path


def _is_python_launcher(argument: str) -> bool:
    """Whether *argument* is one of the Python launchers hooks support."""
    name = Path(argument).name.lower().removesuffix(".exe")
    if name in {"py", "python", "python3"}:
        return True
    if not name.startswith("python3."):
        return False
    return all(part.isdigit() for part in name.removeprefix("python3.").split("."))


def managed_python_command(
    command: object, *, script_path: Path | str, actions: tuple[str, ...] | None = None,
) -> list[str] | None:
    """Return a direct owned command's parsed argv, or ``None``.

    Ownership is deliberately structural: a supported Python launcher must
    directly invoke the exact registered script path.  Callers that own native
    commands also provide the permitted direct action at argv[2].
    """
    if not isinstance(command, str) or not command:
        return None
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    required = 3 if actions is not None else 2
    if len(arguments) < required or not _is_python_launcher(arguments[0]):
        return None
    try:
        if Path(arguments[1]).resolve() != Path(script_path).resolve():
            return None
    except (OSError, ValueError):
        return None
    if actions is not None and arguments[2] not in actions:
        return None
    return arguments
