"""The GitHub token a mind pushes with, verified then stored where it is read.

Beside `token_store` and for the same reason: this is the only package both
hosts install, and the rule about *where* a mind's credential goes has to be
one rule. Where differs by deployment exactly as it does for a bot token — a
containerised mind names its own `GITHUB_TOKEN_KEYRING_KEY` because several
minds run from one image on one machine and the environment cannot hold
several values under one name, while an edge mind reads `GITHUB_TOKEN` out of
its own `.env`.

The keyring is the durable half. A containerised mind's home directory is
inside the image and is wiped by every rebuild; the keyring is a bind mount
and is not. So the token lives there, and the files `git` and `gh` actually
read are written *from* it — at the moment the token is stored, so the tools
work without the mind being restarted, and again when the mind starts, so a
rebuilt container has them back before anything asks.

Those files are only written where the keyring is the store. On an edge mind
the home directory belongs to the operator, their own `~/.git-credentials` is
already a credential the console manages by itself, and a mind rewriting it
would put two writers on one file.

Nothing here hands a token back. The operator does not need the value — they
need to know whether the mind has one that works and which account it belongs
to, which is a question the GitHub API answers.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from hive_surfaces.token_store import (
    env_get,
    env_set,
    keyring_get,
    keyring_lock,
    keyring_set,
    redact,
)

#: The name a mind's token is stored under, and the variable naming a keyring
#: key when the ambient environment is not where it belongs. Declared here and
#: read from here: two copies of a key name is how a token gets written under
#: one spelling and read under another.
TOKEN_ENV_VAR = "GITHUB_TOKEN"  # secret-guard: allow — a key name
KEYRING_KEY_VAR = "GITHUB_TOKEN_KEYRING_KEY"  # secret-guard: allow — a variable name

#: Whether this mind's home directory is its own to rewrite. Declared by the
#: deployment rather than inferred from where the token is stored: a keyring
#: key set on an edge install would otherwise have the mind truncating the
#: operator's own `~/.git-credentials` and `gh` `hosts.yml` — two files the
#: console already manages, whose other hosts and second accounts a
#: single-block rewrite destroys.
OWNS_HOME_VAR = "GITHUB_TOKEN_CONFIGURES_TOOLS"

VERIFY_URL = "https://api.github.com/user"
VERIFY_TIMEOUT_S = 15.0

GITHUB_HOST = "github.com"


class TokenRefused(Exception):
    """GitHub explicitly rejected this token, or it is malformed.

    The only state whose remedy is a new token, which is why it is a separate
    exception from the one below.
    """


class TokenUnverifiable(Exception):
    """GitHub did not answer, or answered something that is not a verdict.

    A secondary rate limit, a 5xx, a DNS failure, a mind with no egress. Not
    a refusal: a page that rendered this as one would have the operator
    revoking a token that works, and nothing in the hive would say otherwise.
    """


@dataclass(frozen=True)
class GithubTokenStatus:
    """What the console may know about a mind's GitHub token.

    Three states, not two. "Nothing is stored" and "what is stored is not
    accepted" look identical on a page that only reports presence, and their
    remedies differ: one is a token nobody supplied, the other is one revoked
    or pasted wrong that is failing on every push. The ordinary case is named
    by the account it belongs to, so the operator can see it is the one they
    meant.
    """

    stored: bool
    accepted: bool | None  # None when nothing is stored
    login: str = ""
    where: str = ""  # "keyring:<key>" or "env", for the operator's own sake
    detail: str = ""
    configured: list[str] = field(default_factory=list)
    #: The last four characters of the stored value, so a row can show which
    #: token this is without the console ever holding one. Never more: four
    #: characters identify a token to the person who pasted it and
    #: authenticate as nobody.
    preview: str = ""


def storage_location() -> tuple[str, str]:
    """Where this mind reads its GitHub token from, as (kind, name).

    A named keyring key is the deliberate signal that this mind's token is not
    the ambient one, the same precedence `token_store` applies.
    """
    named = os.environ.get(KEYRING_KEY_VAR, "").strip()
    if named:
        return "keyring", named
    return "env", TOKEN_ENV_VAR


def owns_its_home() -> bool:
    """Whether writing `git` and `gh`'s own files here is this mind's business.

    True in a container, whose home is image layers nothing else reads and a
    rebuild deletes. False everywhere else, by default: an edge mind shares a
    home with the person who owns the machine.
    """
    return os.environ.get(OWNS_HOME_VAR, "").strip().lower() in {"1", "true", "yes"}


def where_label() -> str:
    kind, name = storage_location()
    return f"keyring:{name}" if kind == "keyring" else "env"


def stored_token() -> str:
    """Whatever this mind would read right now, off disk.

    Off disk and not out of `os.environ`, which is a snapshot from boot: a
    token written an hour ago would otherwise read as never having landed.
    """
    kind, name = storage_location()
    return keyring_get(name) if kind == "keyring" else env_get(name)


def _reject_unusable(token: str) -> None:
    """Refuse a paste that cannot even be sent, before GitHub is asked.

    `isascii` before `isspace`: a paste out of a browser or a password manager
    picks up zero-width spaces and smart quotes, which are not `isspace()` and
    survive to the request, where building the Authorization header raises
    `UnicodeEncodeError` — nowhere near the handlers below, so the operator
    gets a 500 instead of being told the paste is bad. No pattern beyond that:
    GitHub has minted `ghp_`, `github_pat_`, `gho_`, `ghs_` and a bare
    forty-hex shape, and a regex over those refuses the next prefix it adds.
    """
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise TokenRefused("that does not look like a token")


async def verify(token: str, session: aiohttp.ClientSession | None = None) -> str:
    """The GitHub account this token authenticates as.

    Raises `TokenRefused` when GitHub rejected it and `TokenUnverifiable`
    when GitHub did not answer at all. Two exceptions because they are two
    situations: one is a token to replace, the other is a verifier to wait
    for, and folding them together is how a working token gets revoked.

    Verification at all because a token with a character missing is accepted
    by any file, and the symptom arrives later as a push that fails for a
    mind nobody is watching.
    """
    _reject_unusable(token)

    owns_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(
            VERIFY_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "hive-mind-github-token",
            },
            timeout=aiohttp.ClientTimeout(total=VERIFY_TIMEOUT_S),
        ) as response:
            if response.status == 401:
                raise TokenRefused("GitHub rejected that token")
            if response.status != 200:
                # Including 403, which is how a secondary rate limit arrives
                # and which says nothing whatever about the token.
                raise TokenUnverifiable(f"GitHub answered {response.status}")
            body = await response.json(content_type=None)
            return str((body or {}).get("login") or "")
    except (TokenRefused, TokenUnverifiable):
        raise
    except Exception as exc:  # noqa: BLE001
        # Redacted for the same reason the bot API's errors are: this text is
        # logged by the mind and returned to the console, and an upstream is
        # free to quote whatever it was given back at us.
        raise TokenUnverifiable(
            f"could not reach GitHub: {redact(str(exc), token)}"
        ) from None
    finally:
        if owns_session:
            await session.close()


def preview_of(token: str) -> str:
    """The last four characters, or nothing. Never the token.

    Four characters tell the person who pasted it which token this is and
    authenticate as nobody. A short value gets stars rather than most of
    itself.
    """
    if not token:
        return ""
    return f"...{token[-4:]}" if len(token) > 4 else "****"


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _write_private(path: Path, text: str) -> None:
    """Write a credential file owner-only, created that way rather than fixed.

    The mode goes on the `open` itself: the window between a 0644 write and a
    later `chmod` is a window where a token is readable by anyone on the host,
    and on a shared image that is every other mind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
        out.write(text)
    os.chmod(path, 0o600)


