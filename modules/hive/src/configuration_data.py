"""Durable non-secret configuration and narrow local Mind edits."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import posixpath
import re
import secrets
import shutil
import socket
import stat
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

import store

HIVE_MIND_MINDS_DIR = Path("/minds_hive_mind")
HIVE_MIND_MINDS_CONFIG_DIR = Path("/minds_hive_mind_config")
EDGE_MINDS_DIR = Path("/minds_edge")

#: The stack file whose ``include:`` list is what makes a mind's own compose
#: fragment reachable. Scaffolding one without this writes a fragment no
#: `docker compose` invocation can see, which is why creating a mind used to
#: hand the operator a shell line running the `/generate-compose` skill.
#: Configurable, and otherwise under the host Dev root the console already
#: mounts — never a person's home written in here (story 2.1.19).
STACK_COMPOSE_FILENAME = "docker-compose.yml"

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
    if node_id == "inference-proxy":
        import provider_data
        connection = await provider_data.get_connection("hive-proxy")
        await provider_data.save_connection(dict(connection, base_url=normalized), "hive-proxy")
        return normalized
    await store.set_capability_endpoint(node_id, normalized, int(time.time()))
    return normalized


async def get_capability_endpoint(node_id: str) -> str | None:
    if node_id == "inference-proxy":
        state = await store.get_provider_connections()
        for connection in state.get("connections", []):
            if connection["id"] == "hive-proxy":
                return connection["base_url"]
    row = await store.capability_endpoint(node_id)
    return str(row["endpoint"]) if row else None


async def clear_capability_endpoint(node_id: str) -> bool:
    if node_id not in _CONFIGURABLE_CAPABILITY_IDS:
        raise ValueError(
            "This Capability Node does not support endpoint configuration")
    if node_id == "inference-proxy":
        import provider_data
        await provider_data.delete_connection("hive-proxy")
        return True
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
# below is shared; each harness declares its own home and transport.
_HARNESS_SCAFFOLD = {
    # dsh, the hive's default harness (Daniel, 2026-11-17). It is not in the
    # mind image: the built tree is mounted, and the container writes its
    # profile and hooks into this home before the mind server starts.
    "dsh": {
        "config_dir_var": "DSH_HOME",
        "config_dir_name": ".dsh",
        "transport": "dsh_exec_json",
    },
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

#: Where a mind's container sees the built dsh tree, and the two files in it
#: a dsh mind is told about (hive-mind `installer/adapters/dsh.py`).
DSH_MOUNT = "/opt/dsh"
DSH_BIN = f"{DSH_MOUNT}/apps/cli/lib/bin.js"
DSH_READ_LOG_CLI = (
    f"{DSH_MOUNT}/packages/session/session-persistence-jsonl/lib/read-log-cli.js")
DSH_PROFILE = "hive"

# The provider switch is entirely this env block — the harness modules read
# `provider` but branch on these. Base URLs default to the host
# inference-proxy, which is how every Ollama mind in this hive is metered.
#
# The key is written only when one was given for a non-proxy Ollama host. On
# the proxy the mind logs in with its own proxy key, which its compose file
# supplies; a placeholder here would be applied over it at every spawn.
_OLLAMA_ENV = {
    # DSH uses the compose environment's proxy endpoint for every provider.
    "dsh": lambda base, key: {},
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


def mind_proxy_base_url() -> str:
    """The inference proxy as a mind created here reaches it.

    An installed hive runs its own and names it in `HIVE_MIND_PROXY_URL`.
    Otherwise the address this console itself is given for the proxy,
    `INFERENCE_PROXY_URL`, and last this workstation's.
    """
    configured = (os.getenv("HIVE_MIND_PROXY_URL", "").strip()
                  or os.getenv("INFERENCE_PROXY_URL", "").strip()).rstrip("/")
    if configured.endswith("/v1"):
        configured = configured[: -len("/v1")]
    return configured or DEFAULT_OLLAMA_BASE_URL


def _mind_build_args() -> dict[str, str]:
    """Build arguments for a new mind's image: `HIVE_MIND_BUILD_ARGS`, a JSON
    object. An installed hive behind a web proxy names it here, or the image's
    package installs cannot reach anything. Parsed before anything is written.
    """
    raw = os.getenv("HIVE_MIND_BUILD_ARGS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"HIVE_MIND_BUILD_ARGS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("HIVE_MIND_BUILD_ARGS must be a JSON object")
    return {str(key): str(value) for key, value in parsed.items()}

#: What a mind created here is built from: the published hive-mind tree at
#: the exact commit of the release this machine is on, and only that. Built
#: from the host checkout instead, the image carries every file `.dockerignore`
#: misses - every other mind's folder, inference proxy keys in their
#: runtime.yaml among them - into a container its owner can read. Built from a
#: branch, a mind made today and one made next week are different programs and
#: going back a release would not reach new minds.
#:
#: A release cannot name its own commit in its own files, so the identity is
#: read at run time from `.release-context` in the minds folder, which
#: `scripts/release.sh` and the installer write when they put the checkout on a
#: release: `<repository address>#<40-character commit>`. An operator's
#: `HIVE_MIND_BUILD_CONTEXT` still wins, for an install whose hive-mind lives
#: elsewhere or a staged deployment. With neither, creating a mind is refused,
#: saying so: never a branch.
RELEASE_CONTEXT_FILENAME = ".release-context"
_RELEASE_CONTEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^\s#]+|git@[^\s#:]+:[^\s#]+)#[0-9a-f]{40}")


def mind_build_context() -> str:
    explicit = os.getenv("HIVE_MIND_BUILD_CONTEXT", "").strip()
    if explicit:
        return explicit
    path = HIVE_MIND_MINDS_CONFIG_DIR / RELEASE_CONTEXT_FILENAME
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        handle = None
    if handle is not None:
        with os.fdopen(handle) as stream:
            if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                value = stream.read(512).strip()
                if _RELEASE_CONTEXT_RE.fullmatch(value):
                    return value
    raise ValueError(
        "This hive has no release to build a new Mind from: "
        f"minds/{RELEASE_CONTEXT_FILENAME} is missing or does not name a commit. "
        "Put the checkout on a release with scripts/release.sh (or the installer), "
        "which writes it; a Mind is never built from a moving branch.")


#: In-repository packages a mind's image installs from named build contexts
#: (`COPY --from=<name>`), and where each sits inside hive-mind.
MIND_BUILD_PACKAGE_CONTEXTS = {
    "hive_data": "modules/hive-data",
    "hive_surfaces": "modules/hive-surfaces",
}


def mind_build_contexts(context: str) -> dict[str, str]:
    """The named build contexts a mind's image needs, from the main context.

    The image copies `hive_data` and `hive_surfaces` from folders of hive-mind
    itself, so they are named at the same place the main context is: a git
    URL gets docker's subdirectory form on the same ref
    (`<url>#<ref>:modules/hive-surfaces`), a folder gets the subfolder. They
    always follow the main context, so the code and the packages it installs
    come from one revision. A URL already naming a subdirectory is left
    without them: it is not a hive-mind root.
    """
    out: dict[str, str] = {}
    for name, sub in MIND_BUILD_PACKAGE_CONTEXTS.items():
        if "://" in context or context.startswith("git@"):
            url, _, frag = context.partition("#")
            if ":" in frag:
                continue
            out[name] = f"{url}#{frag}:{sub}"
        else:
            out[name] = f"{context.rstrip('/')}/{sub}"
    return out


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


#: Where a mind's key folder is mounted, and the file in it (requirement 21).
#: The mind reads the key from there at each request (hive-mind's
#: core/mind_key.py); the compose file carries no key at all. Asked of the
#: module that writes the file, so the two cannot drift.
def key_folder_mount(name: str) -> str:
    """The volume line that gives this mind, and no other, its key folder
    (read-only). `HOST_KEY_DIR` is a folder outside the checkout, one
    subfolder per mind; it is required, so a compose file made without it is
    refused rather than mounting a folder docker would create as root."""
    import mind_key_files

    return f"${{HOST_KEY_DIR:?set HOST_KEY_DIR in .env}}/{name}:{mind_key_files.MOUNT}:ro"


def key_file_setting() -> str:
    """The environment line that names the key file inside the mind."""
    import mind_key_files

    return f"MIND_KEY_FILE={mind_key_files.MOUNT}/{mind_key_files.FILE_NAME}"


#: The rotation threshold a newly created Mind starts with: the percentage of
#: its conversation model's context window at which a conversation is handed
#: over. A runtime.yaml with no line means 100, which is no early rotation at
#: all, so a new Mind is always written one. Daniel, 2026-11-17: thirty "for
#: now" — this is the console's one copy of the figure. The others, which
#: change with it: the hive installer's constant of the same name (hive-mind,
#: installer/steps.py), minds/example/runtime.yaml in hive-mind and in
#: hive-edge-minds, the variable of the same name in hive-edge-minds
#: setup.sh, and the create-mind and add-mind skills in the hivemind plugin.
#: Two documents show the line as well: docs/multi-mind-architecture.md in
#: hive-mind and README.md in hive-edge-minds.
NEW_MIND_ROTATION_THRESHOLD_PERCENT = 30


#: Where a mind sees the install's one shared folder of generated skills, and
#: where that folder is on the host. Attached to every mind made here, read
#: only; `operator_attachment` makes it writable for the one that holds the
#: operator role. A mind made here otherwise sees only its own folder and a
#: copy of the published code, so a skill a role names that is generated on
#: this install could not reach it.
WORKFLOW_SKILLS_CONTAINER_PATH = "/usr/src/app/install/workflow-skills"
WORKFLOW_SKILLS_HOST_PATH = "${HOST_PROJECT_DIR:-.}/install/workflow-skills"


def workflow_skills_volume(writable: bool = False) -> str:
    mode = "rw" if writable else "ro"
    return f"{WORKFLOW_SKILLS_HOST_PATH}:{WORKFLOW_SKILLS_CONTAINER_PATH}:{mode}"


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
    connection: dict | None = None,
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
    # Refused before anything is written, not after a half-made folder.
    build_context = mind_build_context()
    uid = _mind_uid()
    build_args = _mind_build_args()
    proxy = mind_proxy_base_url()

    shape = _HARNESS_SCAFFOLD[harness]
    is_dsh = harness == "dsh"
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
        # Written, never left to the default: a file with no line rotates at
        # the whole window.
        "rotation_threshold_percent": NEW_MIND_ROTATION_THRESHOLD_PERCENT,
        "runtime_config_dir": config_dir,
        "resume_policy": "always" if is_dsh else "provider-local",
        "prompt_files": ["prompts/common.md", "prompts/harness.md"],
        "startup": {"skip_host_credentials": False, "sync_repo_config": True},
        "transport": {"type": shape["transport"]},
    }
    if is_dsh:
        runtime["dsh_profile"] = DSH_PROFILE
        runtime["dsh_bin"] = DSH_BIN
        # Nothing of a host login, and no repo configuration to sync: its
        # profile and hooks are written by its own container at start.
        runtime["startup"] = {"skip_host_credentials": True, "sync_repo_config": False}
    if provider == "ollama":
        base = (ollama_base_url or proxy).rstrip("/")
        provider_env = _OLLAMA_ENV[harness](base, ollama_api_key)
        if provider_env:
            runtime["env"] = provider_env
    if connection:
        from provider_data import inference_environment
        runtime["provider_connection"] = dict(connection)
        # Endpoint metadata travels with the Mind; credentials stay in its
        # own Compose environment, not in the public runtime configuration.
        runtime["env"] = {k: v for k, v in inference_environment(connection, "", harness).items()
                          if "KEY" not in k and "TOKEN" not in k}

    own = f"/usr/src/app/minds/{name}"
    host = f"${{HOST_PROJECT_DIR:-.}}/minds/{name}"
    compose: dict[str, Any] = {
        "services": {
            name: {
                # The published tree, never the host checkout: see
                # mind_build_context. The code is the image's, so a
                # change reaches this mind when it is rebuilt.
                "build": {
                    "context": build_context,
                    "additional_contexts": mind_build_contexts(build_context),
                    "dockerfile": "Dockerfile",
                    "target": MIND_BUILD_TARGET,
                    **({"args": build_args} if build_args else {}),
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
                    # The mind's one key, the value its owner pasted, is a
                    # FILE in a folder of its own that only this mind has
                    # mounted (below). The mind reads it at each request, so a
                    # replaced key needs no recreate, and the key is in no
                    # environment and no .env. The gateway, the memory store,
                    # the tools and the proxy each accept it as this mind; the
                    # mind sets every name its harnesses, hooks and skills
                    # read from it (core/mind_key.py).
                    key_file_setting(),
                    "LUCENT_URL=http://hive-lucent:8424",
                    "LUCENT_URL_SELF=http://hive-lucent:8424",
                    "HIVE_TOOLS_URL=http://hive-tools:9421",
                    "COMMS_URL=http://hive-comms:8424",
                    # No gateway admin credential: it opens every person's
                    # conversations and mints anyone's credentials. The
                    # console registers this mind, with the session
                    # credential minted beside its runtime.yaml.
                    "HIVEMIND_BROKER_URL=http://hive-comms:8424",
                    # Every harness logs in to the inference proxy with the
                    # mind's key, so every call is metered to it - never a
                    # host login, and never another mind's key.
                    f"INFERENCE_PROXY_URL={proxy}",
                    f"ANTHROPIC_BASE_URL={proxy}",
                    "ANTHROPIC_API_KEY=",
                    f"OPENAI_BASE_URL={proxy}/v1",
                    f"DSH_PROXY_BASE_URL={proxy}/v1",
                    "GATEWAY_URL=http://server:8420",
                    "SPECS_DIR=/usr/src/app/specs/data-classes",
                    f"AUTO_REMEMBER_LOG_DIR=/usr/src/app/minds/{name}/data/auto-remember",
                ],
                # Its own folder and nothing else of the host: not the
                # hive_mind checkout, its .env, the shared keyring, or any
                # other mind (DECIDED 2026-11-15, Daniel: a mind not owned by
                # him never reaches his credentials). The folder is its own
                # to write, runtime.yaml included: a mind saves a setting by
                # writing a new file beside the old one and putting it in
                # its place, so the folder is mounted and not the file
                # (Daniel, 2026-11-17: "No such thing as lockdown"). Its
                # compose folder and its session credential are read-only
                # inside it: what it is started with, and what the console
                # registered for it, are not its to rewrite.
                "volumes": [
                    f"{host}:{own}:rw",
                    f"{host}/container:{own}/container:ro",
                    f"{host}/{SESSION_TOKEN_FILENAME}:{own}/{SESSION_TOKEN_FILENAME}:ro",
                    # Its key folder, and no other mind's: outside the checkout,
                    # read-only here, written only by the console.
                    key_folder_mount(name),
                    # The install's generated skills, which a role may name.
                    workflow_skills_volume(),
                ],
                "restart": "unless-stopped",
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "networks": ["hivemind"],
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
                # exec for dsh, which Docker's tmpfs default is not: its
                # loader copies a native addon under /tmp and loads it there.
                "tmpfs": ["/tmp:exec,mode=1777" if is_dsh else "/tmp"],
                # The mind server, as every container mind here runs; it
                # picks the harness from runtime.yaml. The harness modules
                # have no entry point of their own.
                "command": ["/opt/venv/bin/python3", "-m", "minds.mind_server"],
            }
        }
    }
    if connection:
        from provider_data import inference_environment
        service = compose["services"][name]
        inference_names = {"INFERENCE_PROXY_URL", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                           "ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY",
                           "DSH_PROXY_BASE_URL", "DSH_API_KEY"}
        service["environment"] = [item for item in service["environment"]
                                  if item.split("=", 1)[0] not in inference_names]
        # A direct or custom provider: its endpoint and format are named; the
        # key itself is the mind's key file, applied to the provider names at
        # the mind's start (core/mind_key.py), so none is written here.
        service["environment"] += [f"{k}={v}" for k, v in inference_environment(
            connection, "", harness).items() if not k.endswith(("_KEY", "_TOKEN"))
            or k == "ANTHROPIC_API_KEY"]
    if is_dsh:
        service = compose["services"][name]
        service["environment"] += [
            f"DSH_BIN={DSH_BIN}",
            f"DSH_READ_LOG_CLI={DSH_READ_LOG_CLI}",
            # Where a turn runs: somewhere this mind can write. The image's
            # code is root's.
            f"DSH_SPAWN_DIR={own}/data",
            # What its capture hook classifies with, as its own proxy client.
            "HIVE_CLASSIFIER_MODEL=${HIVE_CLASSIFIER_MODEL:-}",
        ]
        # The harness itself, read-only: a mind must not rewrite the tree it
        # is launched from.
        service["volumes"].append(
            f"{os.getenv('HIVE_NEW_MIND_DSH_DIR', '').strip() or '${HOST_DSH_DIR:-${HOST_PROJECT_DIR:-.}/dsh}'}:{DSH_MOUNT}:ro")
        # Its profile and hooks, written by the image's own code as the
        # mind's user before it takes a conversation: the console makes minds
        # and holds none of that code.
        python = "/opt/venv/bin/python3"
        service["command"] = ["sh", "-ec", (
            f"{python} -m minds.harness.dsh_profile --mind {name} "
            f"--out /usr/src/app/minds --dsh-root {DSH_MOUNT} && "
            f"{python} -m minds.harness.dsh_hooks --mind {name} --out /usr/src/app/minds "
            "--container-minds-dir /usr/src/app/minds && "
            f"exec {python} -m minds.mind_server"
        )]
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
        # The folder is the mind's, with its settings file and the folders
        # it works in. Its compose folder stays this console's.
        for writable in (shape["config_dir_name"], "data", "reference"):
            (mind_dir / writable).mkdir()
            os.chown(mind_dir / writable, uid, uid)
        os.chown(runtime_path, uid, uid)
        os.chown(mind_dir, uid, uid)
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


def stack_compose_path() -> Path:
    """Where the stack file listing every mind's fragment is, from here."""
    configured = os.getenv("HIVE_MIND_STACK_COMPOSE", "").strip()
    if configured:
        return Path(configured)
    root = os.getenv("DEV_ROOTS_DIR", "/dev_roots").strip() or "/dev_roots"
    return Path(root) / "hive_mind" / STACK_COMPOSE_FILENAME


