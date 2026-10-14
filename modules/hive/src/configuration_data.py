"""Durable non-secret configuration and narrow local Mind edits."""

from __future__ import annotations

import os
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

from config import BASE_DIR

HIVE_MIND_MINDS_DIR = Path("/minds_hive_mind")
HIVE_MIND_MINDS_CONFIG_DIR = Path("/minds_hive_mind_config")
EDGE_MINDS_DIR = Path("/minds_edge")

_CONFIGURABLE_CAPABILITY_IDS = {"inference-proxy", "ollama", "pihole"}
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


def _runtime_path(name: str) -> Path:
    """Where a Mind on this host keeps its runtime.yaml, for reading.

    A containerized Mind's file lives under hive_mind's minds/; an edge Mind
    on this host keeps its own in its checkout, mounted at
    /minds_edge/<name>/. Both are read-only here — the console configures a
    Mind through its HTTP API (see mind_runtime.py), which is the one path
    that also reaches a Mind on another machine. This read is the bootstrap
    for registering a Mind that has never started.
    """
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    source = HIVE_MIND_MINDS_DIR / name / "runtime.yaml"
    return source if source.is_file() else EDGE_MINDS_DIR / name / "runtime.yaml"


def local_runtime_configuration(name: str) -> dict[str, Any]:
    source = _runtime_path(name)
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


# The harness-specific half of an internal mind's scaffold. Everything else
# below is shared: the two shapes differ only in which config-dir variable
# codex/claude reads, and which harness module the container runs.
_HARNESS_SCAFFOLD = {
    "claude_cli": {
        "config_dir_var": "CLAUDE_CONFIG_DIR",
        "config_dir_name": ".claude",
        "transport": "cli_stream_json",
    },
    "codex_cli": {
        "config_dir_var": "CODEX_HOME",
        "config_dir_name": ".codex",
        "transport": "codex_exec_json",
    },
}

# The provider switch is entirely this env block — the harness modules read
# `provider` but branch on these. Base URLs default to the host
# inference-proxy, which is how every Ollama mind in this hive is metered.
_OLLAMA_ENV = {
    "claude_cli": lambda base, key: {
        "ANTHROPIC_AUTH_TOKEN": key or "ollama",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": base,
    },
    "codex_cli": lambda base, key: {
        "OLLAMA_BASE_URL": f"{base}/v1",
        "OPENAI_API_KEY": key,
    },
}

DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:8899"


def internal_mind_exists(name: str) -> bool:
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    return (HIVE_MIND_MINDS_CONFIG_DIR / name / "runtime.yaml").is_file()


# A console username as setup.sh and comms accept it.
OWNER_RE = re.compile(r"[a-z0-9][a-z0-9._@-]{0,63}")


def scaffold_internal_mind(
    name: str,
    *,
    mind_id: str,
    harness: str,
    provider: str,
    default_model: str,
    owner: str,
    description: str = "",
    ollama_base_url: str = "",
    ollama_api_key: str = "",
) -> dict[str, str]:
    """Write minds/<name>/runtime.yaml and container/compose.yaml.

    Identity and soul prose are deliberately absent: those live in the
    knowledge graph and are authored conversationally. This writes only what
    the runtime needs to start.
    """
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    if harness not in _HARNESS_SCAFFOLD:
        raise ValueError("Unknown harness")
    if not _MODEL_NAME_RE.fullmatch(default_model):
        raise ValueError("Model name contains unsupported characters")
    if not OWNER_RE.fullmatch(owner):
        raise ValueError("Every Mind needs an owner: a lowercase username")
    mind_dir = HIVE_MIND_MINDS_CONFIG_DIR / name
    if (mind_dir / "runtime.yaml").exists():
        raise ValueError(f"minds/{name} already exists")

    shape = _HARNESS_SCAFFOLD[harness]
    config_dir = f"/usr/src/app/minds/{name}/{shape['config_dir_name']}"

    runtime: dict[str, Any] = {
        "name": name,
        "mind_id": mind_id,
        # The user this Mind belongs to. Comms refuses a first registration
        # without one; once registered, only an admin moves it.
        "owner": owner,
        "gateway_url": f"http://{name}:8420",
        "remote": False,
        "description": description or f"{name.capitalize()} — Hive Mind.",
        "harness": harness,
        "provider": provider,
        "default_model": default_model,
        "runtime_config_dir": config_dir,
        "resume_policy": "provider-local",
        "prompt_files": ["prompts/common.md", "prompts/harness.md"],
        "startup": {"skip_host_credentials": False, "sync_repo_config": True},
        "transport": {"type": shape["transport"]},
    }
    if provider == "ollama":
        base = (ollama_base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/")
        runtime["env"] = _OLLAMA_ENV[harness](base, ollama_api_key)

    compose: dict[str, Any] = {
        "services": {
            name: {
                "build": {"context": "../../..", "dockerfile": "Dockerfile"},
                "container_name": f"hive-mind-{name}",
                "working_dir": "/usr/src/app",
                "environment": [
                    f"MIND_NAME={name}",
                    f"MIND_ID={mind_id}",
                    f"{shape['config_dir_var']}={config_dir}",
                    "PYTHON_KEYRING_BACKEND=core.keyring_backend.HiveMindKeyring",
                    "PYTHONNOUSERSITE=1",
                    "KEY_RING=/usr/src/app/data/keyring",
                    "LUCENT_URL=http://hive-lucent:8424",
                    "LUCENT_URL_SELF=http://hive-lucent:8424",
                    "LUCENT_BEARER_TOKEN=${LUCENT_BEARER_TOKEN}",
                    "HIVE_TOOLS_URL=http://hive-tools:9421",
                    "HIVE_TOOLS_TOKEN=${HIVE_TOOLS_TOKEN}",
                    "COMMS_URL=http://hive-comms:8424",
                    "COMMS_BEARER_TOKEN=${COMMS_BEARER_TOKEN}",
                    # A Mind re-registers from its own runtime.yaml on every
                    # start, and POST /broker/minds is an admin route.
                    "COMMS_ADMIN_BEARER_TOKEN=${COMMS_ADMIN_BEARER_TOKEN}",
                    "HIVEMIND_BROKER_URL=http://hive-comms:8424",
                    "GATEWAY_URL=http://server:8420",
                    "SPECS_DIR=/usr/src/app/specs/data-classes",
                    f"AUTO_REMEMBER_LOG_DIR=/usr/src/app/minds/{name}/data/auto-remember",
                ],
                "volumes": ["${HOST_PROJECT_DIR:-.}:/usr/src/app:rw"],
                "restart": "unless-stopped",
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "networks": ["hivemind"],
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
                "tmpfs": ["/tmp"],
                "command": [
                    "/opt/venv/bin/python3",
                    "-m",
                    f"minds.harness.{harness}",
                ],
            }
        }
    }
    if harness == "codex_cli":
        # Shared with every other codex container on this host: one
        # `npm install -g @openai/codex` updates codex for all of them.
        compose["services"][name]["volumes"].append(
            "codex-global:/home/hivemind/.npm-global"
        )

    container_dir = mind_dir / "container"
    container_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = mind_dir / "runtime.yaml"
    compose_path = container_dir / "compose.yaml"
    runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False))
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False))
    return {"runtime": str(runtime_path), "compose": str(compose_path)}
