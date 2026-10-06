"""The Telegram and Discord surfaces every mind runs.

A mind's identity never enters this package. The surfaces talk to the
gateway named by `COMMS_URL` and read their own tokens from the environment,
so what makes one install different from another is its configuration and its
registered commands — not a fork of this code.

Usage from a host:

    from hive_surfaces import SurfaceConfig, configure, run_telegram_bot

    configure(SurfaceConfig(
        default_model=my_config.default_model,
        telegram_allowed_users=my_config.telegram_allowed_users,
    ))
    await run_telegram_bot()

A mind with a command of its own registers it before starting the surface:

    from hive_surfaces import register_command
    register_command("triage", "Work the alert queue", cmd_triage)
"""

from hive_surfaces.config import SurfaceConfig, config, configure, installed

__all__ = [
    "SurfaceConfig",
    "config",
    "configure",
    "installed",
    "register_command",
    "register_discord_command",
    "run_telegram_bot",
    "run_discord_bot",
]


def __getattr__(name: str):
    """Defer the surface imports until one is actually asked for.

    `telegram` and `discord.py` are both declared dependencies, but a mind
    running Telegram-only should not pay the Discord import — and a host that
    only wants `SurfaceConfig` should not pay either.
    """
    if name in ("register_command", "run_telegram_bot"):
        from hive_surfaces import telegram_bot

        return getattr(telegram_bot, name)
    if name in ("register_discord_command", "run_discord_bot"):
        from hive_surfaces import discord_bot

        return getattr(discord_bot, name)
    raise AttributeError(name)