def stack_fragment_path(name: str) -> str:
    """A mind's fragment as the stack file names it: relative to that file.

    The stack file of this workstation sits beside `minds/`. An installed
    hive's list is a file inside its minds folder, which says so with
    `HIVE_MIND_STACK_MINDS_PREFIX=.`.
    """
    prefix = os.getenv("HIVE_MIND_STACK_MINDS_PREFIX", "").strip() or "minds"
    return posixpath.normpath(f"{prefix}/{name}/container/compose.yaml")


#: One edit of the stack file at a time. The edits run on worker threads,
#: each reads the file, changes one line and replaces it through the same
#: neighbour: two at once is one Mind's line lost, or a rename of a file the
#: other has already moved.
_STACK_LOCK = threading.Lock()

_EMPTY_INCLUDE_RE = re.compile(r"^include:\s*\[\s*\]\s*$")
_FRAGMENT_SUFFIX = "/container/compose.yaml"


def _stack_entries(lines: list[str]) -> list[int]:
    """The lines of the stack's include list that name a mind's fragment."""
    return [i for i, line in enumerate(lines)
            if line.lstrip().startswith("- path:") and _FRAGMENT_SUFFIX in line]


def _install_stack_file(path: Path, rewritten: str, name: str, verb: str) -> None:
    """Replace the stack file with `rewritten`, or leave it exactly as it is.

    The highest-consequence failure here is a file that no longer parses:
    `docker compose` then refuses every subcommand for every service in it,
    so one mind would take the whole running stack down, and that file is
    generated and gitignored — there is no diff to notice it by and no
    checkout to recover with. So the result is parsed before it is installed,
    and a file that would not load is not installed at all.
    """
    try:
        loaded = yaml.safe_load(rewritten)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{verb} {name} would leave {path} unparseable, so it was "
            f"left alone: {exc}") from exc
    # A list, not merely the key: `include:` with nothing under it loads as
    # null, which compose refuses.
    if not isinstance(loaded, dict) or not isinstance(loaded.get("include"), list):
        raise ValueError(
            f"{verb} {name} would lose the include list of {path}, so it "
            "was left alone")
    # Written whole, through a neighbour and a rename, so a failure part way
    # cannot leave the running stack's definition truncated.
    scratch = path.with_name(path.name + ".console-new")
    try:
        scratch.write_text(rewritten)
        stat = path.stat()
        os.chmod(scratch, stat.st_mode & 0o7777)
        try:
            # This console runs as root and the stack file is in the
            # operator's own checkout. A new inode owned by root is a file
            # he can no longer edit, and neither can the skill that
            # generates it.
            os.chown(scratch, stat.st_uid, stat.st_gid)
        except (OSError, AttributeError):
            pass
        os.replace(scratch, path)
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise


