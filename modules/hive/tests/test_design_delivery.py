"""Tests for delivering a design submission into its conversation.

The delivery is driven against a stub gateway that records the requests it
actually received — method, path and body — rather than against patched
functions inside the module under test. Asserting that a mock was called
would restate the implementation; asserting on the HTTP the gateway would
see proves the conversation is adopted before it is spoken to, which is the
invariant the whole design rests on.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import design_data
import design_delivery
from hive_site import testing as suite


class StubGateway:
    """A comms that records calls instead of running a harness."""

    def __init__(self):
        self.calls = []
        self.fail_on = None

    def install(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_URL", "http://hive-comms:8424")
        monkeypatch.setattr(design_delivery, "_post_sync", self._post)
        return self

    def _post(self, path, body, *, headers, stream):
        if self.fail_on and self.fail_on in path:
            raise OSError("gateway unavailable")
        self.calls.append(
            {
                "path": path,
                "body": body,
                "authorization": headers.get("Authorization"),
                "stream": stream,
            }
        )

    @property
    def paths(self):
        return [call["path"] for call in self.calls]



@pytest.fixture(autouse=True)
def _conversations():
    """The conversations these tests point a design session at.

    `design_sessions.conversation_id` is a real foreign key into the
    gateway's `core.sessions`, so each one is a row first.
    """
    suite.run(suite.make_conversations("conv-1", "conv-2", "conv-late"))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    return design_data


@pytest.fixture()
def gateway(monkeypatch):
    return StubGateway().install(monkeypatch)


async def _session_with_feedback(store, conversation_id="conv-1"):
    record = await store.create_design_session("Design sessions", conversation_id=conversation_id)
    published = await store.publish_page(
        record["id"],
        "requirements",
        {
            "r1": {"order": 1, "text": "Sessions are listed"},
            "r2": {"order": 2, "text": "Sessions can be deleted"},
        },
        revision=record["revision"],
    )
    return await store.submit_page(
        published["id"],
        "requirements",
        items={"r2": {"feedback": "Does that remove its notices too?"}},
        revision=published["revision"],
        submitted_by="daniel",
    )


async def test_the_conversation_is_adopted_before_it_is_spoken_to(store, gateway):
    record = await _session_with_feedback(store)
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]

    assert await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel") is True

    assert gateway.paths == [
        "/sessions/conv-1/activate",
        "/sessions/conv-1/message",
    ]
    activate = gateway.calls[0]["body"]
    assert activate["surface"] == "design"
    assert activate["surface_id"] == record["id"]
    # Sent with the key of the person who submitted, and no other.
    assert gateway.calls[0]["authorization"] == "Bearer key-of-daniel"
    assert gateway.calls[1]["authorization"] == "Bearer key-of-daniel"


async def test_the_message_carries_the_question_daniel_asked(store, gateway):
    record = await _session_with_feedback(store)
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]

    await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel")

    content = gateway.calls[1]["body"]["content"]
    assert "Does that remove its notices too?" in content
    assert "Sessions can be deleted" in content
    assert record["id"] in content


async def test_an_untouched_item_is_left_out_of_the_message(store):
    record = await _session_with_feedback(store)

    content = design_delivery.compose_message(record, "requirements", "feedback")

    assert "Sessions can be deleted" in content
    assert "Sessions are listed" not in content


async def test_a_delivered_notice_is_not_sent_again(store, gateway):
    record = await _session_with_feedback(store)
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]
    await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel")

    assert await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel") is False

    assert gateway.paths == [
        "/sessions/conv-1/activate",
        "/sessions/conv-1/message",
    ]


async def test_a_failed_send_leaves_the_notice_pending_for_another_try(store, gateway):
    record = await _session_with_feedback(store)
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]
    gateway.fail_on = "/message"

    assert await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel") is False

    still_pending = await store.pending_notices(record["id"])
    assert [notice["id"] for notice in still_pending] == [notice_id]
    assert still_pending[0]["claimed_at"] is None

    gateway.fail_on = None
    assert await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel") is True
    assert await store.pending_notices(record["id"]) == []


async def test_a_submission_with_nothing_attached_stays_pending(store, gateway):
    record = await _session_with_feedback(store, conversation_id=None)
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]

    assert await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel") is False

    assert gateway.calls == []
    assert [n["id"] for n in await store.pending_notices(record["id"])] == [notice_id]


async def test_pending_work_is_sent_on_startup(store, gateway):
    first = await _session_with_feedback(store, conversation_id="conv-1")
    second = await _session_with_feedback(store, conversation_id="conv-2")

    assert await design_delivery.deliver_pending("daniel", True, "key-of-daniel") == 2

    assert gateway.paths == [
        "/sessions/conv-1/activate",
        "/sessions/conv-1/message",
        "/sessions/conv-2/activate",
        "/sessions/conv-2/message",
    ]
    assert await store.pending_notices(first["id"]) == []
    assert await store.pending_notices(second["id"]) == []


async def test_a_notice_owed_while_nothing_was_attached_is_sent_once_it_is(store, gateway):
    record = await _session_with_feedback(store, conversation_id=None)
    assert await design_delivery.deliver_pending("daniel", True, "key-of-daniel") == 0
    assert gateway.calls == []

    await store.attach_design_session(record["id"], "conv-late", surface="telegram")

    assert await design_delivery.deliver_pending("daniel", True, "key-of-daniel") == 1
    assert gateway.paths == [
        "/sessions/conv-late/activate",
        "/sessions/conv-late/message",
    ]


async def test_a_cancelled_send_releases_its_notice_rather_than_stranding_it(store, gateway):
    record = await _session_with_feedback(store)
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]

    def cancelled(path, body, *, headers, stream):
        raise asyncio.CancelledError()

    gateway._post = cancelled
    import design_delivery as module

    original = module._post_sync
    module._post_sync = cancelled
    try:
        with pytest.raises(asyncio.CancelledError):
            await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel")
    finally:
        module._post_sync = original

    pending = await store.pending_notices(record["id"])
    assert [n["id"] for n in pending] == [notice_id]
    assert pending[0]["claimed_at"] is None


async def test_the_build_message_carries_the_whole_agreed_specification(store):
    record = await store.create_design_session("Design", conversation_id="conv-1")
    record = await store.publish_page(
        record["id"],
        "requirements",
        {"r1": {"order": 1, "text": "Every design session is listed"}},
        revision=record["revision"],
    )
    record = await store.publish_page(
        record["id"],
        "functions",
        {"f1": {"order": 1, "name": "list_design_sessions", "changed": True,
                "calls": ["_connect"]}},
        revision=record["revision"],
    )
    record = await store.publish_page(
        record["id"],
        "tests",
        {"t1": {"order": 1, "sentence": "Listing reports every session",
                "symbol": {"name": "list_design_sessions", "kind": "existing",
                           "resolved": True, "path": "src/design_data.py"},
                "cases": [
                    {"input": "two stored", "expected": "both returned", "outcome": "pass"},
                    {"input": "a deleted id", "expected": "absent", "outcome": "fail"},
                ]}},
        revision=record["revision"],
    )

    content = design_delivery.compose_message(record, "functions", "build")

    assert "Every design session is listed" in content
    assert "Listing reports every session" in content
    assert "src/design_data.py" in content
    assert "both returned" in content
    assert "calls: _connect" in content


# ---------------------------------------------------------------------------
# Requirement — accepting a page reaches the Mind. Acceptance was silent: the
# page advanced in the browser and nothing was ever told, so a design session
# stalled at an empty next page with both parties waiting on the other.
# ---------------------------------------------------------------------------
async def _accepted_requirements(store, conversation_id="conv-1"):
    record = await store.create_design_session("Design", conversation_id=conversation_id)
    record = await store.publish_page(
        record["id"],
        "requirements",
        {"r1": {"order": 1, "text": "Sessions are listed"}},
        revision=record["revision"],
    )
    return record


async def test_accepting_a_page_queues_a_notice_for_the_mind(store):
    """Breaks if accept_page stops writing a notice, or writes one under a
    kind the delivery loop does not carry."""
    record = await _accepted_requirements(store)
    assert await store.pending_notices(record["id"]) == []

    accepted = await store.accept_page(
        record["id"], "requirements", revision=record["revision"],
        submitted_by="daniel",
    )

    notices = await store.pending_notices(accepted["id"])
    assert [(n["kind"], n["page"]) for n in notices] == [("accept", "requirements")]


async def test_the_accepted_page_is_delivered_into_the_conversation(store, gateway):
    """Breaks if the notice is queued but never sent, or sent without
    adopting the conversation first."""
    record = await _accepted_requirements(store)
    accepted = await store.accept_page(
        record["id"], "requirements", revision=record["revision"],
        submitted_by="daniel",
    )
    notice_id = (await store.pending_notices(accepted["id"]))[0]["id"]

    assert await design_delivery.deliver_notice(notice_id, "daniel", "key-of-daniel") is True

    assert gateway.paths == [
        "/sessions/conv-1/activate",
        "/sessions/conv-1/message",
    ]


async def test_an_acceptance_carries_the_page_that_was_accepted(store):
    """The feedback branch reports only flagged rows, so an acceptance
    composed through it arrives saying nothing was changed or asked. Breaks
    if accept falls back to that branch, or stops carrying the items."""
    record = await _accepted_requirements(store)

    accepted_text = design_delivery.compose_message(record, "requirements", "accept")
    feedback_text = design_delivery.compose_message(record, "requirements", "feedback")

    assert "Sessions are listed" in accepted_text
    assert "Sessions are listed" not in feedback_text


async def test_the_shutdown_settles_an_in_flight_delivery_before_the_store_goes(
        store, gateway, monkeypatch):
    """A claimed notice is released as the console stops, not after.

    The delivery runs detached and holds its notice claimed across a whole
    model turn. Left to the event loop's own cancellation it is cancelled
    *after* the lifespan has closed the store, so the handler that releases
    the notice reaches a closed store and raises instead — the notice stays
    claimed, `deliver_pending` steps over it on every later sweep, and a
    submission the page reported as saved is never delivered by any path.
    """
    import main
    import store as data_store
    import design_delivery as module

    record = await _session_with_feedback(store)
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]

    sending = asyncio.Event()
    never = asyncio.Event()

    async def hangs(conversation_id, content, headers):
        sending.set()
        await never.wait()

    monkeypatch.setattr(module, "send_turn", hangs)

    task = asyncio.ensure_future(module.deliver_notice(notice_id, "daniel", "key-of-daniel"))
    main._DESIGN_DELIVERIES.add(task)
    task.add_done_callback(main._DESIGN_DELIVERIES.discard)
    await sending.wait()

    claimed = await data_store.notice(notice_id)
    assert claimed["claimed_at"] is not None, "the delivery never claimed it"

    await main._settle_design_deliveries()

    pending = await store.pending_notices(record["id"])
    assert [n["id"] for n in pending] == [notice_id]
    assert pending[0]["claimed_at"] is None, (
        "the notice is still claimed, so no later sweep will send it")


class _Registry:
    """Comms' mind registry over HTTP, as the console reads it."""

    def __init__(self, rows):
        self.rows = rows

    def install(self, monkeypatch):
        import httpx
        import route_access

        rows = self.rows

        def answer(request):
            assert request.url.path == "/broker/minds"
            return httpx.Response(200, json=rows)

        real = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(answer)
            return real(*args, **kwargs)

        monkeypatch.setattr(route_access.httpx, "AsyncClient", client)


