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


def _read_env_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    match = re.search(
        _KEY_LINE_RE_TEMPLATE.format(key=re.escape(key)), path.read_text(), flags=re.MULTILINE
    )
    if match is None:
        return None
    return match.group(0).split("=", 1)[1]


def _redact(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 4:
        return "****"
    return f"...{value[-4:]}"


def _backup(text: str, label: str, key: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = BACKUP_DIR / f"{label}-{key}-{stamp}.env"
    destination.write_text(text)
    destination.chmod(0o600)


def _write_env_value(path: Path, key: str, new_value: str, label: str = "env") -> bool:
    """Replace KEY=... in place. Returns False if the key isn't in this file.

    The write preserves the inode (see BACKUP_DIR's note): mode and ownership
    are therefore untouched by construction.
    """
    if not path.is_file():
        return False
    text = path.read_text()
    updated_text, count = re.subn(
        _KEY_LINE_RE_TEMPLATE.format(key=re.escape(key)),
        f"{key}={new_value}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        return False
    _backup(text, label, key)
    with open(path, "w") as env_file:
        env_file.write(updated_text)
        env_file.flush()
        os.fsync(env_file.fileno())
    return True


# Each location is (env-file label, literal key in that file) — the key
# name isn't always the credential's own key: edge minds mirror
# COMMS_BEARER_TOKEN into a second literal line, HIVEMIND_BROKER_TOKEN,
# because bare-metal installs have no compose layer to alias it for them.
CREDENTIAL_GROUPS: list[dict[str, Any]] = [
    {
        "key": "comms_bearer_token",
        "label": "Hive Comms bearer token",
        "rotatable": True,
        "note": (
            "One secret, several names: edge minds mirror it as "
            "HIVEMIND_BROKER_TOKEN for their bare-metal broker clients, so "
            "that line is the same value by design, not a second credential. "
            "The admin token below IS a separate credential — it guards the "
            "admin-only comms routes."
        ),
        "locations": [
            ("nervous_system", "COMMS_BEARER_TOKEN"),
            ("hive_mind", "COMMS_BEARER_TOKEN"),
            ("skippy", "COMMS_BEARER_TOKEN"),
            ("skippy", "HIVEMIND_BROKER_TOKEN"),
            ("mordecai", "COMMS_BEARER_TOKEN"),
            ("mordecai", "HIVEMIND_BROKER_TOKEN"),
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


def _github_pat_locations() -> tuple[list[dict[str, Any]], bool]:
    rows = []
    values_seen = set()

    token = None
    if GIT_CREDENTIALS_PATH.is_file():
        match = _GIT_CREDENTIALS_GITHUB_RE.search(GIT_CREDENTIALS_PATH.read_text())
        token = match.group("token") if match else None
    if token:
        values_seen.add(token)
    rows.append(
        {
            "file": "git_credentials",
            "path": str(GIT_CREDENTIALS_PATH),
            "env_key": "github.com",
            "present": bool(token),
            "preview": _redact(token),
        }
    )

    hosts_tokens = (
        [m.group("token") for m in _GH_HOSTS_OAUTH_RE.finditer(GH_HOSTS_PATH.read_text())]
        if GH_HOSTS_PATH.is_file()
        else []
    )
    values_seen.update(hosts_tokens)
    rows.append(
        {
            "file": "gh_hosts",
            "path": str(GH_HOSTS_PATH),
            "env_key": f"oauth_token x{len(hosts_tokens)}" if hosts_tokens else "oauth_token",
            "present": bool(hosts_tokens),
            "preview": _redact(hosts_tokens[0]) if hosts_tokens else None,
        }
    )
    return rows, len(values_seen) <= 1


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


def set_github_pat(token: str) -> dict[str, Any]:
    token = token.strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("That doesn't look like a token")

    login = _verify_github_pat(token)

    written = []
    if GIT_CREDENTIALS_PATH.is_file():
        text = GIT_CREDENTIALS_PATH.read_text()
        updated, count = _GIT_CREDENTIALS_GITHUB_RE.subn(
            lambda m: f"https://{m.group('user')}:{token}@github.com", text  # secret-guard: allow — template, not a value
        )
        if count:
            _backup(text, "git_credentials", "github.com")
            with open(GIT_CREDENTIALS_PATH, "w") as f:
                f.write(updated)
                f.flush()
                os.fsync(f.fileno())
            written.append({"file": "git_credentials", "env_key": "github.com"})

    if GH_HOSTS_PATH.is_file():
        text = GH_HOSTS_PATH.read_text()
        updated, count = _GH_HOSTS_OAUTH_RE.subn(
            lambda m: f"{m.group('prefix')}{token}", text
        )
        if count:
            _backup(text, "gh_hosts", "oauth_token")
            with open(GH_HOSTS_PATH, "w") as f:
                f.write(updated)
                f.flush()
                os.fsync(f.fileno())
            written.append({"file": "gh_hosts", "env_key": f"oauth_token x{count}"})

    if not written:
        raise ValueError("Token verified, but neither credential file is mounted or has a github.com entry")

    return {
        "key": "github_pat",
        "label": "GitHub personal access token",
        "login": login,
        "preview": _redact(token),
        "written": written,
        "restart_targets": [],
    }


def _group_status(group: dict[str, Any]) -> dict[str, Any]:
    if group["key"] == "github_pat":
        location_rows, in_sync = _github_pat_locations()
    else:
        location_rows = []
        values_seen = set()
        for file_label, env_key in group["locations"]:
            path = ENV_FILES[file_label]
            value = _read_env_value(path, env_key)
            if value is not None:
                values_seen.add(value)
            location_rows.append(
                {
                    "file": file_label,
                    "path": str(path),
                    "env_key": env_key,
                    "present": value is not None,
                    "preview": _redact(value),
                }
            )
        in_sync = len(values_seen) <= 1
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
    written = []
    for file_label, env_key in group["locations"]:
        path = ENV_FILES[file_label]
        if _write_env_value(path, env_key, new_value, label=file_label):
            written.append({"file": file_label, "env_key": env_key})

    if not written:
        raise ValueError(f"{group['label']} was not found in any mounted env file")

    return {
        "key": group["key"],
        "label": group["label"],
        "preview": _redact(new_value),
        "written": written,
        "restart_targets": group["restart_targets"],
    }
