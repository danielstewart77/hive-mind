"""The gateway wakes the conversation that briefed a session when it stops.

Driven against the real session manager and a real Postgres of its own. Only
the process that stops being ours is faked: the mind's HTTP endpoint, which
streams whatever events a test scripts, and can be held open mid-turn.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

import hive_data  # noqa: E402
from hive_data import core, watches  # noqa: E402

from comms import sessions as sessions_module  # noqa: E402
from comms_broker_pg import broker_store, fetch, put_mind  # noqa: E402,F401

MIND = "wake-mind-1"
OTHER = "wake-mind-2"


class FakeMind:
    """A mind's HTTP endpoint: records each turn it is handed and answers it."""

    def __init__(self) -> None:
        self.received: list[tuple[str, str]] = []
        #: session id -> events to stream for its next turn.
        self.script: dict[str, list[dict]] = {}
        #: session id -> an event the turn waits on before it answers.
        self.hold: dict[str, asyncio.Event] = {}
        self.status: dict[str, int] = {}
        self.entered: dict[str, asyncio.Event] = {}
        self.app = web.Application()
        self.app.router.add_post("/sessions/{sid}/message", self._message)
        self.server = TestServer(self.app)

    async def _message(self, request: web.Request) -> web.StreamResponse:
        sid = request.match_info["sid"]
        body = await request.json()
        self.received.append((sid, body["content"]))
        self.entered.setdefault(sid, asyncio.Event()).set()
        if sid in self.status:
            return web.json_response({"error": "broken"}, status=self.status.pop(sid))
        gate = self.hold.get(sid)
        if gate is not None:
            await gate.wait()
        events = self.script.pop(sid, None) or [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": f"answer to {len(self.received)}"}]}},
            {"type": "result", "result": "ok"},
        ]
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for event in events:
            await response.write(f"data: {__import__('json').dumps(event)}\n\n".encode())
        await response.write_eof()
        return response


@pytest.fixture
async def mind():
    fake = FakeMind()
    await fake.server.start_server()
    yield fake
    await fake.server.close()


@pytest.fixture
async def gateway(broker_store, mind, monkeypatch):
    manager = sessions_module.SessionManager()
    # The idle moment a wake waits for is the turn lock itself; the pane
    # recheck is not under test here.
    monkeypatch.setattr(manager, "TERMINAL_RECHECK_SECONDS", 0.05)
    url = str(mind.server.make_url("")).rstrip("/")
    for mind_id in (MIND, OTHER):
        await put_mind(mind_id, mind_id, owner="daniel", gateway_url=url)
    manager.test_mind = mind
    yield manager
    for task in list(manager._wake_tasks):
        task.cancel()
    await asyncio.gather(*manager._wake_tasks, return_exceptions=True)


async def _session(manager, mind_id=MIND, mind_server=None) -> dict:
    session_id = f"s-{uuid.uuid4().hex[:8]}"
    now = time.time()
    await core.create_session(
        id=session_id, surface="broker", surface_id="x", mind_id=mind_id,
        created_at=now, last_active=now, status="idle",
        conversation_id=str(uuid.uuid4()), model="m", opening_line="opened")
    url = str((manager.test_mind or mind_server).server.make_url("")).rstrip("/")
    manager._procs[session_id] = {"_mind_url": url}
    manager._mind_ids[session_id] = mind_id
    return await manager._get_row(session_id)


