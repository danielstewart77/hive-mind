"""Credential registry for the /settings credentials tab.

Hive's shared-secret tokens (COMMS_BEARER_TOKEN and friends) are not single
values — each one is duplicated across every .env file that authenticates
with it: the server side that validates the token
(hive_mind/nervous-system/.env) and every client that presents it
(hive_mind/.env for containerized minds, each edge mind's own .env, and
hive-tools/.env). Rotating one means writing the same new value into every
file that currently holds it, which is what this module does — it does not
guess a uniform scheme across all credentials, because not all credentials
share one. HIVE_TOOLS_TOKEN is issued per-consumer from a sqlite table
(sha256-hashed, see hive-tools/auth_api.py) and has no symmetric env copy to
fan out to, so it stays manual-only.

The GitHub PAT is different again: GitHub has no rotation API, so the
console can't mint one — but once Daniel generates a replacement in the
GitHub UI, its two live homes on this host are ~/.git-credentials (git's
credential.helper store) and ~/.config/gh/hosts.yml (the gh CLI, which
keeps the token in two oauth_token fields). Both are file edits, so the
tab accepts a pasted PAT, verifies it against api.github.com before
touching anything, and writes both files. Nothing needs a restart — both
are read per invocation.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Each .env is bind-mounted into this container as a single file, so the
# usual write-temp-then-os.replace dance is impossible: renaming over a
# bind-mount target fails with EBUSY, and even if it didn't, the new inode
# would leave the mount pointing at the old one. Rewriting in place is the
# only thing that works here, so a copy of the original goes to this
# console-owned directory first — a truncated nervous-system/.env would
# take the hive down with it.
BACKUP_DIR = Path(os.environ.get("ENV_BACKUP_DIR", "/data/env-backups"))

# Every write here is read-modify-write over a file several credentials share
# — nervous-system/.env alone holds four of the five. The rotate route is an
# async def doing blocking IO, so FastAPI runs it in a threadpool and two
# clicks genuinely overlap: both read the same original, both write their own
# key onto it, and the second one's copy silently drops the first one's. Both
# then report complete, the audit log records no skips, and the operator
# restarts the hive on the strength of that. The install path got a lock for
# the far cheaper double-clicked-button case; this one mutates the secrets.
_WRITE_LOCK = threading.RLock()

ENV_FILES: dict[str, Path] = {
    "nervous_system": Path("/env_nervous_system/.env"),
    "hive_mind": Path("/env_hive_mind/.env"),
    "skippy": Path("/env_edge/skippy/.env"),
    "mordecai": Path("/env_edge/mordecai/.env"),
    "hive_tools": Path("/env_hive_tools/.env"),
}

# The GitHub PAT's two live homes, bind-mounted as single files like the
# .env files above (same EBUSY constraint — rewrite in place only).
GIT_CREDENTIALS_PATH = Path("/gh/.git-credentials")
GH_HOSTS_PATH = Path("/gh/hosts.yml")

GITHUB_API_USER_URL = "https://api.github.com/user"

_KEY_LINE_RE_TEMPLATE = r"^{key}=.*$"


class Unreadable(Exception):
    """This file exists but its text could not be read."""


def _read_text(path: Path) -> str:
    """Read a credential file, or say why not in one exception type.

    A permissions refusal and a stray non-UTF-8 byte are the same event to
    every caller here: this file cannot be consulted. Left unguarded they
    propagate out of list_credentials and take the whole credentials tab down
    with them, so one unreadable file hides the other six.
    """
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise Unreadable(str(path)) from exc


def _read_env_value(path: Path, key: str) -> str | None:
    """The value the services would load, or None if this file has no say.

    Deliberately the *last* occurrence: docker compose's env_file and
    python-dotenv both take the last, and a hand-edited .env carrying a stale
    duplicate is exactly where reading the first tells the operator a value
    the running service has never seen.
    """
    if not path.is_file():
        return None
    matches = re.findall(
        _KEY_LINE_RE_TEMPLATE.format(key=re.escape(key)), _read_text(path), flags=re.MULTILINE
    )
    if not matches:
        return None
    return matches[-1].split("=", 1)[1]


def _redact(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 4:
        return "****"
    return f"...{value[-4:]}"


def _backup(text: str, label: str, key: str) -> None:
    """Copy a credential file aside before it is rewritten.

    Created 0600 by the open() itself rather than chmod'd afterwards — every
    token the hive has ever held ends up in this directory, and the window
    between a 0644 write and a later chmod is a window where all of them are
    world-readable. O_EXCL with a counter because two rotations inside the
    same second would otherwise write the same filename, and the loser's
    pre-rotation value is then the one copy that no longer exists anywhere.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for attempt in range(1000):
        suffix = "" if attempt == 0 else f"-{attempt}"
        destination = BACKUP_DIR / f"{label}-{key}-{stamp}{suffix}.env"
        try:
            handle = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(handle, "w") as backup_file:
            backup_file.write(text)
        return
    raise OSError(f"could not find a free backup name for {label}-{key}")


