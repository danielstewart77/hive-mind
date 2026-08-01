"""Reading and syncing a Mind's skills over its own HTTP API.

A skill lives in two places on a Mind's disk: the tracked copy in its repo
checkout, and the copy its harness actually loads. Neither is reachable from
here — a Mind on another machine has no bind mount to offer — so the Mind
reports both sides and the console renders the pair and issues the actions.

Same shape as `mind_runtime`, and for the same reason: a container in this
stack, a bare-metal Mind on this host, and a Mind on someone else's laptop
are one code path. A Mind that is down cannot be synced, which is the honest
answer rather than an empty list that reads like "no skills".
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException

from mind_runtime import admin_headers

_TIMEOUT = 12.0


def _endpoint(gateway_url: str, suffix: str = "") -> str:
    url = (gateway_url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=400, detail="This Mind has no gateway URL in the registry"
        )
    return f"{url}/skills{suffix}"


def _unwrap(response: httpx.Response, *, collection: bool = False) -> dict:
    # Starlette answers 404 for a path that matches no route at all, so a
    # 404 on the collection route means the Mind predates this API — there
    # is no "skill not found" reading of it. On the per-skill routes 404 is
    # exactly that, and is passed through.
    if response.status_code == 404 and collection:
        raise HTTPException(
            status_code=501,
            detail="This Mind is running a version without the /skills API",
        )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=_detail(response))
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_detail(response))
    body = response.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Mind returned an unexpected body")
    return body


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return response.text


async def _call(method: str, url: str, *, collection: bool = False) -> dict:
    # Every route is admin-guarded on the Mind, reads included: they return
    # the full text of every skill it runs.
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(method, url, headers=admin_headers())
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap(response, collection=collection)


async def fetch_skills(gateway_url: str) -> dict:
    """Every skill this Mind knows, repo side and installed side."""
    return await _call("GET", _endpoint(gateway_url), collection=True)


async def fetch_diff(gateway_url: str, name: str) -> dict:
    """What differs between the repo's copy and this Mind's."""
    return await _call("GET", _endpoint(gateway_url, f"/{name}/diff"))


async def install(gateway_url: str, name: str) -> dict:
    """Put the repo's version on this Mind — apply and revert alike."""
    return await _call("POST", _endpoint(gateway_url, f"/{name}/install"))


async def write_back(gateway_url: str, name: str) -> dict:
    """Put this Mind's version into its repo checkout, uncommitted."""
    return await _call("POST", _endpoint(gateway_url, f"/{name}/write-back"))


async def remove(gateway_url: str, name: str) -> dict:
    """Delete this Mind's copy. Its repo keeps whatever it had."""
    return await _call("DELETE", _endpoint(gateway_url, f"/{name}"))
