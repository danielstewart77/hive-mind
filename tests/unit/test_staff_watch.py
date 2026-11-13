"""The chief of staff's watch: what one pass decides, from data alone.

The decision is a pure function of the live answer and the ledger's rows, so
these call it directly with both. The two that cross a boundary — reading the
live feed and dropping the wake into a conversation — are driven by replacing
`urllib.request.urlopen`, the transport, and never the code under test.
"""

from __future__ import annotations

import importlib.util
import io
import json
import threading
import urllib.error
from pathlib import Path

import pytest

pytest.importorskip("hive_data",
                    reason="hive_data is not installed in this environment")

TOOL = (Path(__file__).resolve().parents[2]
        / "tools" / "staff_watch" / "staff_watch.py")

STASH = "3d3ae243-989b-41f0-87bd-5a79ef42c70c"
OTHER = "14cb820b-4a42-4f04-a593-54f532fd1d2f"
NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def watch():
    spec = importlib.util.spec_from_file_location("staff_watch", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _task(task_id, status="in progress", sessions=("sess-1",),
          stopped_at=None, woken_at=None, name="Watched work"):
    return {"id": task_id, "name": name, "status": status,
            "sessions": list(sessions), "stopped_at": stopped_at,
            "woken_at": woken_at}


def _live(generating=(), idle=()):
    return {"sessions": [dict(e) for e in generating],
            "idle": [dict(e) for e in idle]}


def _entry(session_id, mind_id=OTHER, last_active=NOW):
    return {"session_id": session_id, "mind_id": mind_id,
            "last_active": last_active}


# ------------------------------------------------- recording what it sees


def test_a_watched_session_gone_from_the_live_feed_is_recorded_as_stopped(
        watch):
    """Comms deletes an ended session's row; the ledger's pair outlives it."""
    plan = watch.decide(_live(), [_task("t1")], STASH, NOW)

    assert plan["stopped"] == ["t1"]


def test_a_watched_session_present_but_idle_is_recorded_as_stopped(watch):
    plan = watch.decide(_live(idle=[_entry("sess-1")]), [_task("t1")],
                        STASH, NOW)

    assert plan["stopped"] == ["t1"]


def test_a_watched_session_still_producing_output_is_not_recorded_as_stopped(
        watch):
    plan = watch.decide(_live(generating=[_entry("sess-1")]), [_task("t1")],
                        STASH, NOW)

    assert plan["stopped"] == []
    assert plan["wake"] == []


def test_a_session_producing_again_clears_the_stop_on_its_row(watch):
    plan = watch.decide(_live(generating=[_entry("sess-1")]),
                        [_task("t1", stopped_at=NOW - 600)], STASH, NOW)

    assert plan["running"] == ["t1"]
    assert plan["pending"] == [], "a cleared stop is no longer owed a wake"


def test_a_task_whose_stop_is_already_recorded_is_not_recorded_twice(watch):
    plan = watch.decide(_live(), [_task("t1", stopped_at=NOW - 600)],
                        STASH, NOW)

    assert plan["stopped"] == []


def test_a_task_with_several_sessions_stops_only_when_all_of_them_have(watch):
    """A bake-off is several sessions under one task."""
    plan = watch.decide(
        _live(generating=[_entry("sess-2")], idle=[_entry("sess-1")]),
        [_task("t1", sessions=("sess-1", "sess-2"))], STASH, NOW)

    assert plan["stopped"] == []
    assert plan["running"] == ["t1"]


def test_a_task_with_no_session_paired_to_it_is_not_a_stopped_task(watch):
    """Nothing was ever running, so there is nothing to report."""
    plan = watch.decide(_live(), [_task("t1", sessions=())], STASH, NOW)

    assert plan["stopped"] == []
    assert plan["wake"] == []


def test_a_finished_task_is_left_alone(watch):
    plan = watch.decide(_live(), [_task("t1", status="done")], STASH, NOW)

    assert plan["stopped"] == []
    assert plan["wake"] == []


# ------------------------------------------------- waking, and not waking


def test_a_stop_recorded_this_pass_is_woken_in_the_same_pass(watch):
    plan = watch.decide(_live(), [_task("t1")], STASH, NOW)

    assert plan["stopped"] == ["t1"]
    assert [row["id"] for row in plan["wake"]] == ["t1"]


def test_a_task_on_hold_is_recorded_and_wakes_nobody(watch):
    """Overnight: knowable in the morning, with nothing awake to hear it."""
    plan = watch.decide(_live(), [_task("t1", status="on hold")], STASH, NOW)

    assert plan["stopped"] == ["t1"]
    assert plan["pending"] == ["t1"]
    assert plan["wake"] == []


def test_a_task_daniel_took_himself_is_recorded_and_wakes_nobody(watch):
    plan = watch.decide(_live(), [_task("t1", status="with Daniel")],
                        STASH, NOW)

    assert plan["stopped"] == ["t1"]
    assert plan["wake"] == []


def test_a_task_waiting_on_daniel_is_still_woken(watch):
    plan = watch.decide(_live(), [_task("t1", status="waiting on response")],
                        STASH, NOW)

    assert [row["id"] for row in plan["wake"]] == ["t1"]


def test_a_status_nobody_taught_the_watch_about_is_woken(watch):
    """Silence is the worse failure, so an unknown status is not quiet."""
    plan = watch.decide(_live(), [_task("t1", status="escalated")],
                        STASH, NOW)

    assert [row["id"] for row in plan["wake"]] == ["t1"]


def test_a_wake_already_delivered_is_not_delivered_again(watch):
    plan = watch.decide(_live(), [_task("t1", stopped_at=NOW - 600,
                                        woken_at=NOW - 300)], STASH, NOW)

    assert plan["pending"] == []
    assert plan["wake"] == []


def test_a_task_that_stops_again_after_a_wake_is_woken_again(watch):
    plan = watch.decide(
        _live(), [_task("t1", stopped_at=NOW - 60, woken_at=NOW - 300)],
        STASH, NOW)

    assert [row["id"] for row in plan["wake"]] == ["t1"]


def test_the_chief_of_staff_producing_output_is_never_interrupted(watch):
    """Its own conversation is in the same live answer it already reads."""
    plan = watch.decide(
        _live(generating=[_entry("stash-1", mind_id=STASH)]),
        [_task("t1")], STASH, NOW)

    assert plan["mid_turn"] is True
    assert plan["stopped"] == ["t1"], "it still records while it stays quiet"
    assert plan["wake"] == []
    assert plan["pending"] == ["t1"], "the wake waits on the row"


def test_another_mind_producing_output_does_not_hold_the_wake(watch):
    plan = watch.decide(
        _live(generating=[_entry("sess-9", mind_id=OTHER)]),
        [_task("t1")], STASH, NOW)

    assert plan["mid_turn"] is False
    assert [row["id"] for row in plan["wake"]] == ["t1"]


def test_wakes_held_while_mid_turn_are_delivered_together_when_idle(watch):
    """Several stops, one message: the next idle pass, not several pings."""
    tasks = [_task("t1", sessions=("a",), stopped_at=NOW - 300),
             _task("t2", sessions=("b",), stopped_at=NOW - 200),
             _task("t3", sessions=("c",), stopped_at=NOW - 100)]

    held = watch.decide(_live(generating=[_entry("s", mind_id=STASH)]),
                        tasks, STASH, NOW)
    delivered = watch.decide(_live(), tasks, STASH, NOW)

    assert held["wake"] == []
    assert [row["id"] for row in delivered["wake"]] == ["t1", "t2", "t3"], (
        "oldest stop first")
    assert watch.wake_message(delivered["wake"]).count("task t") == 3


def test_the_wake_names_every_task_and_sends_the_mind_to_its_skill(watch):
    rows = [_task("t1", name="Hex escalation path", sessions=("sess-1",))]

    text = watch.wake_message(rows)

    assert "Hex escalation path" in text
    assert "task t1" in text
    assert "sess-1" in text
    assert "tend-the-ledger" in text


# ---------------------------------------------------- where the wake goes


def test_the_wake_goes_to_the_minds_most_recently_active_conversation(watch):
    """Its latest one is the surface Daniel last spoke to it on."""
    live = _live(idle=[_entry("old", mind_id=STASH, last_active=NOW - 900),
                       _entry("new", mind_id=STASH, last_active=NOW - 10),
                       _entry("theirs", mind_id=OTHER, last_active=NOW)])

    assert watch.target_session(live, STASH) == "new"


def test_a_mind_with_no_conversation_is_not_given_one(watch):
    """A conversation the watch opened has no surface, so nobody reads it."""
    live = _live(idle=[_entry("theirs", mind_id=OTHER)])

    assert watch.target_session(live, STASH) is None


# ------------------------------------------------------------- the transport


def test_a_live_feed_that_refuses_is_reported_rather_than_read_as_empty(
        watch, monkeypatch):
    """An empty answer would read as every watched session having stopped."""
    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"nope"))

    monkeypatch.setenv("COMMS_URL", "http://comms.test")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "t")
    monkeypatch.setattr(watch.urllib.request, "urlopen", refuse)

    with pytest.raises(watch.Refused, match="401"):
        watch.read_live()


