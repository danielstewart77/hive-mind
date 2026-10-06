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

Upgrading needs `--force-reinstall --no-deps`. Every mind tracks `main`, so the
version never changes — and pip, having cloned the repo and resolved `main` to
a new commit, compares that unchanged version against what is installed and
installs nothing. `--upgrade` alone does not help for the same reason. So a
mind that re-ran its install would stay on whatever commit it first got, with
no error and nothing in the output to say so:

```
pip install --force-reinstall --no-deps \
    "hive-surfaces @ git+https://github.com/danielstewart77/hive-surfaces.git@main"
pip install -r requirements.txt   # resolve anything the new core now needs
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

`discord_task_channels` takes either the ids or a callable returning them.
Two hosts answer "which channels is this mind resident in" differently and
both are right: an edge mind names them in its own `config.yaml`, while the
stack derives them from the scheduled skills that post into them. The callable
is asked per message, so a channel added to a skill starts working without a
restart, and a resolver that fails reads as no resident channels — asking to be
named is quieter than answering strangers in rooms nobody addressed the mind
in.

An empty allow-list authorizes nobody. A host that forgets to call
`configure` gets a surface that refuses every message rather than one that
answers everyone.

`configure` called with a `SurfaceConfig` installs that object whole — a host
handing one over is stating all of it. Called with keywords alone, it changes
only the fields it names: a host that configures its allow-lists at boot and
later names a new default model would otherwise have the second call blank the
allow-list back to nobody.

`photo_dir` and `state_dir` are the host's to give. Their defaults sit under
the working directory rather than beside this package, because installed the
package lives in site-packages — unwritable in some deployments, and wiped by
the next reinstall, which would forget every single-use claim the session
picker holds and let a tap Telegram redelivers act a second time.

`models_catalog` is an async callable returning the rows `/models` lists,
because the catalog is relayed from whatever inference proxy the host talks
to. A mind that supplies none reports that it offers no model list.

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
| `HIVE_TOOLS_URL` | Where held decisions are reported; defaults to localhost |
| `HIVE_TOOLS_TOKEN` | Bearer for that report; absent sends no header |

## Unsolicited turns

A turn the mind produced with nobody listening reaches the chat one of two
ways, and both end at the same delivery path — same chunking, same backoff,
same journalling of whatever is still pending at shutdown.

A surface sharing a process with its mind backend is handed them in memory
through `hive_surfaces.proactive`. A surface in its own container cannot be
handed anything, so it polls: set `proactive_poll_url` to the backend and the
poller puts what it finds on the same queue. Empty means in-process only.

## Held decisions

Some tools wait for a person. hive-tools mints a token, posts the question
with two buttons, and this reports which one was tapped. It decides nothing —
the message ends up saying what hive-tools said, status and all, because the
question after a failed approval is always whether the thing actually
happened.
