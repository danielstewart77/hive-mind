"""Unit tests for calendar service helpers."""

from __future__ import annotations


def test_get_header_fields_extracts_all_fields() -> None:
    from tools.calendar import _get_header_fields

    event = {
        "id": "abc123",
        "summary": "Team Standup",
        "description": "Daily sync",
        "location": "Zoom",
        "start": {"dateTime": "2026-01-01T10:00:00-06:00", "timeZone": "America/Chicago"},
        "end": {"dateTime": "2026-01-01T10:30:00-06:00", "timeZone": "America/Chicago"},
        "status": "confirmed",
        "htmlLink": "https://calendar.google.com/event/abc123",
        "organizer": {"email": "daniel@example.com"},
        "attendees": [
            {"email": "bob@example.com", "responseStatus": "accepted", "displayName": "Bob"},
        ],
        "recurrence": ["RRULE:FREQ=DAILY"],
    }

    result = _get_header_fields(event)
    assert result["id"] == "abc123"
    assert result["summary"] == "Team Standup"
    assert result["description"] == "Daily sync"
    assert result["location"] == "Zoom"
    assert result["start"] == "2026-01-01T10:00:00-06:00"
    assert result["end"] == "2026-01-01T10:30:00-06:00"
    assert result["organizer"] == "daniel@example.com"
    assert len(result["attendees"]) == 1
    assert result["attendees"][0]["email"] == "bob@example.com"
    assert result["recurrence"] == ["RRULE:FREQ=DAILY"]


def test_get_header_fields_handles_missing_fields() -> None:
    from tools.calendar import _get_header_fields

    event: dict[str, object] = {}
    result = _get_header_fields(event)
    assert result["id"] == ""
    assert result["summary"] == "(No title)"
    assert result["start"] == ""
    assert result["end"] == ""
    assert result["attendees"] == []
    assert result["recurrence"] == []
