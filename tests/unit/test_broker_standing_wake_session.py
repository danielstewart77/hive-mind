"""A mind waking itself holds one conversation; a dispatch between two minds
holds one per message.

The sentinel wakes Hex every fifteen minutes. Each wake used to mint a session
keyed on that message's conversation id and kill it on the way out, so 21,114
rows accumulated under one mind and the operator's session list was unreadable.
These guard the two halves of the rule that replaced it.
"""

from __future__ import annotations

import asyncio
import os
import sys

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

from comms import broker  # noqa: E402

HEX = "mind-hex-for-tests"
SKIPPY = "mind-skippy-for-tests"


class FakeSessionManager:
    """The transport: it hands back session rows and records the calls that
    changed them. Nothing about who gets reused is decided here."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.killed: list[str] = []
        self.named: list[tuple[str, str]] = []
        self.sent_to: list[str] = []
        self._active: dict[tuple[str, str], dict] = {}

    async def create_session(self, *, owner_type=None, owner_ref=None,
                             client_ref=None, mind_id=None, model=None,
                             **kwargs) -> dict:
        row = {"id": f"session-{len(self.created) + 1}", "client_ref": client_ref,
               "owner_ref": owner_ref, "mind_id": mind_id}
        self.created.append(row)
        self._active[(owner_type, client_ref)] = row
        return row

    async def get_active_session(self, client_type: str, client_ref: str):
        return self._active.get((client_type, client_ref))

    async def set_session_name(self, session_id: str, *, name=None, color=None):
        self.named.append((session_id, name))
        return {"id": session_id, "name": name}

    async def kill_session(self, session_id: str) -> None:
        self.killed.append(session_id)
        for key, row in list(self._active.items()):
            if row["id"] == session_id:
                del self._active[key]

    async def send_message(self, session_id: str, prompt: str):
        self.sent_to.append(session_id)
        yield {"type": "result", "result": "worked it"}


def _stub_message_store(monkeypatch) -> None:
    """Everything in broker.py that reaches the message table. The behaviour
    under test is which session a wake lands in, not what the table records."""
    async def noop(*args, **kwargs):
        return None

    async def mind_row(mind_id):
        return {"name": mind_id, "model": "model-for-tests"}

    async def next_number(*args, **kwargs):
        return 2

    monkeypatch.setattr(broker, "update_message_status", noop)
    monkeypatch.setattr(broker, "insert_message", noop)
    monkeypatch.setattr(broker, "get_next_message_number", next_number)
    monkeypatch.setattr(broker, "get_mind_by_id", mind_row)


def _wake(session_mgr, *, from_mind, to_mind, conversation_id, message_id):
    asyncio.run(broker.wakeup_and_collect(
        session_mgr=session_mgr,
        message_id=message_id,
        conversation_id=conversation_id,
        from_mind=from_mind,
        to_mind=to_mind,
        content="a sentinel briefing",
        rolling_summary="",
        message_number=1,
        metadata={"request_type": "sentinel_alert"},
    ))


def test_a_mind_waking_itself_twice_lands_both_wakes_in_one_session(monkeypatch):
    mgr = FakeSessionManager()
    _stub_message_store(monkeypatch)

    _wake(mgr, from_mind=HEX, to_mind=HEX,
          conversation_id="conversation-one", message_id="message-one")
    _wake(mgr, from_mind=HEX, to_mind=HEX,
          conversation_id="conversation-two", message_id="message-two")

    assert len(mgr.created) == 1, "the second wake minted a second session"
    assert mgr.sent_to == [mgr.created[0]["id"], mgr.created[0]["id"]]


def test_the_standing_wake_session_is_not_killed_when_the_wake_completes(monkeypatch):
    mgr = FakeSessionManager()
    _stub_message_store(monkeypatch)

    _wake(mgr, from_mind=HEX, to_mind=HEX,
          conversation_id="conversation-one", message_id="message-one")

    assert mgr.killed == []


def test_the_standing_wake_session_is_keyed_on_the_mind_not_the_message(monkeypatch):
    mgr = FakeSessionManager()
    _stub_message_store(monkeypatch)

    _wake(mgr, from_mind=HEX, to_mind=HEX,
          conversation_id="conversation-one", message_id="message-one")

    assert mgr.created[0]["client_ref"] == f"broker-self-{HEX}"


def test_the_standing_wake_session_is_named_after_the_mind(monkeypatch):
    mgr = FakeSessionManager()
    _stub_message_store(monkeypatch)

    _wake(mgr, from_mind=HEX, to_mind=HEX,
          conversation_id="conversation-one", message_id="message-one")

    assert mgr.named == [(mgr.created[0]["id"], f"{HEX}-wakes")]


def test_a_dispatch_between_two_minds_still_gets_its_own_session_per_message(monkeypatch):
    """A task has a beginning and an end and a caller waiting on its result,
    so it keeps the session per message and the kill that ends it."""
    mgr = FakeSessionManager()
    _stub_message_store(monkeypatch)

    _wake(mgr, from_mind=SKIPPY, to_mind=HEX,
          conversation_id="conversation-one", message_id="message-one")
    _wake(mgr, from_mind=SKIPPY, to_mind=HEX,
          conversation_id="conversation-two", message_id="message-two")

    assert len(mgr.created) == 2
    assert mgr.killed == [mgr.created[0]["id"], mgr.created[1]["id"]]
