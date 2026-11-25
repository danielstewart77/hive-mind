"""A mind's one key: the single value it presents to every hive service.

One value is the mind's inference key at the proxy, its gateway credential, its
memory-store credential and its tools credential. The services each hold it as
that mind (the console registers what the person pasted); nothing here invents,
stores or sends it.

Two spellings, one rule:

* inside the mind's container the compose file names `MIND_KEY`;
* in a file shared by several minds (the install's `.env`) it is
  `MIND_KEY_<NAME>`, the name upper-cased with hyphens as underscores, the same
  rule the per-service variables used.

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

import os
import secrets
from collections.abc import Mapping, MutableMapping

#: What a mind's compose file names, inside the container.
KEY = "MIND_KEY"
#: What the mind's compose file reads the key from, in a shared file.
SHARED_PREFIX = "MIND_KEY_"

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
    for name in GENERIC_NAMES:
        target[name] = one
    return list(GENERIC_NAMES)


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

    Without one, a mind started the old way keeps what it always compared
    against: the gateway value it was started with (`started_with`, read before
    the key was applied), so a bot not yet given its own credential keeps
    working through the transition. A value equal to the one key is never
    accepted, and a mind with a key and nothing else answers nobody (a value
    nothing can present) rather than the open route a mind with no credential
    at all has.
    """
    source = os.environ if environ is None else environ
    own = str(source.get("MIND_PROACTIVE_TOKEN") or "").strip()
    if own and own != key(source):
        return own
    carried = started_with.strip()
    if key(source):
        return carried if carried and carried != key(source) else secrets.token_urlsafe(32)
    return carried or None


def generic_environment(reference: str) -> dict[str, str]:
    """Every per-service name mapped to one value, for a compose file to carry.

    `reference` is the interpolation (`${MIND_KEY_WREN}`) or the value.
    """
    return {name: reference for name in GENERIC_NAMES}
