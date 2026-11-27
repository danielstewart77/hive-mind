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
import threading
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
        # A held call waits this long for a person; long enough for a test to
        # decide it, short enough that a test that forgets to would not hang.
        "HITL_WAIT_SECONDS": "60",
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
    """Ada, daniel's mind, makes a gated call and waits on a person: the call
    stays open in a thread of its own, as a mind's would, and the test decides
    it through the site. Yields the held call's eventual reply as well."""
    import holder_credentials

    admin = builtin_console.admin()
    # The site makes daniel's key on its first need, as it does at runtime.
    daniel_key = testing.run(holder_credentials.user_key(SEED_USERNAME))
    ada = _mind_with_a_key("ada", SEED_USERNAME)
    reply: dict = {}

    def ask() -> None:
        try:
            reply["response"] = httpx.post(
                f"{real_tools}/gmail/labels", json=LABEL,
                headers={"Authorization": f"Bearer {ada}"}, timeout=90)
        except httpx.HTTPError as exc:  # pragma: no cover - shown by the assert
            reply["error"] = exc

    caller = threading.Thread(target=ask, daemon=True)
    caller.start()
    deadline = time.monotonic() + 30
    request_id = None
    while request_id is None:
        waiting = httpx.get(f"{real_tools}/hitl/waiting",
                            headers={"Authorization": f"Bearer {daniel_key}"}).json()
        if waiting.get("waiting"):
            request_id = waiting["waiting"][0]["hitl_request_id"]
        assert time.monotonic() < deadline, "the call never came to wait on a person"
        time.sleep(0.1)
    assert caller.is_alive(), "the call must stay open while it waits"
    yield admin, builtin_console, request_id, real_tools, ada, reply, caller
    caller.join(timeout=70)


def _status(url: str, request_id: str, mind_key: str) -> dict:
    return httpx.get(f"{url}/hitl/{request_id}",
                     headers={"Authorization": f"Bearer {mind_key}"}).json()


def test_an_administrator_signed_in_on_the_site_decides_with_their_own_key_and_the_tools_record_who(asked):
    admin, _, request_id, url, ada, reply, caller = asked

    page = admin.get(f"/approvals/{request_id}")
    decided = admin.post(f"/approvals/{request_id}/decision", data={"action": "deny"})

    assert page.status_code == 200 and "ada asks to use gmail_create_label" in page.text
    assert decided.status_code == 200 and "Denied" in decided.text and "by daniel" in decided.text
    assert _status(url, request_id, ada)["status"] == "denied"
    # The mind's own call, which stayed open, is answered with the no.
    caller.join(timeout=20)
    refusal = reply["response"]
    assert refusal.status_code == 403 and refusal.json()["approved"] is False
    assert "denied" in refusal.json()["error"]


def test_a_person_who_is_not_the_owner_or_an_administrator_is_refused_by_the_tools_and_the_request_stays_pending(asked):
    admin, console, request_id, url, ada, reply, caller = asked
    import holder_credentials

    mallory = console.settled_user(admin, "mallory", "mallory-password")
    testing.run(holder_credentials.user_key("mallory"))

    page = mallory.get(f"/approvals/{request_id}")
    decided = mallory.post(f"/approvals/{request_id}/decision", data={"action": "approve"})

    assert "may not decide" in page.text and "gmail_create_label" not in page.text
    assert "may not decide" in decided.text
    assert _status(url, request_id, ada)["status"] == "pending"
    assert caller.is_alive(), "a refused person leaves the mind's call held"
    admin.post(f"/approvals/{request_id}/decision", data={"action": "deny"})


def test_a_decision_posted_signed_out_signs_in_and_lands_on_the_request_page_not_on_the_decision(asked, builtin_console):
    admin, _, request_id, _, ada, reply, caller = asked
    anonymous = builtin_console.client()

    response = anonymous.post(f"/approvals/{request_id}/decision", data={"action": "approve"},
                              follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next=/approvals/{request_id}"
    admin.post(f"/approvals/{request_id}/decision", data={"action": "deny"})


def test_the_training_page_reaches_the_real_tools_with_the_signed_in_persons_own_key_and_an_unsigned_one_is_refused(builtin_console, real_tools):
    import holder_credentials

    admin = builtin_console.admin()
    testing.run(holder_credentials.user_key(SEED_USERNAME))

    ok = admin.get("/api/training/in-flight")
    plain = builtin_console.client().get("/api/training/in-flight", follow_redirects=False)

    assert ok.status_code == 200 and ok.json() == {"in_flight": False}
    assert plain.status_code in (401, 303)
    assert "daniel-key" not in plain.text


def test_an_administrator_approving_on_the_site_releases_the_minds_open_call_and_it_gets_the_tools_answer(asked):
    """Across the wire: the approval the person gives on the site is what
    releases the mind's own call. The tool then runs (no Gmail here, so its own
    answer is that it is not authenticated) and that answer is the reply to the
    call that was held."""
    admin, _, request_id, url, ada, reply, caller = asked

    decided = admin.post(f"/approvals/{request_id}/decision", data={"action": "approve"})
    caller.join(timeout=20)

    assert decided.status_code == 200 and "Approved" in decided.text and "by daniel" in decided.text
    released = reply["response"]
    assert released.status_code == 200
    assert released.json() == {"error": "Gmail not authenticated"}
    assert _status(url, request_id, ada)["status"] == "approved"
