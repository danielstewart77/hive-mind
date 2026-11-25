# hive-surfaces

The Telegram and Discord surfaces every hive mind runs.

A mind's identity never enters this package. The surfaces talk to the gateway
named by `COMMS_URL` and read their own tokens from the environment, so what
makes one install different from another is its configuration and its
registered commands — not a fork of this code.

Every install names a version. The package lives in hive-mind
(`modules/hive-surfaces`), its version is in `pyproject.toml`, and a release is
the tag `hive-surfaces-vX.Y.Z` on hive-mind, equal to that number (the rule is
in `docs/setup/hive-data-versions.md`; the suite here is a gate for the commit
a tag names). A change to the package's code raises the version in the same
pull request, so a newer version is a plain upgrade: no `--force-reinstall`.

## Install

Both forms install this package alone, at a named version. Replace `0.2.0`.

From a wheel (needs only pip; no git, no clone, the exact artefact the release
published):

```
pip install "https://github.com/danielstewart77/hive-mind/releases/download/hive-surfaces-v0.2.0/hive_surfaces-0.2.0-py3-none-any.whl"
```

From git (needs pip and git). pip's subdirectory form initialises every
submodule of hive-mind it can reach, so this clones more than the package and
can ask for credentials a machine does not have; prefer the wheel there:

```
pip install "hive-surfaces @ git+https://github.com/danielstewart77/hive-mind.git@hive-surfaces-v0.2.0#subdirectory=modules/hive-surfaces"
```

Upgrading is the same line with a newer version, with `--upgrade`.

Inside hive-mind the mind image installs the copy in the checkout through the
`hive_surfaces` named build context; nothing is fetched at build time.

## Cutting a release

From a clean checkout of the merge commit, with `pyproject.toml` at the new
number:

```
python scripts/build-package-release.py hive-surfaces-vX.Y.Z   # builds dist/ locally; refuses if the number differs from the tag
git tag -a hive-surfaces-vX.Y.Z -m "hive-surfaces X.Y.Z"
git push origin hive-surfaces-vX.Y.Z
```

The push runs `.github/workflows/hive-surfaces-release.yml`, which builds the
same wheel and sdist and attaches them to the GitHub release of that tag. No
other tag starts it.

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

## The surface's own token

`_get_bot_token` reads `TELEGRAM_BOT_TOKEN` from the environment, unless
`TELEGRAM_BOT_TOKEN_KEYRING_KEY` names a keyring key — in which case that key
wins, because the only reason to name one is that this surface's token is not
the ambient one. An edge mind runs one bot per host and puts it in `.env`; a
stack runs several from one image on one machine, where the environment cannot
hold several values under one name.

`hive_surfaces.token_store` is the writing half of that same rule, used by a
host's own admin-guarded route — a console cannot reach either place, and only
the mind's filesystem can see both. `replace` verifies against the bot API
before storing, so a refused token leaves the working one in place; `status`
reports stored-and-accepted (named by the bot it authenticates as), stored-and-
refused, or nothing stored, and never the token itself. Both live here rather
than in a host, because a writer that disagreed with the reader would store a
token where nothing consults it.

## Environment

| Variable | Purpose |
|---|---|
| `COMMS_URL` | The hive-comms gateway this surface talks to |
| `TELEGRAM_BOT_TOKEN` | Telegram surface; unset disables it |
| `DISCORD_BOT_TOKEN` | Discord surface; unset disables it |
| `MIND_ID` | The mind this surface is a surface of |
| `HIVE_TOOLS_URL` | Where held decisions are reported; defaults to localhost |
| `HIVE_TOOLS_APPROVER_TOKEN` | Bearer for that report: hive-tools' approver credential (holder `hitl-approver`), never a mind's token; absent sends no header |
| `HIVE_TOOLS_TOKEN` | A mind's hive-tools token, for `/models`; never sent with a decision |

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
with two buttons, and this reports which one was tapped, under
`HIVE_TOOLS_APPROVER_TOKEN` — hive-tools refuses a decision made with a mind's
token, so only the bot that receives hive-tools' buttons needs it, and a host
whose mind shares the bot's process should not be given it. It decides nothing —
the message ends up saying what hive-tools said, status and all, because the
question after a failed approval is always whether the thing actually
happened.