def include_mind_in_stack(name: str) -> bool:
    """Add this mind's compose fragment to the stack's ``include:`` list.

    Returns whether the line was added; an entry already naming this mind is
    left alone, so a retried create is not a second include.

    Edited as text rather than through a YAML round-trip: re-dumping the file
    to add one line would rewrite every comment and quoting choice in the
    eight hundred lines that define the services this host is currently
    running. That means matching the file rather than assuming it —

    * the new entry copies the indentation of the entries already there, so
      a four-space list does not get a two-space line and stop parsing,
      taking every other mind's `docker compose` invocation with it;
    * it goes after the last entry *and any keys belonging to it*
      (``env_file``, ``project_directory``), which are more deeply indented
      continuations rather than entries of their own;
    * already-present is judged on the path, so a quoted entry or one with a
      trailing comment is recognised rather than added a second time;
    * the file's mode and ownership are carried onto the replacement, because
      this console runs as root and a stack file left root-owned is one the
      operator's own editor can no longer write.
    """
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    with _STACK_LOCK:
        return _include_mind_in_stack(name)


def _include_mind_in_stack(name: str) -> bool:
    fragment = stack_fragment_path(name)
    # Through a symlink rather than over it: replacing the link with a
    # regular file leaves the real stack file holding the old content, and a
    # compose run from the other location never sees the new mind.
    path = stack_compose_path().resolve()
    text = path.read_text()
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    entries = _stack_entries(lines)
    if any(_names_fragment(lines[i], fragment) for i in entries):
        return False
    if entries:
        last = entries[-1]
        indent = lines[last][:len(lines[last]) - len(lines[last].lstrip())]
        deeper = len(indent) + 2
        after = last + 1
        while (after < len(lines) and lines[after].strip()
               and not lines[after].lstrip().startswith("- ")
               and len(lines[after]) - len(lines[after].lstrip()) >= deeper):
            after += 1
        lines.insert(after, f"{indent}- path: {fragment}")
    else:
        # A list nobody has added to yet, as the installer leaves it:
        # `include: []`. Anything else with no mind in it is a file this
        # console cannot tell where a new one goes in.
        empty = [i for i, line in enumerate(lines) if _EMPTY_INCLUDE_RE.match(line)]
        if len(empty) != 1:
            raise ValueError(
                f"{path} has no minds in its include list, so this console "
                "cannot tell where a new one goes")
        lines[empty[0]:empty[0] + 1] = ["include:", f"  - path: {fragment}"]
    _install_stack_file(path, newline.join(lines) + newline, name, "adding")
    return True