def _rewrite_in_place(path: Path, updated: str, original: str) -> None:
    """Overwrite this file, putting its old contents back if the write fails.

    open(path, "w") truncates before it writes, so a write that fails partway
    — ENOSPC, an EIO on the bind mount, a remount taken mid-write — leaves the
    file empty while the caller reports "not written". The operator reads that
    as "nothing happened to that file" and finds out otherwise when every
    service comes up with no configuration at all. The backup this module
    takes was never wired to a restore; this is that restore.
    """
    try:
        with open(path, "w") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            with open(path, "w") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # The copy in BACKUP_DIR is now the only one. Say so loudly
            # rather than reporting the original failure as the whole story.
            raise
        raise


def _write_env_value(path: Path, key: str, new_value: str, label: str = "env") -> bool:
    """Replace KEY=... in place. Returns False if the key isn't in this file.

    The write preserves the inode (see BACKUP_DIR's note): mode and ownership
    are therefore untouched by construction.
    """
    if not path.is_file():
        return False
    text = _read_text(path)
    # Every occurrence, not the first. A stale duplicate lower down the file
    # is what hand-edits leave behind, and it is the one the service loads —
    # rotating only the first reports a success the running process disagrees
    # with, permanently and silently. The replacement is a lambda because a
    # string replacement expands \1 and \g<name> inside the new value.
    updated_text, count = re.subn(
        _KEY_LINE_RE_TEMPLATE.format(key=re.escape(key)),
        lambda _match: f"{key}={new_value}",
        text,
        flags=re.MULTILINE,
    )
    if not count:
        return False
    _backup(text, label, key)
    _rewrite_in_place(path, updated_text, text)
    return True


def _unwritable_reason(path: Path) -> str | None:
    """Why this path cannot take a write, or None when it can.

    A credential lives in several files and each one is written on its own, so
    "it did not happen" is never the whole answer — the operator needs to know
    whether to create a file, delete a Docker-made directory, or add a missing
    key. hive_tools' .env is a directory on every host where compose created
    the bind source before hive-tools was cloned, which is the case that used
    to vanish into a reported success.
    """
    if path.is_dir():
        return f"{path} is a directory, not a file"
    if not path.exists():
        return f"no file at {path}"
    if not path.is_file():
        return f"{path} is not a regular file"
    return None


def _write_location(path: Path, key: str, new_value: str, label: str) -> dict[str, Any]:
    """One atomic write, reported on its own terms. Never raises."""
    row: dict[str, Any] = {
        "file": label,
        "path": str(path),
        "env_key": key,
        "written": False,
        "reason": None,
    }
    reason = _unwritable_reason(path)
    if reason is not None:
        row["reason"] = reason
        return row
    try:
        wrote = _write_env_value(path, key, new_value, label=label)
    except Unreadable as exc:
        # A non-UTF-8 byte is a ValueError, not an OSError, so a guard written
        # for the latter never saw it — and the caller turns a stray
        # ValueError into a 400 with no per-location report at all, which is
        # the split-brain this function exists to prevent.
        cause = exc.__cause__
        row["reason"] = (
            f"could not be read: {cause.strerror or cause}"
            if isinstance(cause, OSError)
            else "file is not readable as text"
        )
        return row
    except OSError as exc:
        # A read-only mount, a permissions refusal, a full disk. One file
        # failing this way must not deny the rest of the hive its rotation.
        row["reason"] = f"could not be written: {exc.strerror or exc}"
        return row
    if not wrote:
        row["reason"] = f"{key} is not in this file"
        return row
    row["written"] = True
    return row


def _write_file_in_place(path: Path, text: str, label: str, key: str, original: str) -> None:
    """Back up, then overwrite in place — same inode discipline as the env writer."""
    _backup(original, label, key)
    _rewrite_in_place(path, text, original)


def _summarise(rows: list[dict[str, Any]]) -> str:
    return "; ".join(f"{row['file']}: {row['reason']}" for row in rows if not row["written"])


