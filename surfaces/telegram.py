"""Entry point for this stack's Telegram surface container."""

import asyncio

from hive_surfaces import configure, run_telegram_bot

from surfaces import claims
from surfaces.config import surface_config

SURFACE = "telegram"


async def _run() -> None:
    # Before the surface starts, never inside it: the shared core's
    # allow-lists default to empty on purpose, so an unconfigured surface
    # refuses every message rather than answering everyone's.
    configure(surface_config(SURFACE))
    # And the claim before the first long-poll, because adoption is what
    # stops a bot nobody has claimed from refusing its own owner. It retries
    # on the way: nothing orders this container after the gateway.
    claims.install_claim_resolver(SURFACE)
    await claims.adopt_claim(SURFACE)
    await run_telegram_bot()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
