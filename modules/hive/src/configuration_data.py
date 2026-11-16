"""Durable non-secret configuration and narrow local Mind edits."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import secrets
import shutil
import socket
import stat
import time
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

import store

HIVE_MIND_MINDS_DIR = Path("/minds_hive_mind")
HIVE_MIND_MINDS_CONFIG_DIR = Path("/minds_hive_mind_config")
EDGE_MINDS_DIR = Path("/minds_edge")

_CONFIGURABLE_CAPABILITY_IDS = {"inference-proxy", "ollama", "pihole"}
_MIND_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


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


async def set_capability_endpoint(node_id: str, endpoint: str) -> str:
    if node_id not in _CONFIGURABLE_CAPABILITY_IDS:
        raise ValueError(
            "This Capability Node does not support endpoint configuration")
    normalized = normalize_endpoint(endpoint)
    await store.set_capability_endpoint(node_id, normalized, int(time.time()))
    return normalized


async def get_capability_endpoint(node_id: str) -> str | None:
    row = await store.capability_endpoint(node_id)
    return str(row["endpoint"]) if row else None


async def clear_capability_endpoint(node_id: str) -> bool:
    if node_id not in _CONFIGURABLE_CAPABILITY_IDS:
        raise ValueError(
            "This Capability Node does not support endpoint configuration")
    return await store.clear_capability_endpoint(node_id) > 0


async def set_edge_mind_metadata(
    name: str, *, profile: str, role: str, deployment: str
) -> None:
    normalized = name.strip().lower()
    if not _MIND_NAME_RE.fullmatch(normalized):
        raise ValueError("Invalid Mind name")
    await store.set_edge_mind(normalized, profile, role, deployment,
                             int(time.time()))


async def get_edge_mind_metadata(name: str) -> dict[str, str] | None:
    row = await store.edge_mind(name.strip().lower())
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
#
# The key is written only when one was given for a non-proxy Ollama host. On
# the proxy the mind logs in with its own proxy key, which its compose file
# supplies; a placeholder here would be applied over it at every spawn.
_OLLAMA_ENV = {
    "claude_cli": lambda base, key: {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": base,
        **({"ANTHROPIC_AUTH_TOKEN": key} if key else {}),
    },
    "codex_cli": lambda base, key: {
        "OLLAMA_BASE_URL": f"{base}/v1",
        **({"OPENAI_API_KEY": key} if key else {}),
    },
}

DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:8899"

#: What a mind created here is built from: the published hive-mind tree, and
#: only that. Built from the host checkout instead, the image carries every
#: file `.dockerignore` misses - every other mind's folder, inference proxy
#: keys in their runtime.yaml among them - into a container its owner can
#: read. Overridable for an install whose hive-mind lives elsewhere.
DEFAULT_MIND_BUILD_CONTEXT = "https://github.com/danielstewart77/hive-mind.git#master"

#: The image stage whose code belongs to root, so the mind cannot rewrite the
#: server that receives the gateway's and the console's calls.
MIND_BUILD_TARGET = "locked-mind"

#: The user a mind's container runs as (the image's `hivemind`). Its folder
#: is handed to it, because this console writes as root.
DEFAULT_MIND_UID = 1000

#: The credential the gateway presents on every call it makes to a mind,
#: in the file the mind reads it from (hive-mind `minds/runtime_api.py`).
SESSION_TOKEN_FILENAME = "session_token"


_SESSION_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def mind_session_token(name: str) -> str:
    """The session credential a scaffolded mind will present, or "".

    Minted with the scaffold so the console can register the mind with it:
    a mind created here holds no gateway admin credential, so it cannot
    register itself, and the gateway learns the credential from no one else.
    Read as the console wrote it - a regular file, never through a link, in
    the shape `secrets.token_urlsafe(32)` mints - or not at all: this console
    reads as root, and whatever it reads here it hands to the gateway.
    """
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    path = HIVE_MIND_MINDS_CONFIG_DIR / name / SESSION_TOKEN_FILENAME
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    with os.fdopen(handle) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return ""
        value = stream.read(128).strip()
    return value if _SESSION_TOKEN_RE.fullmatch(value) else ""


def internal_mind_exists(name: str) -> bool:
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    return (HIVE_MIND_MINDS_CONFIG_DIR / name / "runtime.yaml").is_file()


# A console username as setup.sh and comms accept it.
OWNER_RE = re.compile(r"[a-z0-9][a-z0-9._@-]{0,63}")


def _comms_variable(name: str) -> str:
    """The variable this mind's gateway value is written under.

    Asked of the module that writes it rather than spelled again here. A name
    may carry hyphens and a `.env` key may not, so two copies of the rule is a
    compose file reading a variable the mint never wrote — and a container that
    comes up holding nothing.
    """
    import holder_credentials

    return holder_credentials.variable_name(
        holder_credentials.SERVICES["comms"], name
    )


def _proxy_variable(name: str) -> str:
    import holder_credentials

    return holder_credentials.variable_name(
        holder_credentials.SERVICES["proxy"], name
    )


def _lucent_variable(name: str) -> str:
    import holder_credentials

    return holder_credentials.variable_name(
        holder_credentials.SERVICES["lucent"], name
    )


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
    if os.path.lexists(mind_dir):
        # Any folder at all, not only one holding a runtime.yaml: a tracked
        # one (`harness`, `example`) or the remains of an earlier mind, whose
        # links this console, writing as root, would follow.
        raise ValueError(f"minds/{name} already exists")
    uid = _mind_uid()

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
        # Created locked down: the console sends this mind no admin
        # credential, and its folder, runtime.yaml and compose file are
        # root's. Read by the console, never by the mind.
        "locked_down": True,
    }
    if provider == "ollama":
        base = (ollama_base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/")
        runtime["env"] = _OLLAMA_ENV[harness](base, ollama_api_key)

    own = f"/usr/src/app/minds/{name}"
    host = f"${{HOST_PROJECT_DIR:-.}}/minds/{name}"
    compose: dict[str, Any] = {
        "services": {
            name: {
                # The published tree, never the host checkout: see
                # DEFAULT_MIND_BUILD_CONTEXT. The code is the image's, so a
                # change reaches this mind when it is rebuilt.
                "build": {
                    "context": os.getenv("HIVE_MIND_BUILD_CONTEXT", "").strip()
                    or DEFAULT_MIND_BUILD_CONTEXT,
                    "dockerfile": "Dockerfile",
                    "target": MIND_BUILD_TARGET,
                },
                "container_name": f"hive-mind-{name}",
                "working_dir": "/usr/src/app",
                "environment": [
                    f"MIND_NAME={name}",
                    f"MIND_ID={mind_id}",
                    f"{shape['config_dir_var']}={config_dir}",
                    "PYTHON_KEYRING_BACKEND=core.keyring_backend.HiveMindKeyring",
                    "PYTHONNOUSERSITE=1",
                    # Its own keyring, in its own folder: the hive's shared
                    # one holds other minds' secrets.
                    f"KEY_RING={own}/data/keyring",
                    "LUCENT_URL=http://hive-lucent:8424",
                    "LUCENT_URL_SELF=http://hive-lucent:8424",
                    # This mind's own value, not the hive's. Both services
                    # key credentials per holder and refuse one another
                    # holder already has, so a mind interpolating the shared
                    # variable comes up holding something nothing accepts.
                    # The left-hand name is what every reader inside the mind
                    # resolves by and does not change; only where the value
                    # comes from does.
                    f"LUCENT_BEARER_TOKEN=${{{_lucent_variable(name)}}}",
                    # No Hive Tools token. The shared one is Skippy's, and a
                    # mind holding it reaches Daniel's Google tools as him.
                    # A mind whose owner is not the owner of the tool
                    # credentials must never reach them (DECIDED 2026-11-15,
                    # Daniel); it gets its own when every service accepts the
                    # mind's one credential.
                    "HIVE_TOOLS_URL=http://hive-tools:9421",
                    "COMMS_URL=http://hive-comms:8424",
                    f"COMMS_BEARER_TOKEN=${{{_comms_variable(name)}}}",
                    # No gateway admin credential: it opens every person's
                    # conversations and mints anyone's credentials. The
                    # console registers this mind, with the session
                    # credential minted beside its runtime.yaml.
                    "HIVEMIND_BROKER_URL=http://hive-comms:8424",
                    # Every harness logs in to the inference proxy with this
                    # mind's own key, minted when it was created, so every
                    # call is metered to it (story 2.1.9) - never a host
                    # login, and never another mind's key.
                    f"INFERENCE_PROXY_URL={DEFAULT_OLLAMA_BASE_URL}",
                    f"ANTHROPIC_BASE_URL={DEFAULT_OLLAMA_BASE_URL}",
                    f"ANTHROPIC_AUTH_TOKEN=${{{_proxy_variable(name)}}}",
                    "ANTHROPIC_API_KEY=",
                    f"OPENAI_BASE_URL={DEFAULT_OLLAMA_BASE_URL}/v1",
                    f"OPENAI_API_KEY=${{{_proxy_variable(name)}}}",
                    f"DSH_PROXY_BASE_URL={DEFAULT_OLLAMA_BASE_URL}/v1",
                    f"DSH_API_KEY=${{{_proxy_variable(name)}}}",
                    "GATEWAY_URL=http://server:8420",
                    "SPECS_DIR=/usr/src/app/specs/data-classes",
                    f"AUTO_REMEMBER_LOG_DIR=/usr/src/app/minds/{name}/data/auto-remember",
                ],
                # Its own folder and nothing else of the host: not the
                # hive_mind checkout, its .env, the shared keyring, or any
                # other mind (DECIDED 2026-11-15, Daniel: a mind not owned by
                # him never reaches his credentials). Of that folder, only
                # its harness home and data are its to write; its runtime.yaml
                # and session credential are read-only, and its compose file
                # is not in the container at all, so it cannot rewrite what
                # it is started with or what the console registers it as.
                "volumes": [
                    f"{host}/runtime.yaml:{own}/runtime.yaml:ro",
                    f"{host}/{SESSION_TOKEN_FILENAME}:{own}/{SESSION_TOKEN_FILENAME}:ro",
                    f"{host}/{shape['config_dir_name']}:{config_dir}:rw",
                    f"{host}/data:{own}/data:rw",
                    # Its skills: synced here at start and installed into.
                    f"{host}/reference:{own}/reference:rw",
                ],
                "restart": "unless-stopped",
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "networks": ["hivemind"],
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
                "tmpfs": ["/tmp"],
                # The mind server, as every container mind here runs; it
                # picks the harness from runtime.yaml. The harness modules
                # have no entry point of their own.
                "command": ["/opt/venv/bin/python3", "-m", "minds.mind_server"],
            }
        }
    }
    # No shared codex volume: every codex container on this host can write
    # it, so a mind holding it could replace the codex binary Daniel's minds
    # run. The image carries its own codex.

    # The folder is the lock: a second create of the same name, or one racing
    # this, fails here rather than overwriting a runtime.yaml whose mind_id
    # has already been minted credentials. A create that fails part way
    # takes its folder with it, so it can simply be run again.
    mind_dir.mkdir(parents=True, exist_ok=False)
    try:
        container_dir = mind_dir / "container"
        container_dir.mkdir()
        runtime_path = mind_dir / "runtime.yaml"
        compose_path = container_dir / "compose.yaml"
        runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False))
        compose_path.write_text(yaml.safe_dump(compose, sort_keys=False))
        # What the mind writes, and only that, is the mind's.
        for writable in (shape["config_dir_name"], "data", "reference"):
            (mind_dir / writable).mkdir()
            os.chown(mind_dir / writable, uid, uid)
        token_path = mind_dir / SESSION_TOKEN_FILENAME
        handle = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(handle, "w") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
        # The mind reads it; it is mounted read-only, so owning it is not
        # being able to change it.
        os.chown(token_path, uid, uid)
    except BaseException:
        shutil.rmtree(mind_dir, ignore_errors=True)
        raise
    return {"runtime": str(runtime_path), "compose": str(compose_path)}


def _mind_uid() -> int:
    """The uid a mind's container runs as; parsed before anything is written."""
    raw = os.getenv("HIVE_MIND_UID", "").strip()
    try:
        return int(raw) if raw else DEFAULT_MIND_UID
    except ValueError as exc:
        raise ValueError(f"HIVE_MIND_UID is not a uid: {raw!r}") from exc


