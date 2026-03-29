"""Ada mind implementation -- CLI-based spawn/send/kill.

Ada uses the Claude CLI in stream-json mode. The spawn/kill logic is
provided by the shared CLI harness (minds.cli_harness).
"""

import logging
from typing import Any

from minds.cli_harness import cli_kill, cli_spawn

log = logging.getLogger("hive-mind.minds.ada")


async def spawn(
    **kwargs: Any,
) -> Any:
    """Spawn a Claude CLI subprocess for Ada.

    Delegates to the shared CLI harness with Ada's logger and default mind_id.
    All keyword arguments are forwarded to cli_spawn().
    """
    kwargs.setdefault("mind_id", "ada")
    kwargs.setdefault("logger", log)
    return await cli_spawn(**kwargs)


async def kill(proc: Any) -> None:
    """Kill a CLI subprocess with SIGTERM, falling back to SIGKILL after 5s."""
    await cli_kill(proc)


# Ada does not have a custom send function -- the session manager
# drives I/O via stdin/stdout on the subprocess directly.
