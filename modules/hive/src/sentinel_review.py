"""Reading and deciding Sentinel's triage policy over its own HTTP API.

The event store lives on the Sentinel sensor box, not here. Same shape as
`mind_skills` and `mind_runtime`, and for the same reason: the machine that
holds the state answers for it, rather than this console reaching across the
LAN into somebody else's filesystem.

What it surfaces is the queue nobody was draining. Hex classifies every
anomalous line, writes his own record-only rules and approves them himself —
which is what keeps the pipeline unattended, and also what lets a class be
silenced forever without Daniel ever seeing it. Every rule and class Hex
writes is therefore stamped unreviewed, and this is where that stamp gets
cleared or overturned.
"""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException

_TIMEOUT = 12.0
# The sweep holds one write transaction across the whole backlog and the
# sensor's own busy_timeout is 30 seconds. A client that gives up first
# reports "unreachable" over work the sensor goes on to commit, so the write
# routes wait longer than the sensor is willing to block.
_WRITE_TIMEOUT = 45.0


def base_url() -> str:
    return (os.getenv("SENTINEL_REVIEW_URL") or "").strip().rstrip("/")


def _headers() -> dict[str, str]:
    token = (os.getenv("SENTINEL_REVIEW_TOKEN") or "").strip()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="SENTINEL_REVIEW_TOKEN is not configured on the console",
        )
    return {"Authorization": f"Bearer {token}"}


def _endpoint(suffix: str) -> str:
    url = base_url()
    if not url:
        raise HTTPException(
            status_code=503,
            detail="SENTINEL_REVIEW_URL is not configured on the console",
        )
    return f"{url}{suffix}"


def _unwrap(response: httpx.Response) -> dict:
    # A 404 here means the sensor is running a build without this API, not
    # that a rule is missing — the routes are fixed, and the ids travel in
    # the body. Reporting it as "not found" would send the operator hunting
    # for a row that exists.
    if response.status_code == 404:
        raise HTTPException(
            status_code=501,
            detail="The Sentinel sensor is running a version without the review API",
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_detail(response))
    body = response.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Sentinel returned an unexpected body")
    return body


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return response.text


async def _get(suffix: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            response = await client.get(_endpoint(suffix), headers=_headers())
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Sentinel sensor unreachable: {exc}"
            ) from exc
    return _unwrap(response)


async def _post(suffix: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
        try:
            response = await client.post(
                _endpoint(suffix), headers=_headers(), json=payload
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Sentinel sensor unreachable: {exc}"
            ) from exc
    return _unwrap(response)


async def fetch_review_queue() -> dict:
    return await _get("/review")


async def fetch_decision_queue() -> dict:
    return await _get("/queue")


async def mark_reviewed(kind: str, row_id: int, note: str | None = None) -> dict:
    return await _post("/review/mark", {"kind": kind, "id": row_id, "note": note})


async def overturn_rule(
    rule_id: int,
    action_kind: str | None = None,
    retire: bool = False,
    note: str | None = None,
) -> dict:
    return await _post(
        "/review/overturn",
        {"rule_id": rule_id, "action_kind": action_kind, "retire": retire, "note": note},
    )


async def apply_queue(class_slug: str | None = None) -> dict:
    return await _post("/queue/apply", {"class_slug": class_slug})


async def fetch_buckets() -> dict:
    return await _get("/buckets")


async def save_bucket(slug: str, revision: int, fields: dict) -> dict:
    return await _post("/buckets/save", {"slug": slug, "revision": revision, **fields})


# The states the catalogue page has to tell apart. Folding them loses the one
# thing the operator needs: which machine to go and look at. "Sensor too old"
# in particular is the EXPECTED first state after deploying the console half,
# and its remedy is a pull on the sensor -- nothing like the remedy for any of
# the others.
NOT_CONFIGURED = "not_configured"
UNREACHABLE = "unreachable"
SENSOR_TOO_OLD = "sensor_too_old"
CREDENTIAL_REFUSED = "credential_refused"
SENSOR_BUSY = "sensor_busy"
EMPTY = "reachable_with_nothing"
READY = "ready"

_STATE_BY_STATUS = {
    501: SENSOR_TOO_OLD,
    401: CREDENTIAL_REFUSED,
    403: CREDENTIAL_REFUSED,
    503: SENSOR_BUSY,
    502: UNREACHABLE,
}


async def catalogue_state() -> dict:
    """The catalogue, or which of the five ways reading it did not work.

    Returned rather than raised, because this feeds a page render: a template
    that got an exception would show the generic error panel, and a template
    that got `{"buckets": []}` would show an empty catalogue -- which reads as
    "there are no buckets" rather than "nobody could ask".
    """
    if not base_url():
        return {
            "state": NOT_CONFIGURED,
            "detail": "SENTINEL_REVIEW_URL is not set on this console",
            "buckets": [],
        }
    try:
        payload = await fetch_buckets()
    except HTTPException as exc:
        state = _STATE_BY_STATUS.get(exc.status_code, UNREACHABLE)
        if exc.status_code == 503 and "not configured" in str(exc.detail):
            state = NOT_CONFIGURED
        return {"state": state, "detail": str(exc.detail), "buckets": []}
    if not payload.get("buckets"):
        return {
            "state": EMPTY,
            "detail": "the sensor answered, and holds no bucket catalogue yet",
            "buckets": [],
            "build": payload.get("build"),
        }
    return {"state": READY, "detail": "", **payload}
