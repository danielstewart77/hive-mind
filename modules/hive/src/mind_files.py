"""Reading and editing the files a Mind's harness loads, over its own API.

`mind_skills` moves whole skill directories between a Mind's repo checkout
and the copy its harness runs. This is the other half of the same page and
a narrower thing: the files themselves, in the two directories the harness
actually reads — `skills` and `hooks` — listed, opened and saved in place.

Same shape as `mind_skills` and `mind_runtime`, and for the same reason: a
container in this stack, a bare-metal Mind on this host, and a Mind on
someone else's laptop are one code path, because the Mind reports its own
filesystem rather than offering a bind mount nobody on another machine has.

Every route is admin-guarded on the Mind, reads included. A hooks listing
names every script the Mind runs per turn, and those scripts carry gateway
URLs and bearer usage; the port answers across the LAN.
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException

from mind_runtime import admin_headers

_TIMEOUT = 20.0

TREES = ("skills", "hooks")


def _endpoint(gateway_url: str, tree: str, suffix: str = "") -> str:
    if tree not in TREES:
        raise HTTPException(status_code=400, detail=f"Unknown tree: {tree}")
    url = (gateway_url or "").strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=400, detail="This Mind has no gateway URL in the registry"
        )
    return f"{url}/files/{tree}{suffix}"


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return response.text


def _unwrap(response: httpx.Response, *, collection: bool = False) -> dict:
    # Starlette answers 404 for a path matching no route at all, so a 404 on
    # the listing means the Mind predates this API. Passed through as a
    # plain 404 it would render as "this folder is empty", which is the one
    # thing the console must never say about a Mind it cannot actually read.
    if response.status_code == 404 and collection:
        raise HTTPException(
            status_code=501,
            detail="This Mind is running a version without the file editor API",
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_detail(response))
    body = response.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Mind returned an unexpected body")
    return body


async def _call(
    method: str,
    url: str,
    *,
    collection: bool = False,
    json: dict | None = None,
    params: dict | None = None,
) -> dict:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(
                method, url, headers=admin_headers(), json=json, params=params
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Mind is unreachable: {exc}"
        ) from exc
    return _unwrap(response, collection=collection)


async def fetch_tree(gateway_url: str, tree: str) -> dict:
    """Every file this Mind's harness reads under one of the two roots."""
    return await _call("GET", _endpoint(gateway_url, tree), collection=True)


async def fetch_file(gateway_url: str, tree: str, path: str) -> dict:
    """One file's text, or the Mind's reason it cannot be shown as text."""
    return await _call(
        "GET", _endpoint(gateway_url, tree, "/content"), params={"path": path}
    )


async def save_file(
    gateway_url: str, tree: str, path: str, text: str, revision: str
) -> dict:
    """Replace one existing file's contents on the Mind.

    `revision` is what the read handed back, and it is mandatory on both
    hops. The Mind refuses with 409 when it no longer describes the file —
    two tabs, or the sync table's "Apply repo copy" landing between opening
    and saving.
    """
    return await _call(
        "PUT",
        _endpoint(gateway_url, tree, "/content"),
        json={"path": path, "text": text, "revision": revision},
    )
