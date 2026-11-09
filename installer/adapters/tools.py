"""Deliverable C (shipped, hive-tools PR #19): what the tools service takes.

As built: build args UID, GID, DOCKER_GID; CPU-safe (no NVIDIA runtime).
Port 9421, GET /health. Required env HIVE_DATABASE_URL (tools schema),
HT_SECRET_KEY, HT_ADMIN_PASSWORD; OLLAMA_BASE_URL and OLLAMA_DEFAULT_MODEL
naming a model that Ollama has. Build context the repo plus the extra context
`hive_data`. Every mount optional. A token is minted inside the container
with `auth_api.generate_token(name)`, which adds a new token on every call —
so the installer mints once and keeps what it got.
"""

from __future__ import annotations

from installer.shell import Shell, StepFailed

PORT = 9421
SERVICE = "hive-tools"
URL = f"http://{SERVICE}:{PORT}"
SECRETS = ("HT_SECRET_KEY", "HT_ADMIN_PASSWORD")

_MINT = """import asyncio,store,auth_api
async def m():
    await store.open()
    try: print(await auth_api.generate_token({name!r}))
    finally: await store.close()
asyncio.run(m())"""


def build_args(host) -> dict[str, str]:
    return {"UID": str(host.uid), "GID": str(host.gid), "DOCKER_GID": str(host.docker_gid)}


def environment(env_ref, *, dsn_ref: str, ollama_url: str, model: str) -> dict[str, str]:
    return {
        "HIVE_DATABASE_URL": dsn_ref,
        "HT_SECRET_KEY": env_ref("HT_SECRET_KEY"),
        "HT_ADMIN_PASSWORD": env_ref("HT_ADMIN_PASSWORD"),
        "OLLAMA_BASE_URL": ollama_url,
        "OLLAMA_DEFAULT_MODEL": model,
    }


def mint_token(sh: Shell, compose: list[str], name: str = "lucent") -> str:
    """A new tools API token. Each call adds one, so call it only when the
    install holds none."""
    proc = sh.run([*compose, "exec", "-T", SERVICE, "python", "-c", _MINT.format(name=name)],
                  timeout=60)
    token = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    if not token[0]:
        raise StepFailed("the tools service minted no token")
    return token[0].strip()