def _names_fragment(line: str, fragment: str) -> bool:
    """Whether this include entry is the one for `fragment`: the path itself,
    quoted or not, with or without a trailing comment — never a longer path
    that merely ends the same way."""
    value = line.lstrip()[len("- path:"):].split(" #", 1)[0].strip().strip("'\"")
    return posixpath.normpath(value) == fragment


def exclude_mind_from_stack(name: str) -> bool:
    """Take this mind's fragment out of the stack's include list again.

    The undo of `include_mind_in_stack`, for a creation that failed after it:
    an entry naming a fragment that is about to be deleted is a stack file
    `docker compose` refuses whole. Returns whether a line was removed. Only
    a bare entry is removed — one carrying keys of its own (`env_file`,
    `project_directory`) was not written here and is left for a person.
    """
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    with _STACK_LOCK:
        return _exclude_mind_from_stack(name)


def _exclude_mind_from_stack(name: str) -> bool:
    fragment = stack_fragment_path(name)
    path = stack_compose_path().resolve()
    text = path.read_text()
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    entries = _stack_entries(lines)
    mine = [i for i in entries if _names_fragment(lines[i], fragment)]
    if not mine:
        return False
    index = mine[0]
    indent = len(lines[index]) - len(lines[index].lstrip())
    following = lines[index + 1] if index + 1 < len(lines) else ""
    if (following.strip() and not following.lstrip().startswith("- ")
            and len(following) - len(following.lstrip()) >= indent + 2):
        raise ValueError(
            f"the entry for {name} in {path} carries settings of its own, so "
            "it was left alone")
    del lines[index]
    if len(entries) == 1 and not any(
            line.lstrip().startswith("- ") and
            len(line) - len(line.lstrip()) == indent for line in lines):
        # It was the only entry: `include:` with nothing under it is null,
        # which compose refuses. Back to the empty list it started as.
        heads = [i for i, line in enumerate(lines) if line.rstrip() == "include:"]
        if len(heads) == 1:
            lines[heads[0]] = "include: []"
    _install_stack_file(path, newline.join(lines) + newline, name, "removing")
    return True


