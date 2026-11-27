"""Schema request endpoints — HITL-gated additions to the lucent KG schema.

Mind submits a proposed new node type or edge type via this surface; Daniel
approves or denies via the Telegram inline keyboard (the existing HITL
pattern shared with gmail / calendar / docker). On approval, hive-tools
forwards the approved schema to lucent's bearer-gated admin endpoint
(`/graph/schema/types` or `/graph/schema/edges`), which writes it into
lucent's allow-list tables.

Flow:
  1. mind → POST /schema/request-new-type → hitl_gate fires →
     hitl_requests row created → notify_daniel sends Telegram → 202 pending
  2. mind polls GET /hitl/{request_id} → 202 until decided
  3. Daniel taps "Approve" → callback hits hive-tools /hitl/{id}/respond →
     respond_to_hitl marks approved → triggers internal replay
  4. Replay POSTs same body with `Authorization: Internal <secret>` →
     hitl_gate sees caller=internal, bypasses → handler executes →
     calls lucent admin endpoint → returns success
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from auth_api import require_api_token
from hitl import hitl_gate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/schema", tags=["schema"])


# ---- Request schema ----


class RequestNewTypeBody(BaseModel):
    """Body for POST /schema/request-new-type.

    `kind="node"` means a new entity type (writes to lucent's
    `/graph/schema/types`). `kind="edge"` means a new edge type
    (writes to lucent's `/graph/schema/edges`).
    """

    kind: str  # "node" | "edge"
    name: str  # the type / edge name (e.g. "ProjectMilestone", "TRACKS")
    description: str = ""
    schema_def: dict  # property_schema (for nodes) or attr_schema (for edges)
    # Node-only: lucent distinguishes first-class (addressed by name in
    # conversation, e.g. Person/Mind/Device) from second-class (facet
    # nodes reached via edges, e.g. ContactMethod). Defaults to
    # second-class because most new types are facets.
    node_class: str = "second-class"
    # Edge-only fields:
    source_type: str = ""
    target_type: str = ""
    symmetric: bool = False
    # Provenance:
    context: str = ""  # one-line reason from the requesting mind


# ---- Lucent admin call ----


def _calling_key(request: Request) -> str:
    """The key of the mind that made this call, as it was presented."""
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def _lucent_admin_call(path: str, body: dict[str, Any], key: str) -> dict[str, Any]:
    """POST to a lucent administrator endpoint with the calling mind's own key.

    The tools hold no credential of their own, so this carries the key of the
    mind that asked and nothing else. The memory store accepts these routes
    only for an administrator's own person key, so a mind's key is refused
    there. A replay of an approved call arrives with the tools' internal
    secret and no mind's key at all; then nothing is sent.
    """
    lucent_url = os.environ.get("LUCENT_URL", "http://hive-lucent:8424")
    if not key:
        return {
            "ok": False,
            "code": "no_calling_key",
            "detail": "this call has no calling mind's key to carry to the memory store",
        }

    try:
        resp = httpx.post(
            f"{lucent_url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            return {
                "ok": False,
                "code": "lucent_error",
                "status": resp.status_code,
                "detail": resp.text,
            }
        return resp.json()
    except Exception as e:
        log.exception("lucent admin call failed: %s", path)
        return {"ok": False, "code": "lucent_unreachable", "detail": str(e)}


# ---- Route ----


@router.post("/request-new-type")
async def request_new_type(
    request: Request,
    body: RequestNewTypeBody,
    caller_mind: str = Depends(hitl_gate("schema_new_type")),
) -> Any:
    """Submit a proposed schema addition. HITL-gated.

    First call: HITL gate intercepts → returns 202 with hitl_request_id.
    After Daniel approves via Telegram: replay arrives with caller=internal
    → gate bypasses → this body runs → forwards to lucent admin endpoint.
    """
    if body.kind == "node":
        if body.node_class not in ("first-class", "second-class"):
            return {
                "ok": False,
                "code": "invalid_node_class",
                "detail": f"node_class must be 'first-class' or 'second-class', got {body.node_class!r}",
            }
        return _lucent_admin_call(
            "/graph/schema/types",
            {
                "name": body.name,
                "kind": body.node_class,
                "property_schema": body.schema_def,
            },
            _calling_key(request),
        )

    if body.kind == "edge":
        if not body.source_type or not body.target_type:
            return {
                "ok": False,
                "code": "missing_endpoints",
                "detail": "edges require source_type and target_type",
            }
        return _lucent_admin_call(
            "/graph/schema/edges",
            {
                "name": body.name,
                "source_type": body.source_type,
                "target_type": body.target_type,
                "attr_schema": body.schema_def,
                "symmetric": body.symmetric,
            },
            _calling_key(request),
        )

    return {
        "ok": False,
        "code": "invalid_kind",
        "detail": f"kind must be 'node' or 'edge', got {body.kind!r}",
    }
