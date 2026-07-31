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
fan out to; GitHub PATs and the Planka password have no rotation API at
all. Both are listed as informational, non-rotatable entries so the tab is
honest about what it can and can't do.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any

ENV_FILES: dict[str, Path] = {
    "nervous_system": Path("/env_nervous_system/.env"),
    "hive_mind": Path("/env_hive_mind/.env"),
    "skippy": Path("/env_edge/skippy/.env"),
    "mordecai": Path("/env_edge/mordecai/.env"),
    "hive_tools": Path("/env_hive_tools/.env"),
}

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


def _write_env_value(path: Path, key: str, new_value: str) -> bool:
    """Replace KEY=... in place, atomically. Returns False if the key isn't in this file."""
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
    metadata = path.stat()
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w") as temporary_file:
            temporary_file.write(updated_text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            os.fchmod(temporary_file.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(temporary_file.fileno(), metadata.st_uid, metadata.st_gid)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
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
        "manual_reason": (
            "GitHub has no rotation API for classic or fine-grained PATs — "
            "generate a replacement in the GitHub UI, then `gh auth login` "
            "or update wherever it's stored locally."
        ),
        "locations": [],
        "restart_targets": [],
    },
]


def _group_status(group: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "key": group["key"],
        "label": group["label"],
        "rotatable": group["rotatable"],
        "manual_reason": group.get("manual_reason"),
        "restart_targets": group["restart_targets"],
        "locations": location_rows,
        "in_sync": len(values_seen) <= 1,
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
        if _write_env_value(path, env_key, new_value):
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
