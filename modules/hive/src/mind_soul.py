"""Reading and writing a Mind's soul, from the console.

A soul is not like `runtime.yaml`. `runtime.yaml` lives on the Mind's own
disk, so the console asks the Mind for it. A soul lives in lucent's graph,
so the console asks lucent — the Mind is not consulted and does not need to
be running.

That matters most in the case this page exists for: a Mind whose soul says
something wrong is a Mind you may not want to leave running while you fix
it. Routing the edit through the Mind would make the broken thing a
prerequisite for repairing it.

The credential is `LUCENT_SOUL_BEARER_TOKEN`, which lucent accepts on
`/graph/soul` and nowhere else, and which is neither the service bearer nor
the admin bearer. The console holds it because the operator editing a Mind's
soul directly is the one case that should bypass the Mind entirely. Every
write from here is recorded with `actor="console"` so it stays
distinguishable, forever, from a line the Mind wrote about itself.
"""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException

_TIMEOUT = 8.0


def _base_url() -> str:
    return (os.getenv("LUCENT_API_URL") or "http://hive-lucent:8424").rstrip("/")


def soul_headers() -> dict[str, str]:
    token = os.getenv("LUCENT_SOUL_BEARER_TOKEN", "")
    if not token:
        raise HTTPException(
            status_code=503,
            detail=(
                "LUCENT_SOUL_BEARER_TOKEN is not configured for this console. "
                "Souls are unwritable from here until it is set."
            ),
        )
    return {"Authorization": f"Bearer {token}"}


def node_name(mind_name: str) -> str:
    """The Mind node's name in the graph.

    Minds are registered under their display name (`Skippy`), while the
    console addresses them by their lowercase id (`skippy`). lucent matches
    case-insensitively, but sending the registered form keeps the recorded
    `mind_name` on every provenance row consistent with the node.
    """
    name = (mind_name or "").strip()
    return name[:1].upper() + name[1:] if name else name


def _unwrap(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"lucent refused the soul request: {response.text[:400]}",
        )
    body = response.json()
    if isinstance(body, dict) and body.get("ok") is False:
        raise HTTPException(
            status_code=400,
            detail=str(body.get("detail") or body.get("code") or "soul request failed"),
        )
    return body if isinstance(body, dict) else {}


async def fetch_soul(mind_name: str) -> dict:
    """The Mind's soul as it stands in the graph, with its change count."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{_base_url()}/graph/soul",
            params={"name": node_name(mind_name)},
            headers=soul_headers(),
        )
    return _unwrap(response)


async def fetch_history(mind_name: str, limit: int = 25) -> list[dict]:
    """Recorded changes, newest first — who changed what, and why."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{_base_url()}/graph/soul/history",
            params={"name": node_name(mind_name), "limit": limit},
            headers=soul_headers(),
        )
    body = _unwrap(response)
    changes = body.get("changes")
    return changes if isinstance(changes, list) else []


async def push_soul(mind_name: str, soul_values: list[str], reason: str) -> dict:
    """Replace the Mind's soul, recorded as a console edit.

    `actor` is hardcoded rather than taken from the request body: a caller
    that could name itself `mind` could launder an operator edit into the
    record as something the Mind decided, and the whole value of the column
    is that it cannot be talked into lying.
    """
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(
            status_code=400,
            detail="Say why this changed — the reason is stored with the edit.",
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{_base_url()}/graph/soul",
            json={
                "name": node_name(mind_name),
                "soul_values": [str(v) for v in soul_values],
                "actor": "console",
                "reason": reason,
            },
            headers=soul_headers(),
        )
    return _unwrap(response)