# Each location is (env-file label, literal key in that file). The key name
# can differ from the credential's own key where a file names it something
# else; a rotation writes every listed location with the same value.
CREDENTIAL_GROUPS: list[dict[str, Any]] = [
    {
        "key": "comms_bearer_token",
        "label": "Hive Comms bearer token",
        "rotatable": True,
        "note": (
            "Guards every comms route — one secret, one name, written to "
            "each location below. The admin token is a separate credential "
            "guarding the admin-only routes; rotating one does not rotate "
            "the other."
        ),
        "locations": [
            ("nervous_system", "COMMS_BEARER_TOKEN"),
            ("hive_mind", "COMMS_BEARER_TOKEN"),
            ("skippy", "COMMS_BEARER_TOKEN"),
            ("mordecai", "COMMS_BEARER_TOKEN"),
            ("hive_tools", "COMMS_BEARER_TOKEN"),
        ],
        "restart_targets": [
            "hive-comms container",
            "containerized minds (ada/bob/bilby/nagatha — docker compose up -d)",
            "skippy.service",
            "mordecai.service",
            "hive-tools container",
        ],
    },
    {
        "key": "comms_admin_bearer_token",
        "label": "Hive Comms admin token",
        "rotatable": True,
        "locations": [
            ("nervous_system", "COMMS_ADMIN_BEARER_TOKEN"),
            ("skippy", "COMMS_ADMIN_BEARER_TOKEN"),
            ("mordecai", "COMMS_ADMIN_BEARER_TOKEN"),
        ],
        "restart_targets": ["hive-comms container", "skippy.service", "mordecai.service"],
    },
    {
        "key": "lucent_bearer_token",
        "label": "Lucent bearer token",
        "rotatable": True,
        "locations": [
            ("nervous_system", "LUCENT_BEARER_TOKEN"),
            ("hive_mind", "LUCENT_BEARER_TOKEN"),
            ("skippy", "LUCENT_BEARER_TOKEN"),
            ("mordecai", "LUCENT_BEARER_TOKEN"),
        ],
        "restart_targets": [
            "hive-lucent (nervous-system container)",
            "containerized minds",
            "skippy.service",
            "mordecai.service",
        ],
    },
    {
        "key": "lucent_admin_bearer_token",
        "label": "Lucent admin token",
        "rotatable": True,
        "locations": [
            ("nervous_system", "LUCENT_ADMIN_BEARER_TOKEN"),
            ("hive_tools", "LUCENT_ADMIN_BEARER_TOKEN"),
        ],
        "restart_targets": ["hive-lucent (nervous-system container)", "hive-tools container"],
    },
    {
        "key": "hive_tools_token",
        "label": "Hive Tools API token",
        "rotatable": False,
        "manual_reason": (
            "Issued per-consumer from hive-tools' own token store (sqlite "
            "api_tokens table, sha256-hashed) — there is no symmetric "
            "env value to fan out. Mint a replacement via hive-tools' "
            "token admin, then update each consumer's HIVE_TOOLS_TOKEN by hand."
        ),
        "locations": [
            ("hive_mind", "HIVE_TOOLS_TOKEN"),
            ("skippy", "HIVE_TOOLS_TOKEN"),
            ("mordecai", "HIVE_TOOLS_TOKEN"),
        ],
        "restart_targets": [],
    },
    {
        "key": "github_pat",
        "label": "GitHub personal access token",
        "rotatable": False,
        "settable": True,
        "note": (
            "GitHub has no rotation API — generate the replacement in the "
            "GitHub UI, then paste it here. It is verified against "
            "api.github.com before ~/.git-credentials and gh's hosts.yml "
            "are touched. No restarts: both are read per invocation."
        ),
        "locations": [],
        "restart_targets": [],
    },
]

_GIT_CREDENTIALS_GITHUB_RE = re.compile(
    r"^https://(?P<user>[^:@/]+)(?::(?P<token>[^@/]*))?@github\.com$", flags=re.MULTILINE
)
_GH_HOSTS_OAUTH_RE = re.compile(r"^(?P<prefix>\s*oauth_token:\s*)(?P<token>\S+)\s*$", flags=re.MULTILINE)
_GH_HOSTS_HOST_RE = re.compile(r"^(?P<host>[^\s#][^:]*):\s*$", flags=re.MULTILINE)

GH_HOSTS_HOST = "github.com"


