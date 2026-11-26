"""A mind's one key: the single value it presents to every hive service.

One value is the mind's inference key at the proxy, its gateway credential, its
memory-store credential and its tools credential. The services each hold it as
that mind (the console registers what the person pasted); nothing here invents,
stores or sends it.

Where the key comes from, in order:

* a FILE: `MIND_KEY_FILE` names a file inside a folder that is bind-mounted
  read-only into this mind alone, one folder per mind, kept outside the
  checkout. The console replaces the file atomically (temp file, fsync,
  rename), so a reader sees the old key or the new one, never half of one.
  `refresh` reads it with a modification-time cache, per request and per
  spawn, so a replaced key takes effect on the next request with no restart
  and no recreate;
* otherwise `MIND_KEY` in the environment (the transition: a mind started
  before it had a key folder, or whose file is not there yet).

Rules for the file: a missing file at the first read falls back to
`MIND_KEY`. After a good key has been read, a file that is unreadable,
garbled (not the shape of a key) or empty is ignored: the last good key stays
in use and the fault is logged once, never the key. A file that is read again
and is good wins over the environment.

In a file shared by several minds (the console's own env files) the old
spelling was `MIND_KEY_<NAME>`, the name upper-cased with hyphens as
underscores; `shared_variable` still names it, for the legacy lines the
retirement step removes.

The readers inside a mind were written against the per-service names
(`COMMS_BEARER_TOKEN`, `LUCENT_BEARER_TOKEN`, `HIVE_TOOLS_TOKEN` and the
inference-key names the harness binaries read) and every per-mind hook copy in
a mind's folder still reads those. `apply_environment` sets each of them from
the one key when it is present, so none of those readers changes and none of
those copies needs touching. When the one key is absent nothing is set and the
old per-service values stand: a mind restarted before or after its move keeps
working. That fallback is the transition; it goes when the old values do.

Standard library only, so a hook or a skill script may copy `resolve`.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass

#: What a mind's compose file names, inside the container.
KEY = "MIND_KEY"
#: What the mind's compose file reads the key from, in a shared file.
SHARED_PREFIX = "MIND_KEY_"
#: Names the file the key is read from, inside the container.
KEY_FILE = "MIND_KEY_FILE"
#: A mind on a direct or custom provider names its endpoint here, and its
#: harness reads the key as this provider token.
PROVIDER_ENDPOINT = "HIVE_PROVIDER_URL"
PROVIDER_TOKEN = "HIVE_PROVIDER_TOKEN"

#: The shape of a key: what the console accepts when one is pasted. Anything
#: else in the file (a half-written value, a stray line, a prompt) is not used.
KEY_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]{8,4096}")

_log = logging.getLogger("hive-mind.mind_key")

#: The names readers inside a mind look for, by what they are for. The first
#: of each is the one a service-specific reader reads; the harness binaries
#: read the inference names and must keep finding them.
GATEWAY = ("COMMS_BEARER_TOKEN", "HIVEMIND_BROKER_TOKEN")
MEMORY = ("LUCENT_BEARER_TOKEN",)
TOOLS = ("HIVE_TOOLS_TOKEN",)
INFERENCE = ("MIND_PROXY_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "DSH_API_KEY")
GENERIC_NAMES = GATEWAY + MEMORY + TOOLS + INFERENCE


def shared_variable(mind: str) -> str:
    """The variable a shared env file holds this mind's one key under.

    A hyphen is not legal in a compose variable name, so it becomes an
    underscore, as it does in every other per-mind variable.
    """
    stem = str(mind).strip().replace("-", "_").upper()
    if not stem:
        raise ValueError("a mind's key variable needs the mind's name")
    return SHARED_PREFIX + stem


@dataclass
class _FileState:
    #: What the file looked like when it was last examined.
    stamp: tuple | None = None
    #: The last good key read from it; "" until one has been.
    good: str = ""
    #: The fault last logged for it, so a fault is logged once, not per request.
    logged: str = ""
    #: The fault found at `stamp`, if any, so an unchanged bad file is not re-read.
    problem: str = ""


_STATE: dict[str, _FileState] = {}
_STATE_LOCK = threading.Lock()


def _examine(path: str, state: _FileState) -> None:
    """Look at the file again only when it has changed (its stamp), and record
    what is in it. Never raises and never logs a key."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        state.stamp, state.problem = None, "the key file is not there"
        return
    except OSError as exc:
        state.stamp, state.problem = None, f"the key file cannot be read ({exc.strerror or exc})"
        return
    stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
    if stamp == state.stamp:
        return
    try:
        with open(path, encoding="utf-8") as stream:
            text = stream.read(8192)
    except (OSError, UnicodeDecodeError) as exc:
        state.stamp, state.problem = None, f"the key file cannot be read ({type(exc).__name__})"
        return
    value = text.strip()
    state.stamp = stamp
    if not value:
        state.problem = "the key file is empty"
    elif not KEY_SHAPE.fullmatch(value):
        state.problem = "the key file does not hold a key (wrong shape)"
    else:
        state.good, state.problem = value, ""


