"""Bob mind implementation -- CLI-based spawn/kill via Ollama.

Bob uses the same Claude CLI harness as Ada. The Ollama env var injection
(ANTHROPIC_AUTH_TOKEN=ollama, ANTHROPIC_API_KEY="", ANTHROPIC_BASE_URL)
is handled by the ModelRegistry: when get_provider("gpt-oss:20b-32k") is
called, the model is not in the static map, so it resolves to the ollama
provider whose env_overrides carry the correct values.

The spawn/kill logic is provided by the shared CLI harness (minds.cli_harness).
"""

import logging
from typing import Any

from minds.cli_harness import cli_kill, cli_spawn

log = logging.getLogger("hive-mind.minds.bob")


async def spawn(
    **kwargs: Any,
) -> Any:
    """Spawn a Claude CLI subprocess for Bob.

    Delegates to the shared CLI harness with Bob's logger and default mind_id.
    All keyword arguments are forwarded to cli_spawn().
    """
    kwargs.setdefault("mind_id", "bob")
    kwargs.setdefault("logger", log)
    return await cli_spawn(**kwargs)


async def kill(proc: Any) -> None:
    """Kill a CLI subprocess with SIGTERM, falling back to SIGKILL after 5s."""
    await cli_kill(proc)


# Bob does not have a custom send function -- the session manager
# drives I/O via stdin/stdout on the subprocess directly.