def remove_internal_mind(name: str, mind_id: str) -> bool:
    """Delete the folder `scaffold_internal_mind` wrote for this mind.

    For a creation that failed after the scaffold, and only for that folder:
    it is deleted when its runtime.yaml carries this mind's id, or when it is
    the bare start of a scaffold that never got as far as writing one. Any
    other folder of that name is somebody else's — a tracked one (`harness`,
    `example`), or a mind that was already there — and is left alone, as is
    a link: this console deletes as root. Returns whether one was deleted.
    """
    if not _MIND_NAME_RE.fullmatch(name):
        raise ValueError("Invalid Mind name")
    mind_dir = HIVE_MIND_MINDS_CONFIG_DIR / name
    if mind_dir.is_symlink() or not mind_dir.is_dir():
        return False
    runtime = mind_dir / "runtime.yaml"
    if runtime.is_file() and not runtime.is_symlink():
        try:
            loaded = yaml.safe_load(runtime.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(
                f"minds/{name} could not be read to tell whose it is, so it "
                f"was left alone: {exc}") from exc
        ours = isinstance(loaded, dict) and str(loaded.get("mind_id") or "") == mind_id
    else:
        # The scaffold makes `container/` first and writes runtime.yaml next.
        names = sorted(entry.name for entry in mind_dir.iterdir())
        container = mind_dir / "container"
        ours = names == [] or (
            names == ["container"] and container.is_dir()
            and not container.is_symlink() and not any(container.iterdir()))
    if not ours:
        # Nothing of this creation's is there to take back: the scaffold
        # refused, because the folder was already somebody's.
        return False
    shutil.rmtree(mind_dir)
    return True


def new_mind_obstacle(name: str, harness: str) -> str | None:
    """Why a Mind cannot be created on this hive as it stands, or None.

    Asked before anything is written, about the things a new Mind's compose
    file is written against and cannot do without. Each of them missing is a
    Mind that every step reports made and that then comes up reading empty
    folders Docker created for it, or holding no harness.
    """
    import credentials_data
    import holder_credentials

    lowered = (name or "").strip().lower()
    reserved = {item.strip().lower() for item in
                os.getenv("HIVE_MIND_RESERVED_NAMES", "").split(",") if item.strip()}
    if lowered in reserved:
        return f"{lowered} is a name this hive's own services use"
    label = holder_credentials.MIND_ENV_LABEL
    path = credentials_data.ENV_FILES.get(label)
    if path is None:
        return (f"this console was given no {label} env file (HIVE_ENV_FILES), "
                "so a new Mind's keys have nowhere to go")
    stack = stack_compose_path()
    if not stack.is_file():
        return (f"the stack file {stack} is not there, so a new Mind cannot "
                "be listed in the stack")
    # Not HOST_DSH_DIR: a dsh mind's fragment falls back to the dsh folder
    # under HOST_PROJECT_DIR when that is unset.
    try:
        value = credentials_data._read_env_value(path, "HOST_PROJECT_DIR")
    except credentials_data.Unreadable:
        return f"{path} could not be read"
    if not value:
        return (f"{path} sets no HOST_PROJECT_DIR, which a new Mind's compose file "
                "is written with. On an installed hive, run the installer once more")
    if not HIVE_MIND_MINDS_CONFIG_DIR.is_dir():
        return (f"{HIVE_MIND_MINDS_CONFIG_DIR} is not mounted, so a new Mind's "
                "folder has nowhere to be written. On an installed hive, run "
                "the installer once more")
    return None


def _mind_uid() -> int:
    """The uid a mind's container runs as; parsed before anything is written."""
    raw = os.getenv("HIVE_MIND_UID", "").strip()
    try:
        return int(raw) if raw else DEFAULT_MIND_UID
    except ValueError as exc:
        raise ValueError(f"HIVE_MIND_UID is not a uid: {raw!r}") from exc


def _own_service(name: str) -> dict | None:
    """The service this console wrote for a mind it made, or None.

    Read from the mind's compose file, which is this console's and which the
    mind sees read-only, never from anything the mind can write. A mind made
    here is known by what it is built as. Never follows a link.
    """
    if not _MIND_NAME_RE.fullmatch(name or ""):
        return None
    path = HIVE_MIND_MINDS_CONFIG_DIR / name / "container" / "compose.yaml"
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        loaded = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    service = (loaded.get("services") or {}).get(name) if isinstance(loaded, dict) else None
    if not isinstance(service, dict):
        return None
    build = service.get("build")
    if not isinstance(build, dict) or build.get("target") != MIND_BUILD_TARGET:
        return None
    return service


def _environment(service: dict) -> dict[str, str]:
    listed = service.get("environment") or []
    if isinstance(listed, dict):
        return {str(k): str(v) for k, v in listed.items()}
    return dict(
        str(item).split("=", 1) if "=" in str(item) else (str(item), "")
        for item in listed)


def name_is_a_host_already(name: str) -> bool:
    """A name another container on this network already answers to, or one
    the hive's own services are named in. A mind's name is its address on
    that network, so a mind named `hive-comms` would answer some of the calls
    meant for the gateway - the admin credential among them."""
    lowered = (name or "").strip().lower()
    return lowered.startswith("hive") or bool(_addresses(lowered))


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit((url or "").strip())
    try:
        port = parts.port
    except ValueError:
        port = -1
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


async def _mind_at(url: str) -> dict | None:
    """The one registered mind that answers at `url`, or None.

    Only when the address is that one mind's and nobody else's: every registry
    row claiming it is the same mind. An address two minds claim, or one no
    row claims, names nobody - a mind can re-register itself anywhere, and
    another mind's key is not its to draw."""
    import route_access

    try:
        rows = await route_access.registered_minds()
    except Exception:  # noqa: BLE001 - a registry that cannot be read names nobody
        return None
    target = _origin(url)
    claimants = [
        row for row in rows or []
        if _origin(str(row.get("gateway_url") or "")) == target]
    whose = {(str(row.get("id") or ""), str(row.get("name") or "").strip().lower())
             for row in claimants}
    return claimants[0] if len(whose) == 1 else None


async def own_credential_for(url: str) -> str | None:
    """The one mind's own key that may be sent to `url`, or None.

    How a change the console has already allowed, by the account of the person
    signed in, reaches a mind it is not that person's to open: the console
    presents that mind's own key, the one it holds for it (Daniel, 2026-11-18:
    "A mind has a key and that is it"), and the mind accepts what it already
    holds. There is no hive-wide administrator key to fall back on (feature
    834, requirement 17). A mind whose key this console does not hold (one on
    another machine, pasted there) gets nothing here: its owner's changes go
    by the owner's key (`user_credential_for`).
    """
    row = await _mind_at(url)
    name = str((row or {}).get("name") or "").strip().lower()
    return key_held_for(name) if name else None


async def user_credential_for(url: str, person: str) -> str | None:
    """The key of `person`, when they own the one mind at `url`, or None.

    A person's key goes only to a mind the registry says is theirs (the mind
    refuses anyone else's by asking the gateway the same question), so an
    administrator changing another person's mind is not sent their own key
    where it would be refused, and no person's key is sent to a mind of
    somebody else's."""
    import holder_credentials

    row = await _mind_at(url)
    owner = str((row or {}).get("owner") or "").strip().lower()
    if not owner or owner != person.strip().lower():
        return None
    return await holder_credentials.user_key(owner)


def key_held_for(name: str) -> str | None:
    """The one key this console holds for the mind called `name`, or None.

    None for a mind whose key is not kept here (another machine), kept in two
    files, or in a file that cannot be read: nothing is sent in its place."""
    import holder_credentials

    service = holder_credentials.SERVICES["comms"]
    try:
        location = holder_credentials.locate(service, name)
        return (holder_credentials._loaded_value(location) if location else None) or None
    except Exception:  # noqa: BLE001 - a key that cannot be found is no key
        return None


def identity_as_made(name: str) -> dict[str, str] | None:
    """Who a mind made here is, as this console wrote it: its id and the
    address it answers at. From its compose file, never its runtime.yaml,
    which is the mind's own to write - the console registers a mind with the
    hive's admin credential, and must not take the mind's word for which
    row that is."""
    service = _own_service(name)
    if service is None:
        return None
    mind_id = _environment(service).get("MIND_ID", "")
    if not mind_id:
        return None
    return {"mind_id": mind_id, "gateway_url": f"http://{name}:8420"}


def _leads_to_such_a_container(host: str, names: set[str]) -> bool:
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