def git_credentials_path() -> Path:
    return _home() / ".git-credentials"


def gh_hosts_path() -> Path:
    return _home() / ".config" / "gh" / "hosts.yml"


def configure_tools(token: str, login: str) -> list[str]:
    """Put the token where `git` and `gh` read it, and say what was written.

    Both read their files per invocation, which is what makes this work on a
    mind that is not restarted. `git config` is run rather than the config
    file being edited here, because git owns the format of its own config and
    a hand-appended section is how a mind ends up with two `[credential]`
    blocks and no helper.

    Returns the paths it wrote and never raises. By the time this runs the
    token is already in the keyring, so an unwritable home would otherwise
    answer the caller with a failure over a token that was in fact stored —
    and the operator would paste it again, or worse, conclude it had not
    taken. What could not be written is reported instead.
    """
    written: list[str] = []

    account = login or "x-access-token"
    try:
        _write_private(
            git_credentials_path(),
            f"https://{account}:{token}@{GITHUB_HOST}\n",  # secret-guard: allow — template, not a value
        )
    except OSError as exc:
        written.append(f"{git_credentials_path()} could not be written: {exc.strerror or exc}")
    else:
        written.append(str(git_credentials_path()))

    try:
        _write_private(
            gh_hosts_path(),
            f"{GITHUB_HOST}:\n"
            f"    oauth_token: {token}\n"
            f"    user: {account}\n"
            "    git_protocol: https\n"
            "    users:\n"
            f"        {account}:\n"
            f"            oauth_token: {token}\n",
        )
    except OSError as exc:
        written.append(f"{gh_hosts_path()} could not be written: {exc.strerror or exc}")
    else:
        written.append(str(gh_hosts_path()))

    try:
        subprocess.run(
            ["git", "config", "--global", "credential.helper", "store"],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        # The file above is still the token's home; what is missing is the
        # helper that reads it, which is a git that is absent or wedged.
        return written
    written.append("git credential.helper=store")
    return written


async def status(session: aiohttp.ClientSession | None = None) -> GithubTokenStatus:
    """Whether this mind has a GitHub token the API accepts."""
    where = where_label()
    token = stored_token()
    if not token:
        return GithubTokenStatus(stored=False, accepted=None, where=where)
    tail = preview_of(token)
    try:
        login = await verify(token, session=session)
    except TokenRefused as exc:
        return GithubTokenStatus(
            stored=True, accepted=False, where=where, detail=str(exc), preview=tail,
        )
    except TokenUnverifiable as exc:
        # Stored, and no verdict. `accepted=None` beside `stored=True` is a
        # third answer the console renders as "could not tell" rather than as
        # a refusal, whose remedy would be to revoke a working token.
        return GithubTokenStatus(
            stored=True, accepted=None, where=where, detail=str(exc), preview=tail,
        )
    return GithubTokenStatus(
        stored=True, accepted=True, login=login, where=where, preview=tail,
    )


async def replace(
    token: str, session: aiohttp.ClientSession | None = None
) -> GithubTokenStatus:
    """Verify `token`, then store it where this mind reads it.

    Verification first and storage only on success: a refused token must leave
    the working one in place, because the operator pasting a typo has not
    asked to take the mind's pushes down.
    """
    login = await verify(token, session=session)
    kind, name = storage_location()
    configured: list[str] = []
    if kind == "keyring":
        # One lock across both, not one per write. Two overlapping stores
        # otherwise leave the keyring holding the second token while the
        # credential files hold the first: the mind pushes as one account and
        # reports the other, and nothing on either side disagrees.
        with keyring_lock():
            keyring_set(name, token, locked=True)
            if owns_its_home():
                configured = configure_tools(token, login)
    else:
        env_set(token, name)
    return GithubTokenStatus(
        stored=True,
        accepted=True,
        login=login,
        where=where_label(),
        configured=configured,
        preview=preview_of(token),
    )


def apply_stored() -> list[str]:
    """Re-write `git` and `gh`'s files from the keyring. For boot.

    A rebuilt container has the token and none of the files, and the first
    thing to notice would otherwise be a push failing. Unverified on purpose:
    this runs while the mind is coming up, and a GitHub that is unreachable
    must not delay that or discard a token that is probably fine.
    """
    if not owns_its_home():
        return []
    kind, _name = storage_location()
    if kind != "keyring":
        return []
    token = stored_token()
    if not token:
        return []
    return configure_tools(token, "")