def is_locked_down(name: str) -> bool:
    """Whether a mind on this host was created locked down (its root-owned
    runtime.yaml says so). Never follows a link."""
    if not _MIND_NAME_RE.fullmatch(name or ""):
        return False
    path = HIVE_MIND_MINDS_CONFIG_DIR / name / "runtime.yaml"
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        loaded = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(loaded, dict) and loaded.get("locked_down") is True


def locked_down_minds() -> list[str]:
    try:
        names = sorted(p.name for p in HIVE_MIND_MINDS_CONFIG_DIR.iterdir())
    except OSError:
        return []
    return [name for name in names if is_locked_down(name)]


def name_is_a_host_already(name: str) -> bool:
    """A name another container on this network already answers to, or one
    the hive's own services are named in. A mind's name is its address on
    that network, so a mind named `hive-comms` would answer some of the calls
    meant for the gateway - the admin credential among them."""
    lowered = (name or "").strip().lower()
    return lowered.startswith("hive") or bool(_addresses(lowered))


def _locked_identities() -> tuple[set[str], set[str]]:
    names = set(locked_down_minds())
    ids = set()
    for name in names:
        try:
            loaded = yaml.safe_load(
                (HIVE_MIND_MINDS_CONFIG_DIR / name / "runtime.yaml").read_text()) or {}
            ids.add(str(loaded.get("mind_id") or ""))
        except (OSError, yaml.YAMLError):
            continue
    ids.discard("")
    return names, ids


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit((url or "").strip())
    try:
        port = parts.port
    except ValueError:
        port = -1
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


