"""Durable non-secret configuration and narrow local Mind edits."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

from config import BASE_DIR

HIVE_MIND_MINDS_DIR = Path("/minds_hive_mind")
HIVE_MIND_MINDS_CONFIG_DIR = Path("/minds_hive_mind_config")

_CONFIGURABLE_CAPABILITY_IDS = {"ollama", "pihole"}
_MIND_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


def _db_path() -> Path:
    return Path(os.getenv("HIVE_DB_PATH", str(BASE_DIR.parent / "data" / "hive.db")))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_node_settings (
            node_id TEXT PRIMARY KEY,
            endpoint TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS edge_mind_settings (
            name TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            role TEXT NOT NULL,
            deployment TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def normalize_endpoint(endpoint: str) -> str:
    normalized = endpoint.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Endpoint must be an HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("Endpoint credentials must not be stored in the URL")
    if parsed.query or parsed.fragment:
        raise ValueError("Endpoint must not include a query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Endpoint port is invalid")
    return normalized


def set_capability_endpoint(node_id: str, endpoint: str) -> str:
    if node_id not in _CONFIGURABLE_CAPABILITY_IDS:
        raise ValueError("This Capability Node does not support endpoint configuration")
    normalized = normalize_endpoint(endpoint)
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO capability_node_settings (node_id, endpoint, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                endpoint = excluded.endpoint,
                updated_at = excluded.updated_at
            """,
            (node_id, normalized, int(time.time())),
        )
        connection.commit()
    finally:
        connection.close()
    return normalized


def get_capability_endpoint(node_id: str) -> str | None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT endpoint FROM capability_node_settings WHERE node_id = ?",
            (node_id,),
        ).fetchone()
    finally:
        connection.close()
    return str(row["endpoint"]) if row else None


def clear_capability_endpoint(node_id: str) -> bool:
    if node_id not in _CONFIGURABLE_CAPABILITY_IDS:
        raise ValueError("This Capability Node does not support endpoint configuration")
    connection = _connect()
    try:
        cursor = connection.execute(
            "DELETE FROM capability_node_settings WHERE node_id = ?", (node_id,)
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def set_edge_mind_metadata(
    name: str, *, profile: str, role: str, deployment: str
) -> None:
    normalized = name.strip().lower()
    if not _MIND_NAME_RE.fullmatch(normalized):
        raise ValueError("Invalid Mind name")
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO edge_mind_settings
                (name, profile, role, deployment, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                profile = excluded.profile,
                role = excluded.role,
                deployment = excluded.deployment,
                updated_at = excluded.updated_at
            """,
            (normalized, profile, role, deployment, int(time.time())),
        )
        connection.commit()
    finally:
        connection.close()


def get_edge_mind_metadata(name: str) -> dict[str, str] | None:
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT profile, role, deployment
            FROM edge_mind_settings
            WHERE name = ?
            """,
            (name.strip().lower(),),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    return {
        "profile": str(row["profile"]),
        "role": str(row["role"]),
        "deployment": str(row["deployment"]),
    }


def _runtime_paths(name: str) -> tuple[Path, Path]:
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    return (
        HIVE_MIND_MINDS_DIR / name / "runtime.yaml",
        HIVE_MIND_MINDS_CONFIG_DIR / name / "runtime.yaml",
    )


def local_runtime_configuration(name: str) -> dict[str, Any]:
    source, _ = _runtime_paths(name)
    if not source.is_file():
        raise ValueError("This Mind has no local runtime configuration")
    try:
        configuration = yaml.safe_load(source.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Runtime configuration is invalid: {exc}") from exc
    if not isinstance(configuration, dict):
        raise ValueError("Runtime configuration must be a mapping")
    if configuration.get("remote") is True:
        raise ValueError("This Mind is remote and must be configured on its host")
    return configuration


def update_local_runtime_model(name: str, default_model: str) -> dict[str, Any]:
    if not _MODEL_NAME_RE.fullmatch(default_model):
        raise ValueError("Model name contains unsupported characters")
    source, target = _runtime_paths(name)
    configuration = local_runtime_configuration(name)
    source_text = target.read_text() if target.is_file() else source.read_text()
    replacement = f"default_model: {json.dumps(default_model)}"
    updated_text, count = re.subn(
        r"^default_model\s*:.*$", replacement, source_text, count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise ValueError("Runtime configuration has no default_model field")
    target.parent.mkdir(parents=True, exist_ok=True)
    target_metadata = target.stat() if target.exists() else source.stat()
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix="runtime-", suffix=".yaml", dir=target.parent
    )
    try:
        with os.fdopen(file_descriptor, "w") as temporary_file:
            temporary_file.write(updated_text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            os.fchmod(temporary_file.fileno(), stat.S_IMODE(target_metadata.st_mode))
            os.fchown(
                temporary_file.fileno(), target_metadata.st_uid, target_metadata.st_gid
            )
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    configuration["default_model"] = default_model
    return configuration
