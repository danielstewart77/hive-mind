"""Story 913, behaviour B23: work that needs a person who is not there is left
waiting, and is finished with the key that comes with that person's next click;
restarting the site signs nobody out and sends nothing.

The rename goes through the real hive site and the real rename code (only the
tools and gateway are a recording transport); the design submission through the
real delivery with the gateway as the recording boundary.
"""
import json
import time

import httpx
import pytest
from hive_site import testing

import auth
import design_delivery
import holder_credentials
import mind_creation
import mind_rename
import route_access

from .test_mind_rename import rename_setup  # noqa: F401 - fixture
from .test_design_delivery import StubGateway, _session_with_feedback, design_data, store, _conversations  # noqa: F401
from .test_replace_mind_key import hive as hive_for_replacement  # noqa: F401


def _key_of(client) -> str:
    return client.cookies.get(auth.SESSION_COOKIE_NAME)


class _Recording(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner, self.seen = inner, []

    async def handle_async_request(self, request):
        # The mind's own runtime is asked with the mind's own key (the one
        # outbound call of a rename that is not a person's: see the report).
        if request.url.path != "/runtime":
            self.seen.append(request.headers.get("authorization"))
        return await self.inner.handle_async_request(request)


def _wait_for(check, what):
    deadline = time.monotonic() + 20
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def _started_by_the_owner(rename_setup, monkeypatch):
    """The owner asks for a rename; the site is then 'stopped' (nothing carries
    it on: the step the click would have started is not run)."""
    admin, ident, root, transport, state, console = rename_setup
    recording = _Recording(transport)
    monkeypatch.setattr(mind_rename, "TRANSPORT", recording)
    real_kick = mind_rename.kick
    monkeypatch.setattr(mind_rename, "kick", lambda *args: None)
    owner = console.settled_user(admin, "mind-owner", "test-password-123")
    from hive_data import broker_ops
    testing.run(broker_ops.update_mind(ident, owner="mind-owner"))
    state["listing"]["owner"] = "mind-owner"
    started = owner.post(f"/api/minds/{ident}/rename", json={"name": "renamed"})
    assert started.status_code == 202, started.text
    console.settled_user(admin, "stranger", "test-password-123")  # while nothing can carry it on
    monkeypatch.setattr(mind_rename, "kick", real_kick)
    recording.seen.clear()  # what the owner's own click sent is not the question
    return admin, owner, console, ident, started.json()["operation_id"], recording, state


def test_a_started_rename_names_the_person_and_holds_no_key_and_is_finished_with_the_next_click_of_that_person(
        rename_setup, monkeypatch):
    admin, owner, console, ident, operation_id, recording, state = _started_by_the_owner(
        rename_setup, monkeypatch)
    key = _key_of(owner)
    record = mind_rename.RENAME_DIR / f"{operation_id}.json"
    assert json.loads(record.read_text())["requester"] == "mind-owner"
    assert key not in record.read_text()
    assert [w["operation_id"] for w in mind_rename.waiting("mind-owner", False)] == [operation_id]

    # Another person's click does not carry it on.
    stranger, _ = console.sign_in("stranger", "test-password-123")
    stranger.get("/api/waiting-work")
    assert recording.seen == [] and mind_rename.latest(ident)["status"] != "success"

    # The owner's next click does, with the owner's own key on every call.
    owner.get("/overview")
    _wait_for(lambda: mind_rename.latest(ident)["status"] == "success", "the rename was not finished")
    assert recording.seen and set(recording.seen) == {f"Bearer {key}"}
    assert key not in (mind_rename.RENAME_DIR / f"{operation_id}.json").read_text()


def test_an_administrators_click_finishes_a_rename_that_waits_for_someone_else_with_the_administrators_key(
        rename_setup, monkeypatch):
    admin, owner, console, ident, operation_id, recording, state = _started_by_the_owner(
        rename_setup, monkeypatch)
    admin_key = _key_of(admin)

    shown = admin.get("/api/waiting-work").json()["waiting"]
    _wait_for(lambda: mind_rename.latest(ident)["status"] == "success", "the rename was not finished")

    assert any(w["kind"] == "rename" and w["waits_for"] == "mind-owner" for w in shown)
    assert recording.seen and set(recording.seen) == {f"Bearer {admin_key}"}


def test_restarting_the_site_signs_nobody_out_sends_nothing_and_loses_nothing(
        rename_setup, monkeypatch):
    from starlette.testclient import TestClient

    import main
    from hive_site import store

    admin, owner, console, ident, operation_id, recording, state = _started_by_the_owner(
        rename_setup, monkeypatch)
    key = _key_of(owner)
    monkeypatch.setattr(mind_rename, "kick", lambda *args: None)
    before = owner.get("/api/waiting-work").json()["waiting"]
    recording.seen.clear()

    with TestClient(main.app):  # the site starting up
        pass
    testing.run(store.open())

    assert recording.seen == []  # nothing was done at start: nobody clicked
    assert mind_rename.latest(ident)["status"] not in mind_rename.FINAL
    after = TestClient(main.app, base_url="https://testserver")
    after.cookies.set(auth.SESSION_COOKIE_NAME, key)  # a new process knows only the cookie
    assert after.get("/overview", follow_redirects=False).status_code == 200
    assert after.get("/api/waiting-work").json()["waiting"] == before


async def test_a_saved_design_submission_waits_for_who_saved_it_or_an_administrator_and_goes_with_that_clicks_key(
        store, monkeypatch):
    gateway = StubGateway().install(monkeypatch)
    record = await _session_with_feedback(store)  # saved by "daniel"
    assert gateway.calls == []

    assert await design_delivery.deliver_pending("sloan", False, "sloan-key") == 0
    assert gateway.calls == []
    assert [n["id"] for n in await design_delivery.waiting("sloan", True)] != []

    assert await design_delivery.deliver_pending("daniel", False, "daniel-key") == 1
    assert {c["authorization"] for c in gateway.calls} == {"Bearer daniel-key"}
    assert await store.pending_notices(record["id"]) == []


async def test_an_administrator_sends_a_submission_saved_by_someone_else_with_their_own_key(store, monkeypatch):
    gateway = StubGateway().install(monkeypatch)
    await _session_with_feedback(store)

    assert await design_delivery.deliver_pending("sloan-admin", True, "admin-key") == 1

    assert {c["authorization"] for c in gateway.calls} == {"Bearer admin-key"}


async def test_a_half_made_mind_is_taken_back_on_the_click_of_who_started_it_or_an_administrator_and_not_before(tmp_path):
    taken_back = []

    async def deregister(name, mind_id):
        taken_back.append(name)

    async def gateway_names(name):
        return ""

    creation = mind_creation.Creation("half", "id-half", "owner", started_by="sloan")
    creation.begin()
    creation.release()  # the request making it is gone
    assert mind_creation.waits_for(creation) == "sloan"

    await mind_creation.sweep(deregister, gateway_names, person="aiden", is_admin=False)
    assert mind_creation.is_open("half")

    await mind_creation.sweep(deregister, gateway_names, person="sloan", is_admin=False)
    assert not mind_creation.is_open("half")


async def test_a_cut_short_key_replacement_waits_for_who_started_it_or_an_administrator(hive_for_replacement):
    hive = hive_for_replacement
    started = holder_credentials.key_journal.begin("replace", "ada", ["comms"], "sloan")
    assert [r["operation_id"] for r in holder_credentials.waiting_replacements("aiden", False)] == []
    assert [r["operation_id"] for r in holder_credentials.waiting_replacements("sloan", False)] == [started["operation_id"]]
    assert [r["operation_id"] for r in holder_credentials.waiting_replacements("root", True)] == [started["operation_id"]]
    assert await holder_credentials.recover_replacements("aiden", False) == []
    assert holder_credentials.key_journal.is_open(started)
    assert hive is not None


@pytest.mark.parametrize("ending_click", ["logout", "end_everywhere"])
def test_a_click_that_ends_the_key_does_not_pick_up_waiting_work_and_the_rename_is_finished_on_the_next_sign_in(
        rename_setup, monkeypatch, ending_click):
    admin, owner, console, ident, operation_id, recording, state = _started_by_the_owner(
        rename_setup, monkeypatch)

    if ending_click == "logout":
        owner.post("/logout", follow_redirects=False)
    else:
        assert owner.post("/api/sign-ins/end").status_code == 200
    time.sleep(1)  # a pickup with the dead key would have run by now

    assert recording.seen == []
    assert mind_rename.latest(ident)["status"] not in ("recovery_required", "success")
    assert [w["operation_id"] for w in mind_rename.waiting("mind-owner", False)] == [operation_id]

    again, _ = console.sign_in("mind-owner", "test-password-123")
    again.get("/overview")
    _wait_for(lambda: mind_rename.latest(ident)["status"] == "success", "the rename was not finished")


def test_signing_out_when_the_record_cannot_be_written_says_so_and_keeps_the_cookie(
        rename_setup, monkeypatch):
    admin, owner, console, ident, operation_id, recording, state = _started_by_the_owner(
        rename_setup, monkeypatch)

    async def down(self, token):
        raise auth.ServiceUnavailable("the record is down")

    monkeypatch.setattr(auth.BuiltinStore, "sign_out", down)
    answer = owner.post("/logout", follow_redirects=False)

    assert answer.status_code == 503
    assert "set-cookie" not in {k.lower() for k in answer.headers} or "max-age=0" not in answer.headers.get("set-cookie", "").lower()