async def withholds_admin_credential(url: str) -> bool:
    """Whether the hive's admin credential must not be sent to `url`.

    Yes when the address is one a locked-down mind's registry row claims,
    whatever it is: such a mind can re-register itself with the gateway at
    any address - a server of its own, or another mind's, which the console
    would then act on for it. And yes when the address leads to such a
    mind's container. No locked-down mind, no question. A registry that
    cannot be read while one exists is answered yes.
    """
    names, ids = _locked_identities()
    if not names:
        return False
    import route_access

    rows = await route_access.registered_minds()
    if rows is None:
        return True
    target = _origin(url)
    for row in rows:
        mine = str(row.get("name") or "").lower() in names or str(row.get("id") or "") in ids
        if mine and _origin(str(row.get("gateway_url") or "")) == target:
            return True
    return await asyncio.to_thread(_leads_to_locked_container, target[1], names)


def _leads_to_locked_container(host: str, names: set[str]) -> bool:
    target = _addresses(host)
    if not target:
        return True
    for name in names:
        if target & (_addresses(name) | _addresses(f"hive-mind-{name}")):
            return True
    return False


def _addresses(host: str) -> set[str]:
    """Where a host resolves, each address in one form (an IPv4 address
    written as IPv6 is the IPv4 address)."""
    if not host:
        return set()
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return set()
    normal = set()
    for raw in found:
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            continue
        mapped = getattr(address, "ipv4_mapped", None)
        normal.add(str(mapped or address))
    return normal
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return set()
