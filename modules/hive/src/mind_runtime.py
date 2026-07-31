"""Reading and writing a Mind's runtime.yaml over its own HTTP API.

A Mind's `runtime.yaml` is the durable truth about what it is, and the Mind
re-registers from that file on every start — so the console has to write the
file, not just the broker row, or the next restart would undo the edit.

It writes it the same way for every Mind: `GET /runtime` and
`PATCH /runtime` against the gateway URL already in the broker registry.
That is one code path for a container in this stack, a bare-metal Mind on
this host, and a Mind on another machine — the last of which no bind mount
can reach. A Mind that is down cannot be configured, which is the honest
answer: the file is authoritative, and a broker row edited while its Mind
was unreachable would be overwritten the moment it came back.
"""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException

_TIMEOUT = 8.0


def admin_headers() -> dict[str, str]:
    """The bearer a Mind accepts on its config-write route.

    The same admin token the gateway uses; Minds accept it (or their own
    `MIND_ADMIN_TOKEN`, when an install sets one).
    """
    token = os.getenv("COMMS_ADMIN_BEARER_TOKEN", "")
    if not token:
        raise HTTPException(
            status_code=503,
            detail="COMMS_ADMIN_BEARER_TOKEN is not configured for this console",
        )
    return {"Authorization": f"Bearer {token}"}


def _endpoint(gateway_url: str) -> str:
    url = (gateway_url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=400, detail="This Mind has no gateway URL in the registry"
        )
    return f"{url}/runtime"


def _unwrap(response: httpx.Response) -> dict:
    if response.status_code == 404:
        raise HTTPException(
            status_code=501,
            detail="This Mind is running a version without the /runtime API",
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    body = response.json()
    configuration = body.get("configuration") if isinstance(body, dict) else None
    if not isinstance(configuration, dict):
        raise HTTPException(status_code=502, detail="Mind returned no configuration")
    return configuration


async def fetch_configuration(gateway_url: str) -> dict:
    """This Mind's runtime configuration, straight from its own file."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(_endpoint(gateway_url))
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap(response)


async def push_default_model(gateway_url: str, default_model: str) -> dict:
    """Write `default_model` into this Mind's runtime.yaml."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.patch(
                _endpoint(gateway_url),
                json={"default_model": default_model},
                headers=admin_headers(),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap(response)