async def test_a_second_users_design_session_names_them_not_daniel(store, gateway, monkeypatch):
    """Story 2.1.19: the person a design message speaks for is the owner of
    the mind the session is attached to."""
    from hive_data import core

    await core.upsert_mind(mind_id="arnold-id", name="arnold-t",
                           gateway_url="http://arnold:8421", model="a-model",
                           harness="claude", registered_at=1.0, last_seen=1.0,
                           owner="sloan")
    _Registry([{"id": "arnold-id", "name": "arnold-t", "owner": "sloan"}]).install(monkeypatch)
    record = await store.create_design_session(
        "Sloan's design", conversation_id="conv-2", mind_id="arnold-id")
    published = await store.publish_page(
        record["id"], "requirements", {"r1": {"order": 1, "text": "A thing"}},
        revision=record["revision"])
    await store.submit_page(
        published["id"], "requirements",
        items={"r1": {"feedback": "Why this?"}}, revision=published["revision"],
        submitted_by="sloan")
    notice_id = (await store.pending_notices(record["id"]))[0]["id"]

    await design_delivery.deliver_notice(notice_id, "sloan", "key-of-sloan")

    content = gateway.calls[1]["body"]["content"]
    assert "Sloan submitted feedback" in content
    assert "Sloan asks: Why this?" in content
    assert "Daniel" not in content
    # Requirement 39: it goes out as the person who submitted it, with their key.
    assert gateway.calls[1]["authorization"] == "Bearer key-of-sloan"


async def test_the_build_and_accept_messages_name_the_owner_and_nobody_else(store):
    record = await _session_with_feedback(store)

    build = design_delivery.compose_message(record, "requirements", "build", "aiden")
    accept = design_delivery.compose_message(record, "requirements", "accept", "aiden")
    nobody = design_delivery.compose_message(record, "requirements", "build", "")

    assert "Aiden pressed Build" in build
    assert "Aiden accepted the requirements page" in accept
    assert "The owner pressed Build" in nobody
    assert all("Daniel" not in text for text in (build, accept, nobody))