def _gh_hosts_github_block(text: str) -> tuple[int, int]:
    """The span of the github.com host block in gh's hosts.yml.

    hosts.yml is keyed by host at column zero, and gh writes oauth_token twice
    inside each block — once under users.<login>, once at the host level. An
    unanchored oauth_token match therefore reaches an enterprise host or a
    second account and overwrites its token with github.com's, destroying a
    credential this console was never asked to touch and reporting the count
    as evidence of thoroughness. Returns (0, 0) when there is no such block.
    """
    for match in _GH_HOSTS_HOST_RE.finditer(text):
        if match.group("host").strip() != GH_HOSTS_HOST:
            continue
        start = match.end()
        following = _GH_HOSTS_HOST_RE.search(text, start)
        return start, following.start() if following else len(text)
    return 0, 0


def _replace_github_oauth(text: str, token: str) -> tuple[str, int]:
    start, end = _gh_hosts_github_block(text)
    if start == end:
        return text, 0
    block, count = _GH_HOSTS_OAUTH_RE.subn(
        lambda match: f"{match.group('prefix')}{token}", text[start:end]
    )
    return text[:start] + block + text[end:], count


def _github_oauth_tokens(text: str) -> list[str]:
    start, end = _gh_hosts_github_block(text)
    if start == end:
        return []
    return [match.group("token") for match in _GH_HOSTS_OAUTH_RE.finditer(text[start:end])]


def _github_pat_locations() -> tuple[list[dict[str, Any]], bool]:
    rows = []
    values_seen = set()

    unreadable_seen = False

    token = None
    git_unreadable = False
    if GIT_CREDENTIALS_PATH.is_file():
        try:
            match = _GIT_CREDENTIALS_GITHUB_RE.search(_read_text(GIT_CREDENTIALS_PATH))
        except Unreadable:
            git_unreadable = unreadable_seen = True
        else:
            token = match.group("token") if match else None
    if token:
        values_seen.add(token)
    rows.append(
        {
            "file": "git_credentials",
            "path": str(GIT_CREDENTIALS_PATH),
            "env_key": "github.com",
            "present": bool(token),
            "unreadable": git_unreadable,
            "preview": _redact(token),
        }
    )

    hosts_tokens: list[str] = []
    hosts_unreadable = False
    if GH_HOSTS_PATH.is_file():
        try:
            hosts_tokens = _github_oauth_tokens(_read_text(GH_HOSTS_PATH))
        except Unreadable:
            hosts_unreadable = unreadable_seen = True
    values_seen.update(t for t in hosts_tokens if t)
    rows.append(
        {
            "file": "gh_hosts",
            "path": str(GH_HOSTS_PATH),
            "env_key": f"oauth_token x{len(hosts_tokens)}" if hosts_tokens else "oauth_token",
            "present": bool(hosts_tokens),
            "unreadable": hosts_unreadable,
            "preview": _redact(hosts_tokens[0]) if hosts_tokens else None,
        }
    )
    return rows, len(values_seen) <= 1 and not unreadable_seen


