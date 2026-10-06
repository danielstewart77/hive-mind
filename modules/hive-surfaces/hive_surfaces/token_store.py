"""The surface token, read and replaced where this host's surface reads it.

This lives in the core and not in either host because the precedence is the
same precedence `_get_bot_token` applies, and a writer that disagreed with the
reader would store a token in a place nothing consults — the symptom being a
surface still running the old one behind a page that says saved. One rule, in
the module both hosts install.

Where that is differs by deployment. An edge mind reads `TELEGRAM_BOT_TOKEN`
out of its own `.env`, one bot per host; a containerised stack runs several
surfaces from one image on one machine and cannot hold several values under
one environment name, so each names its own `TELEGRAM_BOT_TOKEN_KEYRING_KEY`
and the token lives in the keyring. Only the mind's own filesystem can see
either, which is why the route that calls this is the mind's rather than the
console's.

Nothing here ever hands a token back. A caller that returned one would put
every bot in the hive one admin credential away from being impersonated, and
the operator does not need the value — they need to know whether the surface
has one that works, which is a question the bot API answers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import aiohttp

# Telegram's own shape: a numeric bot id, a colon, then the secret. Checked
# before the network call so an obvious paste error is refused without
# handing anything to an external service, and checked at all because the
# verify step is the only other gate and it should never see a stray newline
# or an empty string.
_TOKEN_PATTERN = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,}$")

# The names live here and `telegram_bot` reads them from here, rather than
# each stating its own: two copies of a key name is how a token gets written
# under one spelling and read under another. This module and not that one
# because importing `telegram_bot` pulls the whole telegram dependency chain
# into a mind server that only wants to write a file.
KEYRING_SERVICE = "hive-mind"

#: The two names each surface turns on, keyed by surface. One table rather
#: than a pair of functions per surface: Discord was left reading only the
#: environment when Telegram's keyring path was restored, and a mind whose
#: Discord token lived in the keyring came up crashlooping on a token it had
#: all along.
TOKEN_NAMES = {
    "telegram": (
        "TELEGRAM_BOT_TOKEN",  # secret-guard: allow — a key name
        "TELEGRAM_BOT_TOKEN_KEYRING_KEY",  # secret-guard: allow — a variable name
    ),
    "discord": (
        "DISCORD_BOT_TOKEN",  # secret-guard: allow — a key name
        "DISCORD_BOT_TOKEN_KEYRING_KEY",  # secret-guard: allow — a variable name
    ),
}

DEFAULT_SURFACE = "telegram"
DEFAULT_TOKEN_KEY, KEYRING_KEY_VAR = TOKEN_NAMES[DEFAULT_SURFACE]

_ENV_VAR = DEFAULT_TOKEN_KEY
_KEYRING_KEY_VAR = KEYRING_KEY_VAR
_KEYRING_SERVICE = KEYRING_SERVICE


def names(surface: str) -> tuple[str, str]:
    """The environment variable and the keyring-key variable for `surface`."""
    try:
        return TOKEN_NAMES[surface]
    except KeyError:
        raise ValueError(f"no such surface: {surface!r}") from None


def resolve_token(surface: str = DEFAULT_SURFACE) -> str:
    """The token this surface should start on, or empty if it has none.

    A named keyring key wins over the environment, because the only reason to
    name one is that this surface's token is *not* the ambient one — a stack
    where the root `.env` happens to export some mind's token would otherwise
    start several bots polling as the same bot, each stealing the others'
    updates, with nothing in any log to say so. With no key named the
    environment wins and the default key is the fallback, which is the edge
    layout: one bot per host, token in its own `.env`.
    """
    env_var, key_var = names(surface)
    named = os.environ.get(key_var, "").strip()
    if named:
        return _keyring_get(named) or os.environ.get(env_var, "")
    return os.environ.get(env_var, "") or _keyring_get(env_var)

VERIFY_URL = "https://api.telegram.org/bot{token}/getMe"
VERIFY_TIMEOUT_S = 15.0


class TokenRefused(Exception):
    """The token is malformed, or the bot API will not authenticate it."""


@dataclass(frozen=True)
class TokenStatus:
    """What the console may know about a surface's token.

    Three states, not two. "Nothing is stored" and "what is stored is not
    accepted" look identical on a page that only reports presence, and their
    remedies differ: one is a token nobody has supplied, the other is a token
    that was revoked or pasted wrong and is now failing on every poll. The
    third is the ordinary case, named by the bot the token actually belongs
    to so the operator can see it is the mind they meant.
    """

    stored: bool
    accepted: bool | None  # None when nothing is stored, or when unverifiable
    bot_username: str = ""
    where: str = ""  # "keyring:<key>" or "env", for the operator's own sake
    detail: str = ""


def storage_location(surface: str = DEFAULT_SURFACE) -> tuple[str, str]:
    """Where this mind's surface reads its token from, as (kind, name).

    A named keyring key is the deliberate signal that this surface's token is
    not the ambient one — the same precedence the surfaces themselves apply,
    because a writer that disagreed with the reader would store a token in a
    place nothing consults.
    """
    env_var, key_var = names(surface)
    named = os.environ.get(key_var, "").strip()
    if named:
        return "keyring", named
    return "env", env_var


def _env_path() -> Path:
    """The `.env` an edge mind's own process loads."""
    return Path(os.environ.get("HIVE_PROJECT_DIR", ".")).resolve() / ".env"


