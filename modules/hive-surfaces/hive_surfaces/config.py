"""The configuration the surfaces read, and the host's way of supplying it.

A surface needs to know who may talk to it and which model to name in
`/status`. It does not need — and must never acquire — the rest of a mind's
configuration: the providers, the autopilot guards, the server port. A core
that read the host's whole config object would be a core that could only be
installed into a host shaped exactly like the one it was lifted from.

So the host hands over the fields the surfaces actually read, and nothing
else. Every field has a default that is safe rather than convenient: an empty
allow-list authorizes nobody, because a core installed into a host that forgot
to call `configure` must refuse every message rather than answer all of them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class SurfaceConfig:
    """What the Telegram and Discord surfaces read from their host."""

    # Named in `/status` and `/model`. The surfaces never default a model
    # themselves — this is the host's answer to "what is this mind pointed at".
    default_model: str = ""

    # Empty authorizes nobody. See the module docstring.
    telegram_allowed_users: list[int] = field(default_factory=list)
    telegram_owner_chat_id: int = 0

    discord_allowed_users: list[int] = field(default_factory=list)
    # Empty here means "any channel", which is the existing Discord semantic
    # and is guarded by `discord_allowed_users` rather than by this list.
    discord_allowed_channels: list[int] = field(default_factory=list)
    # Channels this mind answers in without an at-mention. A channel here
    # exists for one conversation with one mind, so requiring a mention on
    # every reply would make the continuity it was created for cost a
    # keystroke nobody keeps paying.
    discord_task_channels: list[int] = field(default_factory=list)

    # Where an inbound photo is written. Defaults under the host's working
    # directory because the alternative — a path derived from this module's
    # own location — is inside site-packages once the core is installed as a
    # package, which is both unwritable in some deployments and useless as a
    # path to hand the mind.
    photo_dir: str = "data/telegram_photos"

    # Where to poll for unsolicited turns, for a surface that does not share a
    # process with its mind backend and so cannot be handed them in memory.
    # Empty means in-process only, which is the edge layout.
    proactive_poll_url: str = ""
    proactive_poll_interval_s: float = 5.0

    # What `/models` lists. The catalog is relayed from whatever inference
    # proxy this host talks to, through a module that belongs to the host and
    # not to a surface — so the host hands over a coroutine returning the rows
    # rather than the core importing a module it cannot declare. Unset means
    # this mind offers no model list, which is reported as exactly that.
    models_catalog: Callable[[], Awaitable[list[dict]]] | None = None

    # Where unsolicited claims and other surface state are kept. Defaults
    # under the host's working directory for the same reason `photo_dir`
    # does: a path derived from the package's own location is inside
    # site-packages, which a reinstall wipes — and the picker claims survive
    # a restart precisely because the incident they prevent runs through one.
    state_dir: str = "data"


class _ConfigProxy:
    """What `config` is, so a late `configure()` is seen by code that imported it.

    The surfaces read `config.telegram_allowed_users` inside handlers, having
    written `from hive_surfaces.config import config` at the top of the file.
    That binds the object, not the module attribute — so rebinding the
    attribute in `configure()` would leave every already-imported surface
    reading the defaults forever, which is an allow-list of nobody and a bot
    that answers no one. Forwarding attribute access to whatever is installed
    now keeps the import spelling and makes the injection actually land.
    """

    __slots__ = ("_current",)

    def __init__(self, current: SurfaceConfig) -> None:
        object.__setattr__(self, "_current", current)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_current"), name)

    def _install(self, cfg: SurfaceConfig) -> None:
        object.__setattr__(self, "_current", cfg)

    def _installed(self) -> SurfaceConfig:
        return object.__getattribute__(self, "_current")

    def __repr__(self) -> str:
        return f"<surface config {self._installed()!r}>"


config = _ConfigProxy(SurfaceConfig())


def configure(cfg: SurfaceConfig | None = None, **overrides) -> SurfaceConfig:
    """Install the host's configuration, before either surface is started.

    `SurfaceConfig` stays frozen and a new one is installed wholesale, so a
    surface reading a field mid-turn cannot find it changed halfway through.

    Given a `cfg`, that object is the whole configuration — a host handing one
    over is stating everything, and a field it left at the default is a field
    it meant to leave at the default. Given only keywords, they are applied to
    whatever is installed now rather than to a fresh default: a host that calls
    this twice — once for the allow-lists at boot, once for a model the console
    changed — would otherwise have its second call blank the allow-list back to
    authorizing nobody, and the symptom is a bot that silently stops answering
    its owner with nothing in the log.

    Returns what was installed, so a host can log exactly what it supplied.
    """
    base = cfg if cfg is not None else config._installed()
    new = replace(base, **overrides) if overrides else base
    config._install(new)
    return new


def photo_root() -> "Path":
    """The directory inbound photos are written under, created on demand."""
    from pathlib import Path

    root = Path(config.photo_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_root() -> "Path":
    """The directory surface state is kept in, created on demand."""
    from pathlib import Path

    root = Path(config.state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def installed() -> SurfaceConfig:
    """The configuration currently in force."""
    return config._installed()
