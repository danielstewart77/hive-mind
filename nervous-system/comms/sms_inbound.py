"""SMS-gate.app inbound webhook helpers.

Pure functions for verifying HMAC signatures, extracting message fields from
permissive payload shapes, and formatting dispatch content. No I/O, no
FastAPI imports — keeps the unit tests fast and the route code thin.

Signature scheme: HMAC-SHA256 of `raw_body + timestamp_string` against the
shared secret, lowercase hex. Headers arrive as `X-Signature` and
`X-Timestamp` (unix seconds, string).

Payload extraction is permissive because the public docs are stale on the
`mms:downloaded` event shape — we accept the documented `sms:received`
fields too and fall back through plausible aliases.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any


def verify_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """Constant-time compare HMAC-SHA256(body + timestamp, secret) to signature."""
    if not (body is not None and timestamp and signature and secret):
        return False
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(body)
    mac.update(timestamp.encode("utf-8"))
    expected = mac.hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def extract_message_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull sender, text, message_id, received_at from a permissive payload.

    The gateway nests the actual event fields under "payload" while the
    envelope carries event type and device_id. Falls back to top-level
    lookups so the function still works on flat shapes during testing.

    Returns None values for missing fields — caller decides how to handle.
    """
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload

    def first(*keys: str) -> Any:
        for k in keys:
            v = inner.get(k)
            if v not in (None, ""):
                return v
        return None

    return {
        "sender": first("phoneNumber", "sender", "from"),
        "text": first("message", "text", "body", "contentPreview"),
        "message_id": first("messageId", "id"),
        "received_at": first("receivedAt", "received_at", "createdAt"),
        "event": payload.get("event"),
        "device_id": payload.get("deviceId") or payload.get("device_id"),
    }


def format_dispatch_content(sender: str | None, text: str | None) -> str:
    """Format the broker message Ada (or any recipient) receives."""
    s = sender or "unknown sender"
    t = text or "(no text in webhook payload)"
    return f"Inbound SMS from {s}: {t}"


def build_broker_message_id(sender: str | None, gateway_message_id: str | None) -> str:
    """Stable broker message_id for an inbound SMS/MMS webhook.

    The sms-gate.app client retries non-2xx deliveries, so the broker needs
    a deterministic dedup key. Compose from (sender, gateway message id);
    fall back to a UUID when either is missing.
    """
    if sender and gateway_message_id:
        return f"sms-{sender}-{gateway_message_id}"
    return str(uuid.uuid4())
