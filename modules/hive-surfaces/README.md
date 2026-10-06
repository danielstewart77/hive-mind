# hive-surfaces

The Telegram and Discord surfaces every hive mind runs.

A mind's identity never enters this package. The surfaces talk to the gateway
named by `COMMS_URL` and read their own tokens from the environment, so what
makes one install different from another is its configuration and its
registered commands — not a fork of this code.

Every mind tracks `main`. There are no pinned versions to carry per install,
which means the test suite here is a gate and not an oracle: nothing merges
red, because `main` is what every surface in the hive is running.

## Install

```
pip install git+https://github.com/danielstewart77/hive-surfaces.git
```

## Use

```python
from hive_surfaces import SurfaceConfig, configure, run_telegram_bot

configure(SurfaceConfig(
    default_model=my_config.default_model,
    telegram_allowed_users=my_config.telegram_allowed_users,
))
await run_telegram_bot()
```

An empty allow-list authorizes nobody. A host that forgets to call
`configure` gets a surface that refuses every message rather than one that
answers everyone.

## A mind's own commands

The core owns the process, the handler table and the slash menu. A mind with a
command no other mind has registers it before the surface starts:

```python
from hive_surfaces import register_command, register_discord_command

register_command("triage", "Work the alert queue", cmd_triage)
register_discord_command("triage", "Work the alert queue", cmd_triage)
```

Registering after the surface is built raises. A dropped registration leaves
the command in neither the menu nor the handler table — or in one and not the
other — and the mind looks like it lost a feature for no reason anybody can
see.

## Environment

| Variable | Purpose |
|---|---|
| `COMMS_URL` | The hive-comms gateway this surface talks to |
| `TELEGRAM_BOT_TOKEN` | Telegram surface; unset disables it |
| `DISCORD_BOT_TOKEN` | Discord surface; unset disables it |
| `MIND_ID` | The mind this surface is a surface of |
