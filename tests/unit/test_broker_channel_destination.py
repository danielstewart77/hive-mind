"""An escalation whose answer Daniel has to read goes to his channel.

Hex finds something on the sentinel that needs a host operated on and hands
it to Skippy. Before this, Skippy woke in a throwaway broker session, answered
Hex, and Hex pinged Daniel's Telegram with a line he could not reply to —
three places, none of them a conversation. Now the dispatch names a Discord
channel, the wake lands in that channel's own session, and the answer is
posted there, so Daniel reads it where replying carries on talking.

These guard the four decisions that makes: which session, whether it is
killed, who the answer is addressed to, and what a failed post costs.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

from comms import broker, discord_out  # noqa: E402

HEX = "mind-hex-for-tests"
SKIPPY = "mind-skippy-for-tests"
SECURITY = "1556059743968039065"


class FakeSessionManager:
    """The transport: it hands back session rows and records the calls that
    changed them. Nothing about who gets reused is decided here."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.killed: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self._active: dict[tuple[str, str], dict] = {}

    def seed_active(self, client_type: str, client_ref: str, session_id: str) -> dict:
        row = {"id": session_id, "client_ref": client_ref}
        self._active[(client_type, client_ref)] = row
        return row

    async def create_session(self, *, surface=None, surface_id=None,
                             client_ref=None, mind_id=None, model=None,
                             **kwargs) -> dict:
        row = {"id": f"session-{len(self.created) + 1}", "surface": surface,
               "client_ref": client_ref, "surface_id": surface_id,
               "mind_id": mind_id}
        self.created.append(row)
        self._active[(surface, client_ref)] = row
        return row

    async def get_active_session(self, client_type: str, client_ref: str):
        return self._active.get((client_type, client_ref))

    async def set_session_name(self, session_id: str, *, name=None, color=None):
        return {"id": session_id, "name": name}

    async def kill_session(self, session_id: str) -> None:
        self.killed.append(session_id)
        for key, row in list(self._active.items()):
            if row["id"] == session_id:
                del self._active[key]

    async def send_message(self, session_id: str, prompt: str):
        self.sent.append((session_id, prompt))
        yield {"type": "result", "result": "Restarted ollama, the stalls stopped."}


class Store:
    """What the message table was told. The behaviour under test is which
    session a wake lands in and where its answer goes, not the schema."""

    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []
        self.replies: list[str] = []


@pytest.fixture
def store(monkeypatch) -> Store:
    recorded = Store()

    async def update_status(message_id, status, **kwargs):
        recorded.statuses.append((message_id, status))

    async def insert(*, content="", **kwargs):
        recorded.replies.append(content)

    async def mind_row(mind_id):
        return {"name": mind_id, "model": "model-for-tests"}

    async def claim(mind_id):
        return "839112201109110825"

    monkeypatch.setattr(broker, "update_message_status", update_status)
    monkeypatch.setattr(broker, "insert_message", insert)
    monkeypatch.setattr(broker, "get_mind_by_id", mind_row)
    monkeypatch.setattr(broker, "_discord_claim", claim)
    return recorded


@pytest.fixture
def posted(monkeypatch) -> list[tuple[str, str]]:
    """The egress to Discord — where the process stops being ours."""
    sent: list[tuple[str, str]] = []

    async def post(channel_id, text, *, token=None):
        sent.append((channel_id, text))
        return 1

    monkeypatch.setattr(broker.discord_out, "post", post)
    return sent


def _wake(session_mgr, *, metadata, from_mind=HEX, to_mind=SKIPPY,
          message_id="message-one"):
    asyncio.run(broker.wakeup_and_collect(
        session_mgr=session_mgr,
        message_id=message_id,
        conversation_id="conversation-one",
        from_mind=from_mind,
        to_mind=to_mind,
        content="A beacon from 192.168.5.65 to 198.147.x.x every 60s.",
        rolling_summary="",
        message_number=1,
        metadata=metadata,
    ))


# --------------------------------------------------------------- the session


def test_a_dispatch_naming_a_channel_lands_in_that_channels_session(
        store, posted):
    mgr = FakeSessionManager()

    _wake(mgr, metadata={"discord_channel": SECURITY})

    assert mgr.created[0]["client_ref"] == SECURITY
    assert mgr.created[0]["surface"] == f"discord:{SKIPPY}"
    assert mgr.sent[0][0] == mgr.created[0]["id"]


def test_the_channels_existing_session_is_reused_rather_than_a_second_minted(
        store, posted):
    """Daniel's thread in that channel is the conversation; a parallel
    session for the same channel is one he never sees."""
    mgr = FakeSessionManager()
    thread = mgr.seed_active(f"discord:{SKIPPY}", SECURITY, "daniels-thread")

    _wake(mgr, metadata={"discord_channel": SECURITY})

    assert mgr.created == []
    assert mgr.sent[0][0] == thread["id"]


