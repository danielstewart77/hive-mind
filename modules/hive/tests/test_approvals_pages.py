"""B18, the hive site's half: a person not signed in is sent to sign-in and
sees nothing; a person who may decide sees the request and decides with
their own key; anyone else is told they may not and sees none of it."""

import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod
from hive_site.testing import run

ID = "3f2a9c1e-7d4b-4a55-9c1e-0a1b2c3d4e5f"
REVIEW = {
    "hitl_request_id": ID, "caller_mind": "ada", "tool_name": "gmail_send",
    "status": "pending", "created_at": 1_700_000_000, "expires_at": 1_700_000_300,
    "decided_at": None, "decided_by": None, "params": "{}",
    "method": "POST", "path": "/gmail/send",
    "body": '{"to": "sam@example.com", "body": "the whole message text"}',
}


def _signed_in(monkeypatch, username="sam"):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = run(auth.create_user(username, "secret-pass", is_admin=False, replace=True))
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, run(auth.create_session_token(user)))
    return client


def _tools_answers(status, payload, sent):
    async def fake(method, path, json_body=None):
        sent.append((method, path, json_body))
        return httpx.Response(status, json=payload)
    return fake


def test_not_signed_in_is_sent_to_sign_in_and_back_to_the_same_request(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    asked = []
    with patch.object(main_mod, "_tools_as_person", _tools_answers(200, REVIEW, asked)):
        response = TestClient(main_mod.app).get(f"/approvals/{ID}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next=/approvals/{ID}"
    assert asked == []
    assert "gmail" not in response.text


def test_a_person_who_may_decide_sees_what_it_will_do_and_decides(monkeypatch):
    client = _signed_in(monkeypatch)
    sent = []
    with patch.object(main_mod, "_tools_as_person", _tools_answers(200, REVIEW, sent)):
        page = client.get(f"/approvals/{ID}")
        client.post(f"/approvals/{ID}/decision", data={"action": "approve"})

    assert page.status_code == 200
    for shown in ("ada", "gmail_send", "sam@example.com", "the whole message text", "Approve", "Deny"):
        assert shown in page.text
    assert ("POST", f"/hitl/{ID}/respond", {"action": "approve"}) in sent


def test_a_person_who_may_not_decide_is_told_so_and_sees_none_of_it(monkeypatch):
    client = _signed_in(monkeypatch, "mallory")
    with patch.object(main_mod, "_tools_as_person",
                      _tools_answers(403, {"detail": "no"}, [])):
        page = client.get(f"/approvals/{ID}")

    assert page.status_code == 200
    assert "may not decide" in page.text
    assert "the whole message text" not in page.text
    assert "Approve" not in page.text


def test_a_decided_request_shows_who_and_when_and_no_buttons(monkeypatch):
    client = _signed_in(monkeypatch)
    decided = {**REVIEW, "status": "approved", "decided_by": "sam",
               "decided_at": 1_700_000_100}
    with patch.object(main_mod, "_tools_as_person", _tools_answers(200, decided, [])):
        page = client.get(f"/approvals/{ID}")

    assert "Approved" in page.text and "by sam" in page.text
    assert "2023-11-14 22:15:00 UTC" in page.text
    assert 'value="approve"' not in page.text
