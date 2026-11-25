"""API tests for Gmail endpoints."""

from __future__ import annotations

import base64
from collections.abc import Generator

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_api import generate_token
from tests.scratch import db, run


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    from server import create_app

    return create_app()


@pytest.fixture()
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    # Entered, not merely constructed: the store opens in the app's
    # lifespan, and a client that never enters it is an app whose database
    # was never opened.
    with TestClient(app, raise_server_exceptions=False) as entered:
        yield entered


@pytest.fixture()
def auth_header(client: TestClient) -> dict[str, str]:
    raw = run(generate_token("ada"))
    return {"Authorization": f"Bearer {raw}"}


def _mock_gmail_service() -> MagicMock:
    service = MagicMock()
    service.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg1"}],
        "resultSizeEstimate": 1,
    }
    service.users().messages().get().execute.return_value = {
        "id": "msg1",
        "threadId": "thread1",
        "payload": {
            "headers": [
                {"name": "From", "value": "sender@test.com"},
                {"name": "Subject", "value": "Test Subject"},
            ],
            "mimeType": "text/plain",
            "body": {"data": "dGVzdCBib2R5"},
        },
        "snippet": "test snippet",
        "labelIds": ["INBOX"],
    }
    service.users().labels().list().execute.return_value = {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
        ]
    }
    return service


def test_read_emails_requires_auth(client: TestClient) -> None:
    resp = client.get("/gmail/messages?query=test")
    assert resp.status_code == 401