def test_a_wake_holds_its_stream_open_until_the_turn_ends(watch,
                                                          monkeypatch):
    """The gateway runs the turn only while something is reading the stream.

    Closing the socket on the status line cancels the turn before the mind
    has said anything, which is why `/take-a-task` sends detached.
    """
    reads = []
    closed = []

    class Stream:
        status = 200

        def readline(self):
            reads.append(1)
            # Three events, then the end of the turn.
            return b"data: {}\n" if len(reads) < 4 else b""

        def close(self):
            closed.append(1)

    monkeypatch.setenv("COMMS_URL", "http://comms.test")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "t")
    monkeypatch.setattr(watch.urllib.request, "urlopen",
                        lambda request, timeout=None: Stream())

    assert watch.send_wake("sess-1", "wake up") is True
    for thread in threading.enumerate():
        if thread.name.startswith("wake-"):
            thread.join(timeout=5)

    assert len(reads) == 4, "the stream is read to its end, not abandoned"
    assert closed == [1]


def test_a_wake_the_gateway_never_answers_is_not_recorded_as_delivered(
        watch, monkeypatch):
    def hang(request, timeout=None):
        raise TimeoutError("comms never answered")

    monkeypatch.setenv("COMMS_URL", "http://comms.test")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "t")
    monkeypatch.setattr(watch.urllib.request, "urlopen", hang)

    assert watch.send_wake("sess-1", "wake up") is False


