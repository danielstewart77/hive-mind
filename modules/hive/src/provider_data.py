"""Named inference endpoints. Credentials always belong to Minds."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

import httpx
import yaml

import configuration_data
import store

_LOCK = asyncio.Lock()
KINDS = {"proxy", "openai", "anthropic", "ollama", "custom"}
FORMATS = {"openai", "anthropic", "ollama"}


def normalize_connection(body: dict, connection_id: str | None = None) -> dict:
    name = str(body.get("name") or "").strip()
    kind = str(body.get("kind") or "custom")
    api = str(body.get("api_format") or (kind if kind in FORMATS else "openai"))
    if not name or len(name) > 100:
        raise ValueError("Give the provider a name of up to 100 characters")
    if kind not in KINDS or api not in FORMATS:
        raise ValueError("Unknown provider type or API format")
    if kind in FORMATS and api != kind:
        raise ValueError("The API format must match the direct provider type")
    if kind == "proxy" and api != "openai":
        raise ValueError("Use the Proxy type for a Hive-compatible proxy; use Custom for other API formats")
    base = configuration_data.normalize_endpoint(str(body.get("base_url") or ""))
    return {"id": connection_id or str(uuid.uuid4()), "name": name, "kind": kind,
            "api_format": api, "base_url": base}


def api_base(connection: dict) -> str:
    base = connection["base_url"].rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def root_url(connection: dict) -> str:
    base = connection["base_url"].rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def inference_environment(connection: dict, key_ref: str, harness: str) -> dict:
    """Runtime-consumed endpoints and references, never literal credentials."""
    api = connection["api_format"]
    proxy = connection["kind"] == "proxy"
    env = {"HIVE_PROVIDER_KIND": connection["kind"], "HIVE_PROVIDER_API": api,
           "HIVE_PROVIDER_URL": connection["base_url"], "HIVE_PROVIDER_TOKEN": key_ref}
    if proxy:
        env.update(INFERENCE_PROXY_URL=root_url(connection), MIND_PROXY_KEY=key_ref)
    if api == "anthropic" or proxy:
        env.update(ANTHROPIC_BASE_URL=root_url(connection), ANTHROPIC_AUTH_TOKEN=key_ref,
                   ANTHROPIC_API_KEY="" if proxy else key_ref)
    if api in {"openai", "ollama"} or proxy:
        env.update(OPENAI_BASE_URL=api_base(connection), OPENAI_API_KEY=key_ref)
    if harness == "codex_cli" and (api != "openai" or connection["kind"] in {"proxy", "custom"}):
        env["OLLAMA_BASE_URL"] = api_base(connection)
    if harness == "dsh":
        env.update(DSH_PROXY_BASE_URL=api_base(connection) if api != "anthropic" else root_url(connection), DSH_API_KEY=key_ref)
    return env


def _legacy_endpoint(folder: Path, runtime: dict, fallback: str) -> str:
    compose = folder / "container/compose.yaml"
    if compose.is_file():
        document = yaml.safe_load(compose.read_text()) or {}
        for service in document.get("services", {}).values():
            env = service.get("environment") or {}
            if isinstance(env, list):
                env = dict(item.split("=", 1) for item in env if isinstance(item, str) and "=" in item)
            for field in ("INFERENCE_PROXY_URL", "DSH_PROXY_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
                value = env.get(field)
                if value and "$" not in value:
                    return configuration_data.normalize_endpoint(value).removesuffix("/v1")
    env = runtime.get("env") or {}
    for field in ("INFERENCE_PROXY_URL", "DSH_PROXY_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        if env.get(field):
            return configuration_data.normalize_endpoint(env[field]).removesuffix("/v1")
    return fallback


async def _registry() -> dict:
    state = await store.get_provider_connections()
    if state:
        return state
    endpoint = await configuration_data.get_capability_endpoint("inference-proxy") or configuration_data.mind_proxy_base_url()
    default = normalize_connection({"name": "Hive Proxy", "kind": "proxy", "base_url": endpoint}, "hive-proxy")
    state = {"connections": [default], "minds": {}}
    root = configuration_data.HIVE_MIND_MINDS_CONFIG_DIR
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            source = folder / "runtime.yaml"
            if not source.is_file():
                continue
            runtime = yaml.safe_load(source.read_text()) or {}
            if runtime.get("remote") is True or not runtime.get("name"):
                continue
            if not (folder / "container/compose.yaml").is_file():
                continue
            snapshot = runtime.get("provider_connection")
            if isinstance(snapshot, dict):
                connection = normalize_connection(snapshot, str(snapshot.get("id") or uuid.uuid4()))
                if not any(p["id"] == connection["id"] for p in state["connections"]):
                    state["connections"].append(connection)
                state["minds"][folder.name] = connection["id"]
                continue
            url = _legacy_endpoint(folder, runtime, default["base_url"])
            connection = next((p for p in state["connections"] if root_url(p) == url), None)
            if connection is None:
                connection = normalize_connection({"name": f"Imported {folder.name}", "kind": "proxy", "base_url": url})
                state["connections"].append(connection)
            state["minds"][folder.name] = connection["id"]
    await store.put_provider_connections(state)
    return state


async def list_connections() -> list[dict]:
    async with _LOCK:
        return (await _registry())["connections"]


async def get_connection(connection_id: str) -> dict:
    for connection in await list_connections():
        if connection["id"] == connection_id:
            return connection
    raise ValueError("That provider no longer exists; select another provider")


async def connection_for_mind(name: str) -> dict | None:
    async with _LOCK:
        state = await _registry()
        selected = state["minds"].get(name)
        return next((p for p in state["connections"] if p["id"] == selected), None)


async def bind_mind(name: str, connection_id: str) -> None:
    async with _LOCK:
        state = await _registry()
        if not any(p["id"] == connection_id for p in state["connections"]):
            raise ValueError("Provider no longer exists")
        state["minds"][name] = connection_id
        await store.put_provider_connections(state)


async def unbind_mind(name: str) -> None:
    async with _LOCK:
        state = await _registry()
        state["minds"].pop(name, None)
        await store.put_provider_connections(state)


async def save_connection(body: dict, connection_id: str | None = None) -> dict:
    saved = normalize_connection(body, connection_id)
    async with _LOCK:
        state = await _registry()
        if connection_id and not any(p["id"] == connection_id for p in state["connections"]):
            raise ValueError("Provider does not exist")
        previous = next((p for p in state["connections"] if p["id"] == connection_id), None)
        if previous and connection_id in state["minds"].values() and any(previous[k] != saved[k] for k in ("kind", "api_format")):
            raise ValueError("Create a separate provider to change the API format of a connection used by Minds")
        state["connections"] = [saved if p["id"] == saved["id"] else p for p in state["connections"]]
        if not connection_id:
            state["connections"].append(saved)
        await store.put_provider_connections(state)
    return saved


async def delete_connection(connection_id: str) -> None:
    async with _LOCK:
        state = await _registry()
        if connection_id in state["minds"].values():
            raise ValueError("This provider is assigned to Minds and cannot be deleted")
        state["connections"] = [p for p in state["connections"] if p["id"] != connection_id]
        await store.put_provider_connections(state)


def normalize_token(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{0,4096}", value.strip()):
        raise ValueError("Paste only the token, without quotes or embedded whitespace")
    return value.strip()


async def fetch_models(connection: dict, token: str, harness: str) -> list[dict]:
    api = connection["api_format"]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if api == "anthropic":
        headers = {"anthropic-version": "2023-06-01", **({"x-api-key": token} if token else {})}
    proxy = connection["kind"] == "proxy"
    params = {"harness": harness.removesuffix("_cli")} if proxy else {}
    url = root_url(connection) + "/api/tags" if api == "ollama" else api_base(connection) + "/models"
    rows, seen = [], set()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            for _ in range(20):
                response = await client.get(url, headers=headers, params=params)
                if response.status_code in (401, 403):
                    raise ValueError("This provider requires a working Mind token to list models. The pasted token has not been discarded")
                if response.status_code != 200:
                    raise ValueError(f"The provider could not list models (HTTP {response.status_code})")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("The provider returned an invalid model list")
                items = payload.get("models" if api == "ollama" else "data", [])
                if not isinstance(items, list):
                    raise ValueError("The provider returned an invalid model list")
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    model = item.get("name") if api == "ollama" else item.get("id")
                    if not isinstance(model, str) or not configuration_data._MODEL_NAME_RE.fullmatch(model) or model in seen:
                        continue
                    seen.add(model)
                    rows.append({"id": model, "label": str(item.get("display_name") or item.get("label") or model), "context_window": item.get("context_window"), "provider": item.get("provider") or connection["name"]})
                if api != "anthropic" or not payload.get("has_more"):
                    break
                last = payload.get("last_id")
                if not last or params.get("after_id") == last:
                    raise ValueError("The provider returned an invalid model-list page")
                params["after_id"] = last
            else:
                raise ValueError("The provider model list exceeded the page limit")
    except (httpx.HTTPError, json.JSONDecodeError):
        raise ValueError("Could not read models from this provider. Check its URL and your Mind token") from None
    return rows


def harness_provider(connection: dict, harness: str) -> str:
    if harness not in {"dsh", "claude_cli", "codex_cli"}:
        raise ValueError("Unknown harness")
    api = connection["api_format"]
    if connection["kind"] == "proxy":
        return "anthropic" if harness == "claude_cli" else "openai" if harness == "codex_cli" else "openai"
    if harness == "claude_cli" and api != "anthropic":
        raise ValueError("Claude CLI requires an Anthropic-compatible provider")
    if harness == "codex_cli" and api == "anthropic":
        raise ValueError("Codex CLI requires an OpenAI-compatible provider")
    return "ollama" if harness == "codex_cli" and connection["kind"] in {"custom", "ollama"} else api


async def apply_pending_url(name: str) -> bool:
    """Apply an intentional provider URL edit when its Mind is reloaded.

    The initial import does not write runtime files or credentials. This
    operation changes only endpoint fields, retaining the Mind's own keys.
    """
    connection = await connection_for_mind(name)
    if not connection:
        return False
    folder = configuration_data.HIVE_MIND_MINDS_CONFIG_DIR / name
    source = folder / "runtime.yaml"
    compose_path = folder / "container/compose.yaml"
    if not source.is_file() or not compose_path.is_file():
        raise ValueError("This Mind's local connection cannot be reconfigured on this host")
    runtime = yaml.safe_load(source.read_text()) or {}
    old = runtime.get("provider_connection")
    previous = old["base_url"] if old else _legacy_endpoint(folder, runtime, connection["base_url"])
    if previous.rstrip("/").removesuffix("/v1") == root_url(connection):
        return False
    document = yaml.safe_load(compose_path.read_text()) or {}
    services = document.get("services") or {}
    service = services.get(name) or next(iter(services.values()), None)
    if not service:
        raise ValueError("This Mind has no Compose service to reconfigure")
    original = service.get("environment") or {}
    env = dict(item.split("=", 1) for item in original if isinstance(item, str) and "=" in item) if isinstance(original, list) else dict(original)
    updates = inference_environment(connection, "", str(runtime.get("harness") or "dsh"))
    endpoint_names = {"INFERENCE_PROXY_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OLLAMA_BASE_URL", "DSH_PROXY_BASE_URL", "HIVE_PROVIDER_URL", "HIVE_PROVIDER_KIND", "HIVE_PROVIDER_API"}
    for key, value in updates.items():
        if key in endpoint_names:
            env[key] = value
    service["environment"] = [f"{key}={value}" for key, value in env.items()] if isinstance(original, list) else env
    runtime["provider_connection"] = dict(connection)
    runtime_env = runtime.setdefault("env", {})
    for key, value in updates.items():
        if key in endpoint_names:
            runtime_env[key] = value
    # These existing files retain their ownership and mode; the source never
    # contains a newly pasted key and is not sent back to the browser.
    source.write_text(yaml.safe_dump(runtime, sort_keys=False))
    compose_path.write_text(yaml.safe_dump(document, sort_keys=False))
    return True