@patch("tools.gmail._get_gmail_service")
def test_read_emails_with_auth_returns_messages(
    mock_svc: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = _mock_gmail_service()
    resp = client.get("/gmail/messages?query=test", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert "messages" in data


@patch("tools.gmail._get_gmail_service")
def test_read_emails_hitl_off(
    mock_svc: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = _mock_gmail_service()
    resp = client.get("/gmail/messages?query=test", headers=auth_header)
    assert resp.status_code == 200


@patch("tools.gmail._get_gmail_service")
def test_get_email_returns_full_message(
    mock_svc: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = _mock_gmail_service()
    resp = client.get("/gmail/messages/msg1", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "msg1"


@patch("tools.gmail._get_gmail_service")
def test_list_labels_returns_array(
    mock_svc: MagicMock,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    mock_svc.return_value = _mock_gmail_service()
    resp = client.get("/gmail/labels", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_send_email_hitl_on_returns_202(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("gmail_send_email", "on"),
    )
    db.commit()

    resp = client.post(
        "/gmail/send",
        json={"to": "test@example.com", "subject": "hi", "body": "hello"},
        headers=auth_header,
    )
    assert resp.status_code == 202


def test_reply_email_hitl_on_returns_202(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("gmail_reply_email", "on"),
    )
    db.commit()

    resp = client.post(
        "/gmail/reply",
        json={"message_id": "msg1", "body": "reply text"},
        headers=auth_header,
    )
    assert resp.status_code == 202


def test_delete_email_hitl_on_returns_202(
    app: FastAPI,
    client: TestClient,
    auth_header: dict[str, str],
) -> None:
    db.execute(
        "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
        "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode, "
        "sudo_timeout_minutes = excluded.sudo_timeout_minutes",
        ("gmail_delete_email", "on"),
    )
    db.commit()

    resp = client.post(
        "/gmail/trash",
        json={"message_id": "msg1"},
        headers=auth_header,
    )
    assert resp.status_code == 202


# ---- Labels, move by name, unsubscribe ----
#
# Gmail's HTTP is stubbed as the service object the router builds; the
# outbound unsubscribe HTTP as an httpx MockTransport. Everything between
# the route and those two is the real code.


def _gates_off(*tool_names: str) -> None:
    for name in tool_names:
        db.execute(
            "INSERT INTO tool_hitl_settings (tool_name, mode) VALUES (?, ?) "
            "ON CONFLICT (tool_name) DO UPDATE SET mode = excluded.mode",
            (name, "off"),
        )
    db.commit()


def _service_with_labels(labels: list[dict[str, str]]) -> MagicMock:
    service = MagicMock()
    service.users().labels().list().execute.return_value = {"labels": labels}
    return service


def _service_with_unsubscribe_headers(headers: list[dict[str, str]]) -> MagicMock:
    service = MagicMock()
    service.users().messages().get().execute.return_value = {
        "id": "msg1", "payload": {"headers": headers},
    }
    service.users().messages().send().execute.return_value = {
        "id": "sent1", "threadId": "t1",
    }
    return service


def _sent_mime(service: MagicMock):
    import email

    raw = service.users().messages().send.call_args.kwargs["body"]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


@patch("tools.gmail._get_gmail_service")
def test_create_nested_label_sends_exact_name_and_returns_new_id(
    mock_svc: MagicMock, client: TestClient, auth_header: dict[str, str],
) -> None:
    _gates_off("gmail_create_label")
    service = _service_with_labels([{"id": "Label_1", "name": "Receipts"}])
    service.users().labels().create().execute.return_value = {
        "id": "Label_9", "name": "Receipts/2026",
    }
    mock_svc.return_value = service

    resp = client.post("/gmail/labels", json={"name": "Receipts/2026"}, headers=auth_header)

    assert resp.json()["id"] == "Label_9"
    sent = service.users().labels().create.call_args.kwargs
    assert sent["body"]["name"] == "Receipts/2026"


@patch("tools.gmail._get_gmail_service")
def test_move_by_label_names_applies_matching_label_ids(
    mock_svc: MagicMock, client: TestClient, auth_header: dict[str, str],
) -> None:
    _gates_off("gmail_move_email")
    service = _service_with_labels([
        {"id": "INBOX", "name": "INBOX"},
        {"id": "Label_7", "name": "Receipts/2026"},
    ])
    service.users().messages().modify().execute.return_value = {"labelIds": ["Label_7"]}
    mock_svc.return_value = service

    client.post(
        "/gmail/move",
        json={"message_id": "msg1", "add_labels": "Receipts/2026", "remove_labels": "INBOX"},
        headers=auth_header,
    )

    sent = service.users().messages().modify.call_args.kwargs["body"]
    assert sent == {"addLabelIds": ["Label_7"], "removeLabelIds": ["INBOX"]}


@patch("tools.gmail._get_gmail_service")
def test_unsubscribe_one_click_posts_one_click_body_to_senders_url(
    mock_svc: MagicMock, client: TestClient, auth_header: dict[str, str],
) -> None:
    import httpx

    _gates_off("gmail_unsubscribe")
    mock_svc.return_value = _service_with_unsubscribe_headers([
        {"name": "List-Unsubscribe",
         "value": "<mailto:leave@list.example>, <https://list.example/u/abc>"},
        {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
    ])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with patch("tools.gmail._UNSUBSCRIBE_TRANSPORT", httpx.MockTransport(handler)):
        client.post("/gmail/unsubscribe", json={"message_id": "msg1"}, headers=auth_header)

    assert [(r.method, str(r.url), r.content) for r in seen] == [
        ("POST", "https://list.example/u/abc", b"List-Unsubscribe=One-Click"),
    ]


@patch("tools.gmail._get_gmail_service")
def test_unsubscribe_mailto_only_sends_unsubscribe_email_to_that_address(
    mock_svc: MagicMock, client: TestClient, auth_header: dict[str, str],
) -> None:
    _gates_off("gmail_unsubscribe")
    service = _service_with_unsubscribe_headers([
        {"name": "List-Unsubscribe",
         "value": "<mailto:leave@list.example?subject=remove%20me>"},
    ])
    mock_svc.return_value = service

    client.post("/gmail/unsubscribe", json={"message_id": "msg1"}, headers=auth_header)

    sent = _sent_mime(service)
    assert (sent["to"], sent["subject"]) == ("leave@list.example", "remove me")


@patch("tools.gmail._get_gmail_service")
def test_unsubscribe_web_link_only_returns_link_marked_needs_browser(
    mock_svc: MagicMock, client: TestClient, auth_header: dict[str, str],
) -> None:
    _gates_off("gmail_unsubscribe")
    service = _service_with_unsubscribe_headers([
        {"name": "List-Unsubscribe", "value": "<https://list.example/prefs>"},
    ])
    mock_svc.return_value = service

    data = client.post(
        "/gmail/unsubscribe", json={"message_id": "msg1"}, headers=auth_header,
    ).json()

    assert (data["url"], data["needs_browser"]) == ("https://list.example/prefs", True)
