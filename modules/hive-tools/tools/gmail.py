"""Gmail FastAPI router -- migrated from hive_mind_mcp."""

from __future__ import annotations

import base64
import html
import logging
import re
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from hitl import hitl_gate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/gmail", tags=["gmail"])

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


# ---- Service helper ----


def _get_gmail_service() -> Any:
    """Build and return an authenticated Gmail API service, or None."""
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        log.error("Google API libraries not installed")
        return None

    token_paths = [
        Path("credentials/token.json"),
        Path.home() / ".gmail-mcp" / "token.json",
    ]

    creds = None
    for path in token_paths:
        if path.exists():
            creds = Credentials.from_authorized_user_file(str(path), SCOPES)
            break

    if creds is None:
        log.error("No Gmail token found")
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            for path in token_paths:
                if path.exists():
                    try:
                        path.write_text(creds.to_json())
                    except OSError:
                        log.warning("Could not persist refreshed token to %s", path)
                    break
        except RefreshError:
            log.error("Gmail refresh token expired")
            return None

    return build("gmail", "v1", credentials=creds)


# ---- MIME body extraction ----


def _html_to_text(h: str) -> str:
    """Convert HTML to readable plain text."""
    h = re.sub(r"<style[^>]*>.*?</style>", "", h, flags=re.DOTALL | re.IGNORECASE)
    h = re.sub(r"<script[^>]*>.*?</script>", "", h, flags=re.DOTALL | re.IGNORECASE)
    h = re.sub(
        r'<a\s[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
        r"\2 (\1)",
        h,
        flags=re.DOTALL | re.IGNORECASE,
    )
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.IGNORECASE)
    h = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", h, flags=re.IGNORECASE)
    h = re.sub(r"<[^>]+>", "", h)
    h = html.unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def _decode_body(data: str, charset: str = "utf-8") -> str:
    raw = base64.urlsafe_b64decode(data)
    return raw.decode(charset, errors="replace")


def _extract_body(payload: dict[str, Any]) -> tuple[str, str]:
    """Walk MIME tree and extract body text."""
    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])
    charset = "utf-8"
    for param in payload.get("headers", []):
        if param.get("name", "").lower() == "content-type":
            m = re.search(r"charset=([^\s;]+)", param.get("value", ""), re.IGNORECASE)
            if m:
                charset = m.group(1).strip("\"'")

    body_data = payload.get("body", {}).get("data")
    if body_data and mime_type == "text/plain":
        return _decode_body(body_data, charset), "text/plain"
    if body_data and mime_type == "text/html":
        return _html_to_text(_decode_body(body_data, charset)), "text/html_converted"

    plain_result = None
    html_result = None
    for part in parts:
        text, btype = _extract_body(part)
        if text:
            if btype == "text/plain" and plain_result is None:
                plain_result = (text, btype)
            elif btype == "text/html_converted" and html_result is None:
                html_result = (text, btype)

    if plain_result:
        return plain_result
    if html_result:
        return html_result

    return ("", "none")


def _extract_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract attachment metadata from MIME tree."""
    attachments: list[dict[str, Any]] = []
    parts = payload.get("parts", [])
    for part in parts:
        filename = part.get("filename")
        if filename:
            attachments.append({
                "filename": filename,
                "mime_type": part.get("mimeType", "unknown"),
                "size": part.get("body", {}).get("size", 0),
            })
        attachments.extend(_extract_attachments(part))
    return attachments


def _get_header(headers: list[dict[str, str]], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _send_mime(service: Any, msg: Any, thread_id: str = "") -> dict[str, Any]:
    """Send a built MIME message: the one send path every write here uses."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    result = service.users().messages().send(userId="me", body=payload).execute()
    return {
        "status": "sent",
        "message_id": result["id"],
        "thread_id": result.get("threadId", ""),
    }


# ---- Labels ----


class UnknownLabel(ValueError):
    """A label given by name that the account does not have."""


def _account_labels(service: Any) -> list[dict[str, Any]]:
    return service.users().labels().list(userId="me").execute().get("labels", [])


