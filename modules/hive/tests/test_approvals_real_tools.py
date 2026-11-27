"""B18 across the wire: the hive site carries the signed-in person's own key to
the real tools application, and the tools' own rules decide.

The tools are not imitated: their application runs in its own process (its
modules share names with the console's) against the suite's scratch Postgres,
the same record of keys the site writes the person's key into. So what these
tests show is the answer of the real rule, not of a stand-in that was told what
to say."""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hive_site import testing

from .multiuser_support import SEED_USERNAME, free_port

TOOLS = Path(__file__).resolve().parents[2] / "hive-tools"
LABEL = {"name": "Receipts/2026"}


@pytest.fixture
def real_tools(scratch_database, monkeypatch):
    port = free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "HIVE_DATABASE_URL": scratch_database.dsn,
        "HIVE_SITE_PUBLIC_URL": "https://site.test",
        "COMMS_URL": "http://127.0.0.1:9",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=TOOLS, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, "the tools did not start"
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "the tools did not answer"
            time.sleep(0.2)
        monkeypatch.setenv("HIVE_TOOLS_URL", url)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _mind_with_a_key(name: str, owner: str) -> str:
    """A mind owned by `owner`, and its own key in the one record."""
    from hive_data import access, core

    key = secrets.token_urlsafe(24)

    async def make():
        now = time.time()
        await core.upsert_mind(mind_id=f"id-{name}", name=name, gateway_url="http://x",
                               model="m", harness="h", registered_at=now,
                               last_seen=now, owner=owner)
        await core.upsert_credential(
            holder=name, username=owner, role="mind", token_sha=access.digest(key),
            created_at=now, revoked_at=None)

    testing.run(make())
    return key


@pytest.fixture
def asked(builtin_console, real_tools):
    """Ada, daniel's mind, asks for a gated call and waits on a person."""
    import holder_credentials

    admin = builtin_console.admin()
    # The site makes daniel's key on its first need, as it does at runtime.
    testing.run(holder_credentials.user_key(SEED_USERNAME))
    ada = _mind_with_a_key("ada", SEED_USERNAME)
    raised = httpx.post(f"{real_tools}/gmail/labels", json=LABEL,
                        headers={"Authorization": f"Bearer {ada}"})
    assert raised.status_code == 202, raised.text
    return admin, builtin_console, raised.json()["hitl_request_id"], real_tools, ada


def _status(url: str, request_id: str, mind_key: str) -> dict:
    return httpx.get(f"{url}/hitl/{request_id}",
                     headers={"Authorization": f"Bearer {mind_key}"}).json()


def test_an_administrator_signed_in_on_the_site_decides_with_their_own_key_and_the_tools_record_who(asked):
    admin, _, request_id, url, ada = asked

    page = admin.get(f"/approvals/{request_id}")
    decided = admin.post(f"/approvals/{request_id}/decision", data={"action": "deny"})

    assert page.status_code == 200 and "ada asks to use gmail_create_label" in page.text
    assert decided.status_code == 200 and "Denied" in decided.text and "by daniel" in decided.text
    assert _status(url, request_id, ada)["status"] == "denied"


def test_a_person_who_is_not_the_owner_or_an_administrator_is_refused_by_the_tools_and_the_request_stays_pending(asked):
    admin, console, request_id, url, ada = asked
    import holder_credentials

    mallory = console.settled_user(admin, "mallory", "mallory-password")
    testing.run(holder_credentials.user_key("mallory"))

    page = mallory.get(f"/approvals/{request_id}")
    decided = mallory.post(f"/approvals/{request_id}/decision", data={"action": "approve"})

    assert "may not decide" in page.text and "gmail_create_label" not in page.text
    assert "may not decide" in decided.text
    assert _status(url, request_id, ada)["status"] == "pending"


def test_a_decision_posted_signed_out_signs_in_and_lands_on_the_request_page_not_on_the_decision(asked, builtin_console):
    _, _, request_id, _, ada = asked
    anonymous = builtin_console.client()

    response = anonymous.post(f"/approvals/{request_id}/decision", data={"action": "approve"},
                              follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next=/approvals/{request_id}"


def test_the_training_page_reaches_the_real_tools_with_the_signed_in_persons_own_key_and_an_unsigned_one_is_refused(builtin_console, real_tools):
    import holder_credentials

    admin = builtin_console.admin()
    testing.run(holder_credentials.user_key(SEED_USERNAME))

    ok = admin.get("/api/training/in-flight")
    plain = builtin_console.client().get("/api/training/in-flight", follow_redirects=False)

    assert ok.status_code == 200 and ok.json() == {"in_flight": False}
    assert plain.status_code in (401, 303)
    assert "daniel-key" not in plain.text
