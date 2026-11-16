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

import json
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
    # Two refusal shapes, both answered 200. The schema-validating routes say
    # `ok: false`; the older graph routes say `upserted: false` with a
    # `reason` and no `ok` at all, and reading only the first means a refused
    # write is returned here as a success.
    if isinstance(body, dict) and (
            body.get("ok") is False or body.get("upserted") is False):
        raise HTTPException(
            status_code=400,
            detail=str(body.get("detail") or body.get("reason")
                       or body.get("code") or "soul request failed"),
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


async def seed_mind_node(
    mind_name: str, mind_id: str, owner: str, credential: str,
) -> dict:
    """Create a brand-new Mind's node in the graph, as that Mind.

    Written with the mind's own memory-store credential rather than the
    console's soul bearer, because provenance in the graph is the credential
    that wrote the row: seeded with the console's, every mind in the hive
    would have been created by the console acting as itself, and a second
    user's mind would carry a write made under Daniel's house credential.
    The mind has had its own credential since it was minted, moments before
    this call.

    The node carries its name and nothing else. The Mind type's schema
    allows exactly one property, `name`, so anything more is refused as an
    unknown field — and the soul itself is authored in conversation
    afterwards, not guessed at here.

    It is created with its edge in the same call, because the graph refuses
    a node with no edge: "cannot create a node without at least one edge".
    A seed sent without one is not a node nobody linked, it is no node.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{_base_url()}/graph/upsert",
            json={
                "entity_type": "Mind",
                "name": node_name(mind_name),
                "data_class": f"{(mind_name or '').strip().lower()}-identity",
                "mind_id": mind_id,
                "owner": owner,
                "properties": json.dumps({"name": node_name(mind_name)}),
                # The graph refuses a node with no edge at all, and the
                # registry allows a Mind exactly one relation: CREATED_BY a
                # Person. Pointed at the owner rather than at Daniel, which
                # is the whole point of a hive with more than one person in
                # it — a mind made for Sloan hangs off Sloan.
                "relation": "CREATED_BY",
                "target_type": "Person",
                "target_name": node_name(owner),
                # The mind's own credential made this write, so the record
                # says the mind did, which is what every later soul edit
                # is compared against.
                "source": "self",
            },
            headers={"Authorization": f"Bearer {credential}"},
        )
    return _unwrap(response)


async def push_soul(mind_name: str, soul_values: list[str], reason: str) -> dict:
    """Replace the Mind's soul, recorded as a console edit.

    `actor` is hardcoded rather than taken from the request body: a caller
    that could name itself `mind` could launder an operator edit into the
    record as something the Mind decided, and the whole value of the column
    is that it cannot be talked into lying.

    `allow_shrink` is sent because this page *is* the confirmation. The guard
    exists for a caller that built its list from a read it never checked, and
    that cannot be this one: the operator was shown every line and edited the
    text in front of them. Withholding it put a refusal on the page whose only
    remedy was a flag the page had no way to send, which is a dead end rather
    than a safeguard. The guard still stands for every programmatic writer,
    where an unchecked read actually happens.
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
                "allow_shrink": True,
            },
            headers=soul_headers(),
        )
    return _unwrap(response)