def refresh(environ: MutableMapping[str, str] | None = None) -> str:
    """Bring this mind's key up to date from its key file, and return it.

    With no `MIND_KEY_FILE` this changes nothing. With one, a good key in the
    file is set as `MIND_KEY` and re-applied to every per-service name when it
    differs from what the environment holds, so the next request, spawn or
    registration presents the new key. The rules for a missing, empty or
    garbled file are in the module's description.
    Cost when nothing changed: one `stat`.
    """
    target = os.environ if environ is None else environ
    path = str(target.get(KEY_FILE) or "").strip()
    if not path:
        return key(target)
    with _STATE_LOCK:
        state = _STATE.setdefault(path, _FileState())
        _examine(path, state)
        problem, good = state.problem, state.good
        if problem and state.logged != problem:
            state.logged = problem
            if good:
                _log.error("%s: keeping the last good key", problem)
            elif key(target):
                _log.warning("%s: using MIND_KEY from the environment", problem)
            else:
                _log.error("%s and MIND_KEY is not set: this mind has no key", problem)
        elif not problem:
            state.logged = ""
    if good and key(target) != good:
        target[KEY] = good
        apply_environment(target)
    return key(target)


def key(environ: Mapping[str, str] | None = None) -> str:
    """This mind's one key, or "" when it was started without one."""
    source = os.environ if environ is None else environ
    return str(source.get(KEY) or "").strip()


def resolve(environ: Mapping[str, str] | None, *legacy: str) -> str:
    """The one key if present, otherwise the first old variable that is set."""
    source = os.environ if environ is None else environ
    one = key(source)
    if one:
        return one
    for name in legacy:
        value = str(source.get(name) or "").strip()
        if value:
            return value
    return ""


def apply_environment(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Set every per-service name from the one key, when there is one.

    Returns the names it set. With no `MIND_KEY` nothing is touched. With one,
    it wins over whatever an old fragment still carries: a mind has one key,
    never an old and a new both working.
    """
    target = os.environ if environ is None else environ
    one = key(target)
    if not one:
        return []
    names = list(GENERIC_NAMES)
    if str(target.get(PROVIDER_ENDPOINT) or "").strip():
        # A mind on a direct or custom provider: the key is the provider token
        # its harness is told to read (the compose names the endpoint only),
        # and, on an Anthropic-format provider that is not the hive's proxy,
        # the Anthropic key name too.
        names.append(PROVIDER_TOKEN)
        if (str(target.get("HIVE_PROVIDER_API") or "") == "anthropic"
                and str(target.get("HIVE_PROVIDER_KIND") or "") != "proxy"):
            names.append("ANTHROPIC_API_KEY")
    for name in names:
        target[name] = one
    return names


def runtime_env(declared: Mapping[str, object] | None,
                environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """A mind's `runtime.yaml` `env:` block, less any key it carries once the
    mind has its one key.

    Some minds keep their proxy key in plain text there. A spawn applies that
    block over the process environment, so left in it the old value would beat
    the one key and the mind would hold two. With a one key present the block's
    key-bearing names are dropped; without one the block is returned as is.
    """
    block = dict(declared or {})
    if not key(environ):
        return block
    return {name: value for name, value in block.items() if name not in GENERIC_NAMES}


def proactive_token(environ: Mapping[str, str] | None = None, started_with: str = "") -> str | None:
    """What the mind's own bot must present to collect proactive messages.

    The bot's credential is its own (`MIND_PROACTIVE_TOKEN`), never the mind's
    key: a bot given the key would be a holder of the mind's inference key.

    Without one, a mind started the old way (no `MIND_KEY`) keeps what it
    always compared against: the gateway value it was started with
    (`started_with`, read before the key was applied), so a bot not yet given
    its own credential keeps working through the transition. A mind on the one
    key never takes that old gateway value: once the old keys retire it would
    be a retired credential still collecting the mind's messages. Such a mind
    with no `MIND_PROACTIVE_TOKEN` answers nobody (a value nothing can present)
    rather than the open route a mind with no credential at all has.
    """
    source = os.environ if environ is None else environ
    own = str(source.get("MIND_PROACTIVE_TOKEN") or "").strip()
    if own and own != key(source):
        return own
    if key(source) or str(source.get(KEY_FILE) or "").strip():
        # On the one key, or told where its key file is (which may not be
        # readable yet): never the old gateway value.
        return secrets.token_urlsafe(32)
    return started_with.strip() or None


def generic_environment(reference: str) -> dict[str, str]:
    """Every per-service name mapped to one value, for a compose file to carry.

    `reference` is the interpolation (`${MIND_KEY_WREN}`) or the value.
    """
    return {name: reference for name in GENERIC_NAMES}
