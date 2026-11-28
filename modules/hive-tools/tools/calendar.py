"""Google Calendar FastAPI router -- migrated from hive_mind_mcp."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from hitl import hitl_gate
from schemas import CalendarCreateRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_calendar_service() -> Any:
    """Build and return an authenticated Calendar API service, or None."""
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        log.error("Google API libraries not installed")
        return None

    token_paths = [
        Path("credentials/calendar_token.json"),
        Path.home() / ".gmail-mcp" / "calendar_token.json",
    ]

    creds = None
    for path in token_paths:
        if path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(path), SCOPES)
            except Exception as e:
                log.error("Could not read Calendar token from %s: %s", path, e)
                return None
            break

    if creds is None:
        log.error("No Calendar token found")
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
            log.error("Calendar refresh token expired")
            return None

    try:
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        log.error("Failed to build Calendar service: %s", e)
        return None


def _get_header_fields(event: dict[str, Any]) -> dict[str, Any]:
    """Extract common fields from a Calendar event into a flat dict."""
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id", ""),
        "summary": event.get("summary", "(No title)"),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "start": start.get("dateTime", start.get("date", "")),
        "end": end.get("dateTime", end.get("date", "")),
        "start_timezone": start.get("timeZone", ""),
        "status": event.get("status", ""),
        "html_link": event.get("htmlLink", ""),
        "organizer": event.get("organizer", {}).get("email", ""),
        "attendees": [
            {
                "email": a.get("email", ""),
                "response": a.get("responseStatus", ""),
                "display_name": a.get("displayName", ""),
            }
            for a in event.get("attendees", [])
        ],
        "recurrence": event.get("recurrence", []),
    }


# ---- Read endpoints (default HITL: off) ----


@router.get("/events")
async def list_events(
    query: str = "",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 25,
    calendar_id: str = "primary",
    caller: str = Depends(hitl_gate("calendar_list_events")),
) -> Any:
    """List/search calendar events."""
    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    max_results = max(1, min(max_results, 250))

    if not time_min:
        time_min = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if not time_max:
        time_max = (datetime.now(UTC) + timedelta(days=7)).isoformat().replace("+00:00", "Z")

    try:
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if query:
            kwargs["q"] = query

        result = service.events().list(**kwargs).execute()
        events = [_get_header_fields(e) for e in result.get("items", [])]
        return {"events": events, "returned": len(events), "calendar": calendar_id}
    except Exception as e:
        log.exception("list_calendar_events failed")
        return {"error": str(e)}


@router.get("/events/{event_id}")
async def get_event(
    event_id: str,
    calendar_id: str = "primary",
    caller: str = Depends(hitl_gate("calendar_get_event")),
) -> Any:
    """Get a single calendar event by ID."""
    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return _get_header_fields(event)
    except Exception as e:
        log.exception("get_calendar_event failed")
        return {"error": str(e)}


@router.get("/list")
async def list_calendars(
    caller: str = Depends(hitl_gate("calendar_list")),
) -> Any:
    """List all calendars the user has access to."""
    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    try:
        result = service.calendarList().list().execute()
        return [
            {
                "id": c["id"],
                "summary": c.get("summary", ""),
                "timezone": c.get("timeZone", ""),
                "access_role": c.get("accessRole", ""),
                "primary": c.get("primary", False),
            }
            for c in result.get("items", [])
        ]
    except Exception as e:
        log.exception("list_calendars failed")
        return {"error": str(e)}


@router.get("/availability")
async def check_availability(
    time_min: str = Query(...),
    time_max: str = Query(...),
    calendar_ids: str = "primary",
    caller: str = Depends(hitl_gate("calendar_check_availability")),
) -> Any:
    """Check free/busy availability."""
    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    ids = [c.strip() for c in calendar_ids.split(",") if c.strip()]

    try:
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": cid} for cid in ids],
        }
        result = service.freebusy().query(body=body).execute()
        availability: dict[str, Any] = {}
        for cid in ids:
            cal_data = result.get("calendars", {}).get(cid, {})
            availability[cid] = {
                "busy": cal_data.get("busy", []),
                "errors": cal_data.get("errors", []),
            }
        return availability
    except Exception as e:
        log.exception("check_availability failed")
        return {"error": str(e)}


# ---- Write endpoints (default HITL: on) ----


@router.post("/events")
async def create_event(
    body: CalendarCreateRequest,
    caller: str = Depends(hitl_gate("calendar_create_event")),
) -> Any:
    """Create a new calendar event."""
    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    try:
        event_body: dict[str, Any] = {
            "summary": body.summary,
            "start": {"dateTime": body.start_time, "timeZone": body.timezone},
            "end": {"dateTime": body.end_time, "timeZone": body.timezone},
        }
        if body.description:
            event_body["description"] = body.description
        if body.location:
            event_body["location"] = body.location
        if body.attendees:
            emails = [e.strip() for e in body.attendees.split(",") if e.strip()]
            event_body["attendees"] = [{"email": e} for e in emails]

        result = service.events().insert(
            calendarId=body.calendar_id,
            body=event_body,
            sendUpdates="all" if body.attendees else "none",
        ).execute()

        return {
            "status": "created",
            "event_id": result["id"],
            "html_link": result.get("htmlLink", ""),
            "summary": body.summary,
        }
    except Exception as e:
        log.exception("create_calendar_event failed")
        return {"error": str(e)}


@router.post("/events/quick-add")
async def quick_add(
    text: str = Query(...),
    calendar_id: str = "primary",
    caller: str = Depends(hitl_gate("calendar_quick_add")),
) -> Any:
    """Create an event from natural language."""
    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    try:
        result = service.events().quickAdd(calendarId=calendar_id, text=text).execute()
        return {
            "status": "created",
            "event_id": result["id"],
            "summary": result.get("summary", ""),
            "start": result.get("start", {}),
            "end": result.get("end", {}),
            "html_link": result.get("htmlLink", ""),
        }
    except Exception as e:
        log.exception("quick_add_event failed")
        return {"error": str(e)}


@router.patch("/events/{event_id}")
async def update_event(
    event_id: str,
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    location: str = "",
    timezone: str = "",
    calendar_id: str = "primary",
    caller: str = Depends(hitl_gate("calendar_update_event")),
) -> Any:
    """Update an existing calendar event."""
    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    try:
        patch_body: dict[str, Any] = {}
        if summary:
            patch_body["summary"] = summary
        if description:
            patch_body["description"] = description
        if location:
            patch_body["location"] = location

        tz = timezone or "America/Chicago"
        if start_time:
            patch_body["start"] = {"dateTime": start_time, "timeZone": tz}
        if end_time:
            patch_body["end"] = {"dateTime": end_time, "timeZone": tz}

        if not patch_body:
            return {"error": "No fields to update."}

        result = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=patch_body,
            sendUpdates="all",
        ).execute()

        return {
            "status": "updated",
            "event_id": result["id"],
            "summary": result.get("summary", ""),
            "html_link": result.get("htmlLink", ""),
        }
    except Exception as e:
        log.exception("update_calendar_event failed")
        return {"error": str(e)}


@router.delete("/events/{event_id}")
async def delete_event(
    event_id: str,
    calendar_id: str = "primary",
    caller: str = Depends(hitl_gate("calendar_delete_event")),
) -> Any:
    """Delete a calendar event."""
    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    try:
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates="all",
        ).execute()
        return {"status": "deleted", "event_id": event_id}
    except Exception as e:
        log.exception("delete_calendar_event failed")
        return {"error": str(e)}


@router.post("/events/{event_id}/invite")
async def invite_to_event(
    event_id: str,
    attendee_emails: str = Query(...),
    calendar_id: str = "primary",
    caller: str = Depends(hitl_gate("calendar_invite")),
) -> Any:
    """Add attendees to an existing event."""
    emails = [e.strip() for e in attendee_emails.split(",") if e.strip()]
    if not emails:
        return {"error": "No email addresses provided."}

    service = _get_calendar_service()
    if service is None:
        return {"error": "Calendar not authenticated"}

    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        existing = event.get("attendees", [])
        existing_emails = {a.get("email", "").lower() for a in existing}
        for email in emails:
            if email.lower() not in existing_emails:
                existing.append({"email": email})

        result = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body={"attendees": existing},
            sendUpdates="all",
        ).execute()

        return {
            "status": "updated",
            "event_id": result["id"],
            "summary": result.get("summary", ""),
            "attendees": [
                {"email": a.get("email"), "response": a.get("responseStatus")}
                for a in result.get("attendees", [])
            ],
        }
    except Exception as e:
        log.exception("invite_to_event failed")
        return {"error": str(e)}
