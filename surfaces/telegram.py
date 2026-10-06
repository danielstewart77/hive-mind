"""Entry point for this stack's Telegram surface container."""

import asyncio

from hive_surfaces import configure, run_telegram_bot

from surfaces.config import surface_config


def main() -> None:
    # Before the surface starts, never inside it: the shared core's
    # allow-lists default to empty on purpose, so an unconfigured surface
    # refuses every message rather than answering everyone's.
    configure(surface_config())
    asyncio.run(run_telegram_bot())


if __name__ == "__main__":
    main()
