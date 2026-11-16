"""SMS send via self-hosted sms-gateway server (capcom6/sms-gateway).

Outbound counterpart to hive-comms' `/sms/inbound` webhook receiver. Posts to
the local sms-gateway container's 3rd-party API; the server dispatches the
job to the phone app via its outbound channel. HITL-gated by default so a
Telegram approval fires before anything leaves the home.

Also exposes a thin proxy over hive-comms' `/sms/inbound/enabled` toggle
so the inbound listener can be flipped on/off from the Hive Tools surface.

Env vars:
- ``SMS_GATEWAY_URL`` — base URL for the local sms-gateway container, e.g.
  ``http://sms-gateway:3000`` (reached over the ``sms-private`` Docker network).
- ``SMS_GATEWAY_USER`` / ``SMS_GATEWAY_PASS`` — basic-auth credentials issued
  by the server when the phone registers as a device.
- ``COMMS_URL`` / ``COMMS_BEARER_TOKEN`` — used by the inbound-toggle proxy.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth_api import require_api_token
from hitl import hitl_gate
from schemas import SmsInboundEnabledRequest, SmsSendRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sms", tags=["sms"])

_HTTP_TIMEOUT_SECONDS = 15.0


def _gateway_config() -> tuple[str, tuple[str, str]] | None:
    url = os.environ.get("SMS_GATEWAY_URL", "").strip().rstrip("/")
    user = os.environ.get("SMS_GATEWAY_USER", "").strip()
    pw = os.environ.get("SMS_GATEWAY_PASS", "").strip()
    if not (url and user and pw):
        return None
    return url, (user, pw)


@router.post("/send")
async def send_sms(
    body: SmsSendRequest,
    caller: str = Depends(hitl_gate("sms_send")),
) -> Any:
    """Send an SMS via the self-hosted sms-gateway server.

    HITL-gated — the first call fires a Telegram approval prompt; on
    approve the same request replays through and reaches this handler.
    """
    cfg = _gateway_config()
    if cfg is None:
        return {"error": "SMS_GATEWAY_URL/USER/PASS not configured"}
    base_url, auth = cfg

    payload = {"message": body.body, "phoneNumbers": [body.to]}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base_url}/api/3rdparty/v1/messages",
                json=payload,
                auth=auth,
            )
    except httpx.HTTPError as e:
        log.exception("sms send: transport error")
        return {"error": f"gateway transport error: {e}"}

    if resp.status_code >= 400:
        log.warning("sms send: gateway returned %d %s", resp.status_code, resp.text[:200])
        return {
            "error": f"gateway returned {resp.status_code}",
            "detail": resp.text[:500],
        }

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:500]}

    return {
        "status": "sent",
        "to": body.to,
        "gateway": data,
    }


# ---------------------------------------------------------------------------
# Inbound-toggle proxy → hive-comms /sms/inbound/enabled
# ---------------------------------------------------------------------------


def _comms_config() -> tuple[str, dict[str, str]]:
    url = os.environ.get("COMMS_URL", "").strip().rstrip("/")
    token = os.environ.get("COMMS_BEARER_TOKEN", "").strip()
    if not url:
        raise HTTPException(503, "COMMS_URL not configured")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return url, headers


@router.get("/inbound/enabled")
async def get_inbound_enabled(_caller: str = Depends(require_api_token)) -> Any:
    """Proxy: read the current inbound-listener flag from hive-comms."""
    base_url, headers = _comms_config()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{base_url}/sms/inbound/enabled", headers=headers)
    except httpx.HTTPError as e:
        log.exception("sms/inbound/enabled get: transport error")
        raise HTTPException(502, f"comms transport error: {e}") from e
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:500])
    return resp.json()


@router.put("/inbound/enabled")
async def set_inbound_enabled(body: SmsInboundEnabledRequest,
                              _caller: str = Depends(require_api_token)) -> Any:
    """Proxy: flip the inbound-listener flag in hive-comms."""
    base_url, headers = _comms_config()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.put(
                f"{base_url}/sms/inbound/enabled",
                json={"enabled": body.enabled},
                headers=headers,
            )
    except httpx.HTTPError as e:
        log.exception("sms/inbound/enabled put: transport error")
        raise HTTPException(502, f"comms transport error: {e}") from e
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text[:500])
    return resp.json()