def _verify_github_pat(token: str) -> str:
    """Ask GitHub who this token belongs to. Raises ValueError if it's no good."""
    request = urllib.request.Request(
        GITHUB_API_USER_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "hive-console-credentials",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response).get("login", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError("GitHub rejected that token (401) — nothing was written") from exc
        raise ValueError(f"GitHub API answered {exc.code} — nothing was written") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach GitHub to verify the token: {exc.reason}") from exc


def _substitute_github_token(
    path: Path,
    label: str,
    key: str,
    rewrite: Any,
    absent_reason: str,
    count_suffix: bool = False,
) -> dict[str, Any]:
    """Rewrite every match in one credential file. Never raises.

    count_suffix reports how many occurrences were replaced, matching what the
    status listing shows for that file: gh's hosts.yml carries the token once
    per user plus once at the host level, and a bare "written" would hide a
    file that took one of three.
    """
    row: dict[str, Any] = {
        "file": label,
        "path": str(path),
        "env_key": key,
        "written": False,
        "reason": None,
    }
    reason = _unwritable_reason(path)
    if reason is not None:
        row["reason"] = reason
        return row
    try:
        text = path.read_text()
        updated, count = rewrite(text)
        if not count:
            row["reason"] = absent_reason
            return row
        _write_file_in_place(path, updated, label, key, text)
    except OSError as exc:
        row["reason"] = f"could not be written: {exc.strerror or exc}"
        return row
    except UnicodeDecodeError:
        row["reason"] = "file is not readable as text"
        return row
    row["env_key"] = f"{key} x{count}" if count_suffix else key
    row["written"] = True
    return row


def set_github_pat(token: str) -> dict[str, Any]:
    token = token.strip()
    # isascii before isspace: a paste out of a browser or a password manager
    # picks up zero-width spaces and smart quotes, which are not isspace() and
    # survive to urllib, where building the Authorization header raises
    # UnicodeEncodeError — uncaught by the HTTPError/URLError handlers below,
    # so the operator gets a 500 instead of being told the paste is bad.
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("That doesn't look like a token")

    login = _verify_github_pat(token)

    # Two files, two atomic actions, reported the same way a rotation's five
    # are. git's store and gh's hosts.yml are written by different tools and
    # go missing independently; one succeeding says nothing about the other.
    with _WRITE_LOCK:
        locations = [
            _substitute_github_token(
                GIT_CREDENTIALS_PATH,
                "git_credentials",
                "github.com",
                lambda text: _GIT_CREDENTIALS_GITHUB_RE.subn(
                    lambda m: f"https://{m.group('user')}:{token}@github.com",  # secret-guard: allow — template, not a value
                    text,
                ),
                "no github.com entry in this file",
            ),
            _substitute_github_token(
                GH_HOSTS_PATH,
                "gh_hosts",
                "oauth_token",
                lambda text: _replace_github_oauth(text, token),
                f"no {GH_HOSTS_HOST} oauth_token entry in this file",
                count_suffix=True,
            ),
        ]
    written = [
        {"file": row["file"], "env_key": row["env_key"]} for row in locations if row["written"]
    ]

    if not written:
        raise ValueError(
            "Token verified, but neither credential file could take it — "
            + _summarise(locations)
        )

    return {
        "key": "github_pat",
        "label": "GitHub personal access token",
        "login": login,
        "preview": _redact(token),
        "locations": locations,
        "written": written,
        "complete": all(row["written"] for row in locations),
        "restart_targets": [],
    }


def _group_status(group: dict[str, Any]) -> dict[str, Any]:
    if group["key"] == "github_pat":
        location_rows, in_sync = _github_pat_locations()
    else:
        location_rows = []
        values_seen = set()
        blank_seen = False
        unreadable_seen = False
        for file_label, env_key in group["locations"]:
            path = ENV_FILES[file_label]
            try:
                value = _read_env_value(path, env_key)
            except Unreadable:
                unreadable_seen = True
                value = None
                unreadable = True
            else:
                unreadable = False
            # A key present with no value is not a configured credential. It
            # used to render as "set" — preview is empty, and the template
            # falls back to that word — over a hive holding no shared secret
            # at all, with every location agreeing and in_sync reading true.
            if value:
                values_seen.add(value)
            elif value == "":
                blank_seen = True
            location_rows.append(
                {
                    "file": file_label,
                    "path": str(path),
                    "env_key": env_key,
                    "present": bool(value),
                    "unreadable": unreadable,
                    "preview": _redact(value),
                }
            )
        in_sync = len(values_seen) <= 1 and not blank_seen and not unreadable_seen
    return {
        "key": group["key"],
        "label": group["label"],
        "rotatable": group["rotatable"],
        "settable": group.get("settable", False),
        "note": group.get("note"),
        "manual_reason": group.get("manual_reason"),
        "restart_targets": group["restart_targets"],
        "locations": location_rows,
        "in_sync": in_sync,
    }


def list_credentials() -> list[dict[str, Any]]:
    return [_group_status(group) for group in CREDENTIAL_GROUPS]


def rotate_credential(key: str) -> dict[str, Any]:
    group = next((g for g in CREDENTIAL_GROUPS if g["key"] == key), None)
    if group is None:
        raise ValueError(f"unknown credential: {key!r}")
    if not group["rotatable"]:
        raise ValueError(group.get("manual_reason") or f"{group['label']} has no rotation API")

    new_value = secrets.token_urlsafe(32)
    # Five files are five operations, not one. Each is attempted regardless of
    # what the last one did, and each reports its own outcome: a rotation that
    # reached four of five and called itself a success is how one consumer ends
    # up holding a token the rest of the hive has stopped honouring.
    with _WRITE_LOCK:
        locations = [
            _write_location(ENV_FILES[file_label], env_key, new_value, file_label)
            for file_label, env_key in group["locations"]
        ]
    written = [
        {"file": row["file"], "env_key": row["env_key"]} for row in locations if row["written"]
    ]

    if not written:
        raise ValueError(
            f"{group['label']} was written to no location — {_summarise(locations)}"
        )

    return {
        "key": group["key"],
        "label": group["label"],
        "preview": _redact(new_value),
        "locations": locations,
        "written": written,
        "complete": all(row["written"] for row in locations),
        "restart_targets": group["restart_targets"],
    }