def _find_label(labels: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The label called `name` - exact first, then case-insensitively.

    Gmail treats label names as case-insensitive (it refuses "receipts"
    beside "Receipts"), so the fallback finds what Gmail would call the same.
    """
    for lbl in labels:
        if lbl.get("name") == name:
            return lbl
    folded = name.casefold()
    for lbl in labels:
        if str(lbl.get("name", "")).casefold() == folded:
            return lbl
    return None


def _resolve_label_ids(service: Any, entries: list[str]) -> list[str]:
    """Label ids for a mix of ids and names; an unknown name raises.

    An entry that is already an id is passed through, so callers that send
    ids keep working. Anything else must be the name of one of the
    account's labels.
    """
    if not entries:
        return []
    labels = _account_labels(service)
    ids = {lbl["id"] for lbl in labels}
    resolved: list[str] = []
    unknown: list[str] = []
    for entry in entries:
        if entry in ids:
            resolved.append(entry)
            continue
        found = _find_label(labels, entry)
        if found is None:
            unknown.append(entry)
        else:
            resolved.append(found["id"])
    if unknown:
        raise UnknownLabel(", ".join(unknown))
    return resolved


# ---- Unsubscribe ----

#: The outbound transport for one-click unsubscribe POSTs. None is httpx's
#: real network transport; tests swap in a MockTransport.
_UNSUBSCRIBE_TRANSPORT: httpx.BaseTransport | None = None

ONE_CLICK_BODY = "List-Unsubscribe=One-Click"


def _unsubscribe_plan(list_unsubscribe: str, list_unsubscribe_post: str) -> dict[str, Any]:
    """Decide how to unsubscribe from a message's List-Unsubscribe headers.

    RFC 8058 one-click (an https URL plus `List-Unsubscribe-Post:
    List-Unsubscribe=One-Click`) wins; a mailto is next, because it can be
    sent without a browser; a bare web link is last and is only reported.
    """
    targets = re.findall(r"<\s*([^>]+?)\s*>", list_unsubscribe or "")
    https = [t for t in targets if t.lower().startswith("https://")]
    web = [t for t in targets if t.lower().startswith(("https://", "http://"))]
    mailto = [t for t in targets if t.lower().startswith("mailto:")]
    one_click = ONE_CLICK_BODY.lower() in (list_unsubscribe_post or "").lower().replace(" ", "")

    if https and one_click:
        return {"method": "one_click", "url": https[0]}
    if mailto:
        return {"method": "mailto", "mailto": mailto[0]}
    if web:
        return {"method": "browser", "url": web[0]}
    return {"method": "none"}


def _parse_mailto(mailto: str) -> tuple[str, str, str]:
    """(to, subject, body) from a mailto: URI, with unsubscribe defaults."""
    parts = urlsplit(mailto)
    to = unquote(parts.path)
    query = {k.lower(): v for k, v in parse_qs(parts.query).items()}
    if not to and "to" in query:
        to = query["to"][0]
    subject = query.get("subject", ["unsubscribe"])[0] or "unsubscribe"
    body = query.get("body", ["unsubscribe"])[0] or "unsubscribe"
    return to, subject, body


def _one_click_post(url: str) -> httpx.Response:
    with httpx.Client(transport=_UNSUBSCRIBE_TRANSPORT, timeout=30.0,
                      follow_redirects=False) as client:
        return client.post(
            url,
            content=ONE_CLICK_BODY,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )


# ---- Request models for write endpoints ----


class GmailSendBody(BaseModel):
    to: str
    subject: str
    body: str
    cc: str = ""
    # Optional single attachment. Pass the file content as base64 (one-line,
    # no wrapping — produced by `base64 -w 0`). MIME type is inferred from
    # the filename; falls back to application/octet-stream.
    attachment_filename: str = ""
    attachment_content_b64: str = ""


class GmailReplyBody(BaseModel):
    message_id: str
    body: str


class GmailTrashBody(BaseModel):
    message_id: str


class GmailMoveBody(BaseModel):
    message_id: str
    # Comma-separated label ids or label names, mixed freely.
    add_labels: str = ""
    remove_labels: str = ""


class GmailCreateLabelBody(BaseModel):
    # Nested labels use "/", e.g. "Receipts/2026".
    name: str


class GmailUnsubscribeBody(BaseModel):
    message_id: str


# ---- Read endpoints (default HITL: off) ----


@router.get("/messages")
async def read_emails(
    query: str = Query(...),
    max_results: int = 10,
    caller: str = Depends(hitl_gate("gmail_read_emails")),
) -> Any:
    """Search and fetch emails."""
    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    max_results = max(1, min(max_results, 250))
    total_estimated = 0

    try:
        message_ids: list[str] = []
        page_token = None
        while len(message_ids) < max_results:
            page_size = min(100, max_results - len(message_ids))
            result = service.users().messages().list(
                userId="me", q=query, maxResults=page_size, pageToken=page_token
            ).execute()
            total_estimated = result.get("resultSizeEstimate", 0)
            for msg in result.get("messages", []):
                message_ids.append(msg["id"])
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        messages = []
        for msg_id in message_ids:
            msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            headers = msg.get("payload", {}).get("headers", [])
            body, body_type = _extract_body(msg.get("payload", {}))
            messages.append({
                "id": msg["id"],
                "thread_id": msg.get("threadId", ""),
                "from": _get_header(headers, "From"),
                "to": _get_header(headers, "To"),
                "subject": _get_header(headers, "Subject"),
                "date": _get_header(headers, "Date"),
                "snippet": msg.get("snippet", ""),
                "body": body,
                "body_type": body_type,
                "labels": msg.get("labelIds", []),
            })

        return {"messages": messages, "returned": len(messages), "total_estimated": total_estimated}
    except Exception as e:
        log.exception("read_emails failed")
        return {"error": str(e)}


@router.get("/messages/{message_id}")
async def get_email(
    message_id: str,
    caller: str = Depends(hitl_gate("gmail_get_email")),
) -> Any:
    """Fetch a single email by ID."""
    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = msg.get("payload", {}).get("headers", [])
        body, body_type = _extract_body(msg.get("payload", {}))
        attachments = _extract_attachments(msg.get("payload", {}))

        return {
            "id": msg["id"],
            "thread_id": msg.get("threadId", ""),
            "from": _get_header(headers, "From"),
            "to": _get_header(headers, "To"),
            "cc": _get_header(headers, "Cc"),
            "subject": _get_header(headers, "Subject"),
            "date": _get_header(headers, "Date"),
            "snippet": msg.get("snippet", ""),
            "body": body,
            "body_type": body_type,
            "labels": msg.get("labelIds", []),
            "attachments": attachments,
        }
    except Exception as e:
        log.exception("get_email failed")
        return {"error": str(e)}


@router.get("/labels")
async def list_labels(
    caller: str = Depends(hitl_gate("gmail_list_labels")),
) -> Any:
    """List all Gmail labels."""
    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    try:
        result = service.users().labels().list(userId="me").execute()
        return [
            {"id": lbl["id"], "name": lbl["name"], "type": lbl.get("type", "user")}
            for lbl in result.get("labels", [])
        ]
    except Exception as e:
        log.exception("list_labels failed")
        return {"error": str(e)}


# ---- Write endpoints (default HITL: on) ----


@router.post("/send")
async def send_email(
    body: GmailSendBody,
    caller: str = Depends(hitl_gate("gmail_send_email")),
) -> Any:
    """Send an email via Gmail."""
    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    try:
        from email.mime.multipart import MIMEMultipart
        from email.mime.base import MIMEBase
        from email import encoders
        import mimetypes

        has_attachment = bool(body.attachment_filename and body.attachment_content_b64)

        if has_attachment:
            msg = MIMEMultipart()
            msg.attach(MIMEText(body.body, "plain"))

            ctype, _ = mimetypes.guess_type(body.attachment_filename)
            if ctype is None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)

            attachment_bytes = base64.b64decode(body.attachment_content_b64)
            part = MIMEBase(maintype, subtype)
            part.set_payload(attachment_bytes)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=body.attachment_filename,
            )
            msg.attach(part)
        else:
            msg = MIMEText(body.body)

        msg["to"] = body.to
        msg["subject"] = body.subject
        if body.cc:
            msg["cc"] = body.cc

        return _send_mime(service, msg)
    except Exception as e:
        log.exception("send_email failed")
        return {"error": str(e)}


@router.post("/reply")
async def reply_to_email(
    body: GmailReplyBody,
    caller: str = Depends(hitl_gate("gmail_reply_email")),
) -> Any:
    """Reply to an email thread."""
    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    try:
        original = service.users().messages().get(
            userId="me", id=body.message_id, format="metadata",
            metadataHeaders=["Subject", "From", "To", "Message-ID"],
        ).execute()

        headers = original.get("payload", {}).get("headers", [])
        orig_subject = _get_header(headers, "Subject")
        orig_from = _get_header(headers, "From")
        orig_msg_id = _get_header(headers, "Message-ID")
        thread_id = original.get("threadId", "")

        subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"

        msg = MIMEText(body.body)
        msg["to"] = orig_from
        msg["subject"] = subject
        if orig_msg_id:
            msg["In-Reply-To"] = orig_msg_id
            msg["References"] = orig_msg_id

        return _send_mime(service, msg, thread_id)
    except Exception as e:
        log.exception("reply_to_email failed")
        return {"error": str(e)}


@router.post("/trash")
async def trash_email(
    body: GmailTrashBody,
    caller: str = Depends(hitl_gate("gmail_delete_email")),
) -> Any:
    """Move a Gmail message to Trash."""
    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    try:
        service.users().messages().trash(userId="me", id=body.message_id).execute()
        return {"status": "trashed", "message_id": body.message_id}
    except Exception as e:
        log.exception("delete_email failed")
        return {"error": str(e)}


@router.post("/move")
async def move_email(
    body: GmailMoveBody,
    caller: str = Depends(hitl_gate("gmail_move_email")),
) -> Any:
    """Modify Gmail labels on a message."""
    add = [lbl.strip() for lbl in body.add_labels.split(",") if lbl.strip()] if body.add_labels else []
    remove = [lbl.strip() for lbl in body.remove_labels.split(",") if lbl.strip()] if body.remove_labels else []

    if not add and not remove:
        return {"error": "Provide at least one of add_labels or remove_labels."}

    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    try:
        try:
            add_ids = _resolve_label_ids(service, add)
            remove_ids = _resolve_label_ids(service, remove)
        except UnknownLabel as e:
            return {"error": f"Unknown label(s): {e}"}
        result = service.users().messages().modify(
            userId="me",
            id=body.message_id,
            body={"addLabelIds": add_ids, "removeLabelIds": remove_ids},
        ).execute()
        return {
            "status": "updated",
            "message_id": body.message_id,
            "labels": result.get("labelIds", []),
        }
    except Exception as e:
        log.exception("move_email failed")
        return {"error": str(e)}


@router.post("/labels")
async def create_label(
    body: GmailCreateLabelBody,
    caller: str = Depends(hitl_gate("gmail_create_label")),
) -> Any:
    """Create a label (nested via "/"), or return the one already there."""
    name = body.name.strip()
    if not name:
        return {"error": "Label name is empty."}

    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    try:
        existing = _find_label(_account_labels(service), name)
        if existing is not None:
            return {"id": existing["id"], "name": existing["name"], "created": False}
        try:
            created = service.users().labels().create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ).execute()
        except Exception:
            # Created between our list and our create: answer with that one.
            raced = _find_label(_account_labels(service), name)
            if raced is None:
                raise
            return {"id": raced["id"], "name": raced["name"], "created": False}
        return {"id": created["id"], "name": created["name"], "created": True}
    except Exception as e:
        log.exception("create_label failed")
        return {"error": str(e)}


@router.post("/unsubscribe")
async def unsubscribe(
    body: GmailUnsubscribeBody,
    caller: str = Depends(hitl_gate("gmail_unsubscribe")),
) -> Any:
    """Unsubscribe from a message's list via its List-Unsubscribe headers."""
    service = _get_gmail_service()
    if service is None:
        return {"error": "Gmail not authenticated"}

    try:
        msg = service.users().messages().get(
            userId="me", id=body.message_id, format="metadata",
            metadataHeaders=["List-Unsubscribe", "List-Unsubscribe-Post"],
        ).execute()
        headers = msg.get("payload", {}).get("headers", [])
        plan = _unsubscribe_plan(
            _get_header(headers, "List-Unsubscribe"),
            _get_header(headers, "List-Unsubscribe-Post"),
        )
        result: dict[str, Any] = {"message_id": body.message_id, **plan}

        if plan["method"] == "one_click":
            resp = _one_click_post(plan["url"])
            ok = resp.status_code < 400
            result.update({
                "outcome": "unsubscribed" if ok else "failed",
                "http_status": resp.status_code,
            })
            if not ok:
                # Said as an error, because `error` is what an approved
                # replay's outcome is judged failed by, for every tool.
                result["error"] = (
                    f"the one-click unsubscribe endpoint answered "
                    f"{resp.status_code}")
        elif plan["method"] == "mailto":
            to, subject, text = _parse_mailto(plan["mailto"])
            if not to:
                return {**result, "outcome": "failed", "error": "mailto has no address"}
            out = MIMEText(text)
            out["to"] = to
            out["subject"] = subject
            sent = _send_mime(service, out)
            result.update({"outcome": "email_sent", "to": to, "subject": subject,
                           "sent_message_id": sent["message_id"]})
        elif plan["method"] == "browser":
            result.update({"outcome": "needs_browser", "needs_browser": True})
        else:
            result.update({"outcome": "no_unsubscribe_header"})
        # Logged as well as returned: an approved call's response is stored
        # as the request's outcome, and this line is the host's own copy.
        log.info("gmail unsubscribe %s: %s", body.message_id, result)
        return result
    except Exception as e:
        log.exception("unsubscribe failed")
        return {"error": str(e)}
