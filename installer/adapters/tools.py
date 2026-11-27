"""Deliverable C (shipped, hive-tools PR #19): what the tools service takes.

As built: build args UID, GID, DOCKER_GID; CPU-safe (no NVIDIA runtime).
Port 9421, GET /health. Required env HIVE_DATABASE_URL (tools schema),
HT_SECRET_KEY, HT_ADMIN_PASSWORD; INFERENCE_PROXY_URL and INFERENCE_PROXY_KEY
(its model calls go through the proxy, never Ollama — story 10.16.1) and
HIVE_TOOLS_DEFAULT_MODEL naming a chat model the proxy serves. Build context its own folder, modules/hive-tools of hive-mind, plus the
extra context `hive_data` (modules/hive-data). Every mount optional. The tools hold no
credential of their own and the installer mints none: a mind calls them with
its own key and a person (through the hive site) with theirs, both recognised
from the hive database's one record of keys.
"""

from __future__ import annotations

PORT = 9421
SERVICE = "hive-tools"
URL = f"http://{SERVICE}:{PORT}"
DOCKER_SOCKET = "/var/run/docker.sock"
SECRETS = ("HT_SECRET_KEY", "HT_ADMIN_PASSWORD")
HITL_WAIT_SECONDS = "120"

def build_args(host) -> dict[str, str]:
    return {"UID": str(host.uid), "GID": str(host.gid), "DOCKER_GID": str(host.docker_gid)}


def environment(env_ref, *, dsn_ref: str, proxy_url: str, key_ref: str,
                model_ref: str) -> dict[str, str]:
    return {
        "HIVE_DATABASE_URL": dsn_ref,
        "HT_SECRET_KEY": env_ref("HT_SECRET_KEY"),
        "HT_ADMIN_PASSWORD": env_ref("HT_ADMIN_PASSWORD"),
        "INFERENCE_PROXY_URL": proxy_url,
        "INFERENCE_PROXY_KEY": key_ref,
        "HIVE_TOOLS_DEFAULT_MODEL": model_ref,
        # How long a gated call is held open for a person's yes before the
        # mind is told no. Under the console's 310 second wait on these calls.
        "HITL_WAIT_SECONDS": HITL_WAIT_SECONDS,
    }
