"""Unit tests for Gmail service helpers."""

from __future__ import annotations


def test_html_to_text_strips_tags() -> None:
    from tools.gmail import _html_to_text

    result = _html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in result
    assert "world" in result
    assert "<p>" not in result
    assert "<b>" not in result


def test_html_to_text_converts_links() -> None:
    from tools.gmail import _html_to_text

    result = _html_to_text('<a href="https://example.com">Click here</a>')
    assert "Click here" in result
    assert "https://example.com" in result


def test_extract_body_prefers_plain_text() -> None:
    from tools.gmail import _extract_body
    import base64

    plain_data = base64.urlsafe_b64encode(b"Plain text body").decode()
    html_data = base64.urlsafe_b64encode(b"<p>HTML body</p>").decode()

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": plain_data},
            },
            {
                "mimeType": "text/html",
                "body": {"data": html_data},
            },
        ],
    }

    body, body_type = _extract_body(payload)
    assert body == "Plain text body"
    assert body_type == "text/plain"


def test_extract_attachments_finds_files() -> None:
    from tools.gmail import _extract_attachments

    payload = {
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": "dGVzdA=="},
            },
            {
                "filename": "report.pdf",
                "mimeType": "application/pdf",
                "body": {"size": 12345},
            },
        ],
    }

    attachments = _extract_attachments(payload)
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "report.pdf"
    assert attachments[0]["mime_type"] == "application/pdf"
    assert attachments[0]["size"] == 12345