async def _settle(manager) -> None:
    """Let every wake delivery in flight finish."""
    for _ in range(50):
        await asyncio.sleep(0.02)
        pending = [t for t in manager._wake_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def _turn(manager, session_id: str, text: str = "go", **kw) -> list[dict]:
    return [event async for event in manager.send_message(session_id, text, **kw)]


def _wakes_to(mind: FakeMind, sender: dict) -> list[str]:
    return [text for sid, text in mind.received if sid == sender["id"] and "[wake]" in text]


async def _watch(manager, worker, sender, task="the task"):
    watch = await manager.ask_watch(worker, sender, task, armed_after=0)
    return watch["capability"]


# --- protected behaviour 1 ---------------------------------------------------

async def test_a_stop_of_the_briefed_session_wakes_the_sender_and_no_other_conversation(
        gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    bystander = await _session(gateway)
    await _watch(gateway, worker, sender, "build the thing")

    await _turn(gateway, worker["id"])
    await _settle(gateway)

    woken = _wakes_to(mind, sender)
    assert len(woken) == 1
    assert worker["id"] in woken[0] and "build the thing" in woken[0]
    assert "ended its turn" in woken[0]
    assert _wakes_to(mind, bystander) == []
    assert [sid for sid, _ in mind.received if sid == worker["id"]] == [worker["id"]]


async def test_a_turn_that_dies_wakes_the_sender_and_says_it_died(gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)
    mind.script[worker["id"]] = [{"type": "result", "is_error": True, "result": "boom"}]

    await _turn(gateway, worker["id"])
    await _settle(gateway)

    woken = _wakes_to(mind, sender)
    assert len(woken) == 1 and "died" in woken[0]


async def test_a_brief_accepted_but_never_started_wakes_its_sender_that_it_died(
        gateway, mind):
    """Blocker one of the first review: an accepted brief whose spawn fails
    used to leave the watch silent for ever."""
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await gateway.ask_watch(worker, sender, "the brief")
    gateway._procs.pop(worker["id"])
    gateway._procs[worker["id"]] = {}          # no container to reach

    with pytest.raises(ValueError):
        await _turn(gateway, worker["id"], brief_from=sender["id"])
    await _settle(gateway)

    woken = _wakes_to(mind, sender)
    assert len(woken) == 1 and "died" in woken[0]


async def test_a_turn_already_running_does_not_wake_a_watch_whose_brief_has_not_started(
        gateway, mind):
    """Blocker two: the stop of a turn that was already running when the ask
    arrived must not wake a watch whose own brief had not yet begun."""
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    mind.hold[worker["id"]] = asyncio.Event()
    running = asyncio.create_task(_turn(gateway, worker["id"], "someone else's turn"))
    await mind.entered.setdefault(worker["id"], asyncio.Event()).wait()

    await gateway.ask_watch(worker, sender, "my brief")      # asked, brief not started
    mind.hold[worker["id"]].set()
    await running
    await _settle(gateway)

    assert _wakes_to(mind, sender) == []
    mind.hold.pop(worker["id"])
    await _turn(gateway, worker["id"], "my brief", brief_from=sender["id"])
    await _settle(gateway)
    assert len(_wakes_to(mind, sender)) == 1


async def test_a_brief_without_asking_wakes_nobody(gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)

    await _turn(gateway, worker["id"])
    await _settle(gateway)

    assert _wakes_to(mind, sender) == []


# --- protected behaviour 2 ---------------------------------------------------

async def test_a_sender_mid_turn_gets_its_waiting_wakes_as_one_message_after_the_turn(
        gateway, mind):
    sender = await _session(gateway)
    workers = [await _session(gateway, OTHER) for _ in range(3)]
    for index, worker in enumerate(workers):
        await _watch(gateway, worker, sender, f"task {index}")
    mind.hold[sender["id"]] = asyncio.Event()
    busy = asyncio.create_task(_turn(gateway, sender["id"], "daniel is talking"))
    await mind.entered.setdefault(sender["id"], asyncio.Event()).wait()

    for worker in workers:
        await _turn(gateway, worker["id"])
    await _settle(gateway)
    during = _wakes_to(mind, sender)
    mind.hold.pop(sender["id"]).set()
    await busy
    await _settle(gateway)

    after = _wakes_to(mind, sender)
    assert during == []
    assert len(after) == 1
    assert all(w["id"] in after[0] for w in workers)
    assert all(f"task {i}" in after[0] for i in range(3))


# --- protected behaviour 3 ---------------------------------------------------

async def test_the_same_stop_never_wakes_a_sender_twice_even_when_the_slow_check_runs(
        gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)

    await _turn(gateway, worker["id"])
    await _settle(gateway)
    for _ in range(3):
        await gateway.wake_sweep()
        await _settle(gateway)

    assert len(_wakes_to(mind, sender)) == 1


async def test_a_wake_that_failed_to_deliver_arrives_at_the_next_check_and_only_once(
        gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)
    mind.status[sender["id"]] = 500                 # the sender's mind is down

    await _turn(gateway, worker["id"])
    await _settle(gateway)
    first = _wakes_to(mind, sender)
    await gateway.wake_sweep()
    await _settle(gateway)
    await gateway.wake_sweep()
    await _settle(gateway)

    assert len(first) == 1                           # the failed attempt
    assert len(_wakes_to(mind, sender)) == 2         # plus exactly one retry
    states = await fetch("SELECT state FROM core.session_wakes")
    assert [r["state"] for r in states] == ["delivered"]


# --- protected behaviour 4 ---------------------------------------------------

async def test_once_the_sender_marks_the_task_finished_later_stops_wake_nobody(
        gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    capability = await _watch(gateway, worker, sender)

    for _ in range(3):
        await _turn(gateway, worker["id"])
        await _settle(gateway)
    before = len(_wakes_to(mind, sender))
    assert await gateway.finish_watch(worker["id"], sender["id"], "not the capability") == "refused"
    assert await gateway.finish_watch(worker["id"], sender["id"], capability) == "finished"
    assert await gateway.finish_watch(worker["id"], sender["id"], capability) == "absent"
    await _turn(gateway, worker["id"])
    await _settle(gateway)

    assert before == 3
    assert len(_wakes_to(mind, sender)) == 3


# --- protected behaviour 5 ---------------------------------------------------

async def test_the_end_of_a_turn_that_was_itself_a_wake_wakes_nobody(gateway, mind):
    """A watches B and B watches A. B stopping wakes A; the wake is a turn of
    A, and A stopping must not wake B, or the two ping for ever."""
    a = await _session(gateway, MIND)
    b = await _session(gateway, OTHER)
    await _watch(gateway, b, a, "b's work")
    await _watch(gateway, a, b, "a's work")

    await _turn(gateway, b["id"], "a real brief")
    await _settle(gateway)
    for _ in range(2):
        await gateway.wake_sweep()
        await _settle(gateway)

    assert len(_wakes_to(mind, a)) == 1
    assert _wakes_to(mind, b) == []
    assert len([1 for sid, _ in mind.received if sid == b["id"]]) == 1


# --- who is never woken ------------------------------------------------------

async def test_ending_the_sender_drops_the_wake_and_starts_nothing(gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)
    mind.hold[worker["id"]] = asyncio.Event()
    running = asyncio.create_task(_turn(gateway, worker["id"]))
    await mind.entered.setdefault(worker["id"], asyncio.Event()).wait()

    await core.delete_session(sender["id"])
    mind.hold.pop(worker["id"]).set()
    await running
    await _settle(gateway)
    await gateway.wake_sweep()
    await _settle(gateway)

    assert [t for _, t in mind.received if "[wake]" in t] == []


async def test_a_sender_that_rotated_still_receives_the_wake_on_the_same_row(gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)
    await core.update_session(sender["id"], conversation_id=str(uuid.uuid4()))

    await _turn(gateway, worker["id"])
    await _settle(gateway)

    assert len(_wakes_to(mind, sender)) == 1


async def test_a_gateway_that_died_mid_brief_reports_that_turn_as_died_to_the_watcher(
        gateway, mind):
    """A turn cut off by a restart leaves a user row and nothing after it. The
    slow check reads that as a turn that died, once."""
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)
    await core.record_turn(session_id=worker["id"], role="user", content="x",
                           created_at=time.time() - 100)
    gateway._started_at = time.time()                # this process began after it

    await gateway.wake_sweep()
    await _settle(gateway)
    await gateway.wake_sweep()
    await _settle(gateway)

    woken = _wakes_to(mind, sender)
    assert len(woken) == 1 and "died" in woken[0]


async def test_a_terminal_turn_wakes_once_for_the_assistant_post_and_a_death_says_so(
        gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)
    await core.bind_surface(client_type="terminal", client_ref="tile-1",
                            session_id=worker["id"])

    await gateway.record_turn("terminal", "tile-1", "user", "typed")
    await _settle(gateway)
    none_yet = _wakes_to(mind, sender)
    await gateway.record_turn("terminal", "tile-1", "assistant", "done it")
    await _settle(gateway)
    one = _wakes_to(mind, sender)
    await gateway.record_turn("terminal", "tile-1", "user", "again")
    await gateway.record_turn("terminal", "tile-1", "", "", died=True)
    await _settle(gateway)

    assert none_yet == []
    assert len(one) == 1 and "ended its turn" in one[0]
    both = _wakes_to(mind, sender)
    assert len(both) == 2 and "died" in both[1]


# --- the slow check never announces a stop a second time --------------------

async def test_the_slow_check_does_not_wake_again_for_stops_whose_records_were_pruned(
        gateway, mind):
    """Only the newest few stops are kept. The check once read every older
    reply as an unannounced stop and woke the sender again for each."""
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)
    for _ in range(watches.STOPS_KEPT + 2):
        await _turn(gateway, worker["id"])
        await _settle(gateway)
    delivered = len(_wakes_to(mind, sender))

    await gateway.wake_sweep()
    await _settle(gateway)

    assert delivered == watches.STOPS_KEPT + 2
    assert len(_wakes_to(mind, sender)) == delivered


async def test_a_turn_that_died_after_saying_something_wakes_the_sender_once(
        gateway, mind):
    worker = await _session(gateway, OTHER)
    sender = await _session(gateway)
    await _watch(gateway, worker, sender)
    mind.script[worker["id"]] = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "half an answer"}]}},
        {"type": "result", "is_error": True, "result": "boom"}]

    await _turn(gateway, worker["id"])
    await _settle(gateway)
    await gateway.wake_sweep()
    await _settle(gateway)

    woken = _wakes_to(mind, sender)
    assert len(woken) == 1 and "died" in woken[0]


async def test_a_wake_turn_that_ends_in_a_terminal_pane_wakes_nobody(gateway, mind):
    """A wake delivered to a conversation held in a browser terminal ends
    through the Stop hook's report, not through the code that sent it."""
    watcher = await _session(gateway, MIND)
    pane = await _session(gateway, OTHER)
    worker = await _session(gateway, OTHER)
    await _watch(gateway, pane, watcher, "the pane's work")
    await _watch(gateway, worker, pane, "the worker")
    await core.bind_surface(client_type="terminal", client_ref="tile-2",
                            session_id=pane["id"])
    await watches.record_stop(worker["id"], "t1", "ended", turn_id=1)
    claimed = await watches.claim_pending(pane["id"])
    await watches.mark_delivered([row["id"] for row in claimed])

    await gateway.record_turn("terminal", "tile-2", "user", "the wake text")
    await gateway.record_turn("terminal", "tile-2", "assistant", "read it")
    await _settle(gateway)

    assert _wakes_to(mind, watcher) == []