def test_the_channels_session_is_keyed_on_the_person_not_the_channel(
        store, posted):
    """`surface_id` is who the bot answers. The bot keys its own sessions
    that way, and a different value mints a second one for the channel."""
    mgr = FakeSessionManager()

    _wake(mgr, metadata={"discord_channel": SECURITY})

    assert mgr.created[0]["surface_id"] == "839112201109110825"


def test_the_channels_session_is_not_killed_when_the_exchange_ends(
        store, posted):
    mgr = FakeSessionManager()

    _wake(mgr, metadata={"discord_channel": SECURITY})

    assert mgr.killed == []


def test_a_channel_name_instead_of_an_id_is_not_a_destination(store, posted):
    """A name 404s on the first post. Dropped, so the dispatch behaves as a
    plain one rather than as one whose answer silently goes nowhere."""
    mgr = FakeSessionManager()

    _wake(mgr, metadata={"discord_channel": "skippy-security"})

    assert mgr.created[0]["client_ref"] == f"broker-conversation-one-{SKIPPY}"
    assert mgr.killed == [mgr.created[0]["id"]]
    assert posted == []


def test_a_self_wake_that_names_a_channel_still_goes_to_the_channel(
        store, posted):
    """An explicit destination beats the standing wake: a mind that named a
    channel has said where it wants its answer read."""
    mgr = FakeSessionManager()

    _wake(mgr, metadata={"discord_channel": SECURITY},
          from_mind=SKIPPY, to_mind=SKIPPY)

    assert mgr.created[0]["client_ref"] == SECURITY
    assert mgr.killed == []


# ---------------------------------------------------------------- the answer


def test_the_answer_is_posted_into_the_named_channel(store, posted):
    mgr = FakeSessionManager()

    _wake(mgr, metadata={"discord_channel": SECURITY})

    assert posted == [(SECURITY, "Restarted ollama, the stalls stopped.")]


def test_a_dispatch_with_no_channel_posts_nothing_to_discord(store, posted):
    mgr = FakeSessionManager()

    _wake(mgr, metadata={"request_type": "security_triage"})

    assert posted == []


def test_a_refused_post_still_leaves_the_caller_its_answer(store, monkeypatch):
    """The mind cannot be asked to do the work twice, so a Discord outage
    costs the copy and not the dispatch."""
    mgr = FakeSessionManager()

    async def refuse(channel_id, text, *, token=None):
        raise discord_out.DeliveryFailed("Discord returned 403")

    monkeypatch.setattr(broker.discord_out, "post", refuse)

    _wake(mgr, metadata={"discord_channel": SECURITY})

    assert store.replies == ["Restarted ollama, the stalls stopped."]
    assert ("message-one", "completed") in store.statuses
    assert ("message-one", "failed") not in store.statuses


# ---------------------------------------------------------------- the prompt


def test_the_wake_actually_sent_into_the_channel_is_addressed_to_daniel(
        store, posted):
    """The broker has to hand the destination to the prompt, not merely know
    it. Woken with the plain prompt, the mind answers Hex — and Daniel is
    handed a report written to a machine, in his own channel."""
    mgr = FakeSessionManager()

    _wake(mgr, metadata={"discord_channel": SECURITY})

    prompt = mgr.sent[0][1]
    assert SECURITY in prompt
    assert "Daniel" in prompt


def test_the_prompt_for_a_channel_dispatch_addresses_daniel():
    """A mind not told who is reading writes a report to another machine."""
    prompt = broker.build_wakeup_prompt(
        "hex", SKIPPY, "conversation-one", "a beacon", "", 1,
        discord_channel=SECURITY,
    )

    assert SECURITY in prompt
    assert "Daniel" in prompt


def test_the_prompt_for_a_plain_dispatch_still_addresses_the_caller():
    prompt = broker.build_wakeup_prompt(
        "hex", SKIPPY, "conversation-one", "a beacon", "", 1,
    )

    assert "Daniel" not in prompt
    assert "delivered back to hex" in prompt


# ----------------------------------------------------------- the egress


def test_an_answer_over_discords_limit_goes_out_in_order_not_truncated():
    """Discord refuses a body over 2000 characters outright, and a briefing
    that stops mid-sentence reads as the whole answer."""
    answer = "a" * 2000 + "b" * 500

    chunks = discord_out.split_message(answer)

    assert len(chunks) == 2
    assert chunks[0] == "a" * 2000
    assert chunks[1] == "b" * 500
    assert "".join(chunks) == answer


def test_an_empty_answer_is_not_posted_as_an_empty_message():
    assert discord_out.split_message("") == []


def test_posting_without_a_token_refuses_rather_than_calling_discord():
    """An unset token is a configuration fault the broker logs and survives.
    Reaching the API with an empty bearer would be a 401 per escalation."""
    with pytest.raises(discord_out.DeliveryFailed):
        asyncio.run(discord_out.post(SECURITY, "anything", token=""))