def test_a_wake_comms_refuses_is_not_recorded_as_delivered(watch,
                                                           monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, io.BytesIO(b"gone"))

    monkeypatch.setenv("COMMS_URL", "http://comms.test")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "t")
    monkeypatch.setattr(watch.urllib.request, "urlopen", refuse)

    assert watch.send_wake("sess-1", "wake up") is False


def test_the_wake_posts_the_turn_as_the_message_route_expects(watch,
                                                             monkeypatch):
    sent = {}

    class Answer:
        status = 200

        def readline(self):
            return b""

        def close(self):
            pass

    def capture(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        sent["auth"] = request.headers.get("Authorization")
        return Answer()

    monkeypatch.setenv("COMMS_URL", "http://comms.test/")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "secret")
    monkeypatch.setattr(watch.urllib.request, "urlopen", capture)

    watch.send_wake("sess-1", "wake up")

    assert sent["url"] == "http://comms.test/sessions/sess-1/message"
    assert sent["body"] == {"content": "wake up"}
    assert sent["auth"] == "Bearer secret"


def test_a_pass_with_no_credential_refuses_rather_than_reading_nothing(
        watch, monkeypatch):
    monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_URL", "http://comms.test")

    with pytest.raises(watch.Refused, match="admin"):
        watch.read_live()


def test_a_chief_of_staff_silent_past_a_turns_life_no_longer_holds_the_wake(
        watch):
    """A generating flag left set by a reader that went away must not last.

    The gateway kills a turn at the mind's own timeout, so silence beyond
    that is a stalled turn and not a working one.
    """
    stalled = _entry("stash-1", mind_id=STASH)
    stalled["last_block_at"] = NOW - watch.STALLED - 1

    plan = watch.decide(_live(generating=[stalled]), [_task("t1")],
                        STASH, NOW)

    assert plan["mid_turn"] is False
    assert [row["id"] for row in plan["wake"]] == ["t1"]


def test_a_chief_of_staff_merely_quiet_still_holds_the_wake(watch):
    """Silence inside a long turn is not the turn ending."""
    working = _entry("stash-1", mind_id=STASH)
    working["last_block_at"] = NOW - watch.STALLED + 1

    plan = watch.decide(_live(generating=[working]), [_task("t1")],
                        STASH, NOW)

    assert plan["mid_turn"] is True
    assert plan["wake"] == []


def test_a_wake_stream_that_times_out_waiting_is_not_treated_as_the_end(
        watch):
    """A mind thinking for longer than the socket timeout is the normal case.

    The timeout given to `urlopen` governs every later read, so a drain that
    stopped on one would close the socket and cancel the turn — the very
    thing holding the stream open exists to prevent.
    """
    events = iter([TimeoutError("timed out"),
                   OSError("the read timed out"),
                   b"data: {}\n",
                   b""])
    closed = []

    class Stream:
        def readline(self):
            value = next(events)
            if isinstance(value, Exception):
                raise value
            return value

        def close(self):
            closed.append(1)

    watch._drain(Stream(), "sess-1")

    assert next(events, "drained") == "drained", (
        "every event was read, two timeouts included")
    assert closed == [1]


def test_a_wake_stream_that_genuinely_breaks_is_let_go(watch):
    class Stream:
        def readline(self):
            raise OSError("connection reset by peer")

        def close(self):
            pass

    watch._drain(Stream(), "sess-1")  # reported, never raised
