"""Deliverable C (shipped, hive-tools PR #19): what the tools service takes.

As built: build args UID, GID, DOCKER_GID; CPU-safe (no NVIDIA runtime).
Port 9421, GET /health. Required env HIVE_DATABASE_URL (tools schema),
HT_SECRET_KEY, HT_ADMIN_PASSWORD; INFERENCE_PROXY_URL and INFERENCE_PROXY_KEY
(its model calls go through the proxy, never Ollama — story 10.16.1) and
HIVE_TOOLS_DEFAULT_MODEL naming a chat model the proxy serves. Build context its own folder, modules/hive-tools of hive-mind, plus the
extra context `hive_data` (modules/hive-data). Every mount optional. A token is minted inside the container
with `auth_api.generate_token(name)`, which adds a new token on every call —
so the installer mints once and keeps what it got.
"""

from __future__ import annotations

from installer.shell import Shell, StepFailed

PORT = 9421
SERVICE = "hive-tools"
URL = f"http://{SERVICE}:{PORT}"
#: A route behind the token check: 401 for a token the store does not hold,
#: anything else (404 for this id) for one it does.
DOCKER_SOCKET = "/var/run/docker.sock"
TOKEN_CHECK = "/hitl/installer-token-check"  # secret-guard: allow — a route path
#: TOOLS_ADMIN_BEARER_TOKEN guards the tools' credential routes. It is handed
#: to the tools and to the console, which registers a mind's pasted key with
#: them at creation; no mind and no bot is given it.
SECRETS = ("HT_SECRET_KEY", "HT_ADMIN_PASSWORD", "TOOLS_ADMIN_BEARER_TOKEN")

_MINT = """import asyncio,store,auth_api
async def m():
    await store.open()
    try: print(await auth_api.generate_token({name!r}))
    finally: await store.close()
asyncio.run(m())"""


def build_args(host) -> dict[str, str]:
    return {"UID": str(host.uid), "GID": str(host.gid), "DOCKER_GID": str(host.docker_gid)}


def environment(env_ref, *, dsn_ref: str, proxy_url: str, key_ref: str,
                model_ref: str) -> dict[str, str]:
    return {
        "HIVE_DATABASE_URL": dsn_ref,
        "HT_SECRET_KEY": env_ref("HT_SECRET_KEY"),
        "HT_ADMIN_PASSWORD": env_ref("HT_ADMIN_PASSWORD"),
        "TOOLS_ADMIN_BEARER_TOKEN": env_ref("TOOLS_ADMIN_BEARER_TOKEN"),
        "INFERENCE_PROXY_URL": proxy_url,
        "INFERENCE_PROXY_KEY": key_ref,
        "HIVE_TOOLS_DEFAULT_MODEL": model_ref,
    }


def mint_token(sh: Shell, compose: list[str], name: str = "lucent") -> str:
    """A new tools API token. Each call adds one, so call it only when the
    install holds none."""
    proc = sh.run([*compose, "exec", "-T", SERVICE, "python", "-c", _MINT.format(name=name)],
                  timeout=60, quiet=True)
    token = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    if not token[0]:
        raise StepFailed("the tools service minted no token")
    return token[0].strip()