def _keyring_get(key: str) -> str:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, key) or ""
    except Exception:  # noqa: BLE001
        return ""


def _keyring_set(key: str, token: str) -> None:
    import keyring

    keyring.set_password(_KEYRING_SERVICE, key, token)


def _env_get(env_var: str = _ENV_VAR) -> str:
    """The token currently in the `.env` file, not in this process.

    The process's own environment is a snapshot from boot: a token written an
    hour ago is on disk and not in `os.environ`, and reporting the stale copy
    would tell the operator their write never landed.
    """
    path = _env_path()
    try:
        # `open`, not `Path.read_text`: the `newline` keyword only reached
        # the latter in 3.13 and a mind may be running 3.12. Pinned either
        # way, because universal-newline translation is silent in both
        # directions and a CRLF `.env` read and written back would be
        # rewritten line by line.
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{env_var}="):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return ""


def _env_set(token: str, env_var: str = _ENV_VAR) -> None:
    """Replace the token line in `.env`, or add it, and nothing else.

    A line rewrite rather than a dump: the file carries comments and every
    other secret this mind holds, and a round-trip through any parser is how
    one of those comes back quoted differently or not at all. Written to a
    temporary file in the same directory and renamed, carrying the original's
    mode, because a half-written `.env` is a mind that will not boot.
    """
    path = _env_path()
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            original = handle.read()
    except OSError:
        original = ""
    lines = original.splitlines(keepends=True)
    replacement = f"{env_var}={token}\n"
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{env_var}=") and not replaced:
            out.append(replacement)
            replaced = True
        elif line.strip().startswith(f"{env_var}="):
            continue  # a second declaration is a file whose last word wins
        else:
            out.append(line)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(replacement)

    staging = path.with_name(f".{path.name}.surface-token.tmp")
    with open(staging, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(out))
    if path.exists():
        os.chmod(staging, path.stat().st_mode & 0o7777)
    else:
        os.chmod(staging, 0o600)
    os.replace(staging, path)


def stored_token(surface: str = DEFAULT_SURFACE) -> str:
    """Whatever this surface would read right now, off disk.

    Not `resolve_token`: that reads `os.environ`, which is a snapshot from
    boot, so a token written an hour ago would read as never having landed.
    """
    kind, name = storage_location(surface)
    return _keyring_get(name) if kind == "keyring" else _env_get(name)


async def verify(token: str, session: aiohttp.ClientSession | None = None) -> str:
    """The bot username this token authenticates as.

    Raises `TokenRefused` when the bot API will not take it. This is the whole
    reason the write is not a blind store: a token with a character missing is
    accepted by any file, and the symptom arrives at the next restart as a
    surface that polls, gets 401 forever, and looks like Telegram is down.
    """
    if not _TOKEN_PATTERN.match(token):
        raise TokenRefused("not the shape of a bot token")

    owns_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(
            VERIFY_URL.format(token=token),
            timeout=aiohttp.ClientTimeout(total=VERIFY_TIMEOUT_S),
        ) as response:
            body = await response.json(content_type=None)
            if response.status != 200 or not body.get("ok"):
                raise TokenRefused(
                    str(body.get("description") or f"bot API returned {response.status}")
                )
            return str((body.get("result") or {}).get("username") or "")
    except TokenRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TokenRefused(f"could not reach the bot API: {exc}") from exc
    finally:
        if owns_session:
            await session.close()


async def status(
    session: aiohttp.ClientSession | None = None, surface: str = DEFAULT_SURFACE
) -> TokenStatus:
    """Whether this surface has a token the bot API accepts."""
    kind, name = storage_location(surface)
    where = f"keyring:{name}" if kind == "keyring" else "env"
    token = stored_token(surface)
    if not token:
        return TokenStatus(stored=False, accepted=None, where=where)
    try:
        username = await verify(token, session=session)
    except TokenRefused as exc:
        return TokenStatus(
            stored=True, accepted=False, where=where, detail=str(exc),
        )
    return TokenStatus(
        stored=True, accepted=True, bot_username=username, where=where,
    )


async def replace(
    token: str,
    session: aiohttp.ClientSession | None = None,
    surface: str = DEFAULT_SURFACE,
) -> TokenStatus:
    """Verify `token`, then store it where this mind's surface reads it.

    Verification first and storage only on success: a refused token must
    leave the working one in place, because the operator pasting a typo has
    not asked to take the surface down.
    """
    username = await verify(token, session=session)
    kind, name = storage_location(surface)
    if kind == "keyring":
        _keyring_set(name, token)
    else:
        _env_set(token, name)
    return TokenStatus(
        stored=True,
        accepted=True,
        bot_username=username,
        where=f"keyring:{name}" if kind == "keyring" else "env",
    )
