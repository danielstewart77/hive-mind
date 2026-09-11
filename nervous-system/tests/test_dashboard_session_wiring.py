"""The live feed against the real session manager, not the feed alone.

`test_dashboard_feed.py` proves the feed's own rules. What matters here is
that it is actually fed: that the one hook point catches both publishers,
that a tool result published to observers does not reach the dashboard, and
that a conversation's context figures come back from the row the mind wrote
them to — with nothing invented for the minds that never reported.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from comms.sessions import SessionManager


def _run(coro):
    return asyncio.run(coro)


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    return mgr


async def _session_row(
    mgr: SessionManager, session_id: str, owner_type: str = "terminal",
    mind_id: str = "skippy", model: str = "claude-opus-5",
) -> None:
    await mgr._db.execute(
        "INSERT INTO sessions (id, claude_sid, owner_type, owner_ref, model, "
        "created_at, last_active, status, mind_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, f"conv-{session_id}", owner_type, "ref-1", model,
         1_700_000_000.0, 1_700_000_000.0, "running", mind_id),
    )
    await mgr._db.commit()


TEXT_EVENT = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]},
}
TOOL_RESULT_EVENT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [{"type": "tool_result", "content": "BEGIN RSA PRIVATE KEY"}],
    },
}


# --- the hook actually fires -----------------------------------------------


def test_publishing_an_assistant_event_puts_its_text_on_the_feed():
    """The one hook point is `_publish_session_event`, which both the chat
    path and the terminal tailer go through. If it stopped firing, every
    column on the dashboard would be permanently empty while the hive
    worked normally — a failure with no other symptom."""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            return mgr.dashboard.since("s1", 0)

    assert [block["text"] for block in _run(scenario())] == ["working"]


def test_a_tool_result_published_to_observers_never_reaches_the_dashboard():
    """Observers get every harness event unfiltered — that is what the tile
    speaker consumes. The dashboard answers to any console account, so the
    filtering has to happen before the bytes leave, not in the browser."""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", TOOL_RESULT_EVENT)
            return mgr.dashboard.since("s1", 0)

    assert _run(scenario()) == []


def test_a_result_event_ends_the_conversations_turn():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", {"type": "user", "message": {}})
            before = mgr.dashboard.state("s1")["generating"]
            await mgr._publish_session_event("s1", {"type": "result", "subtype": "success"})
            return before, mgr.dashboard.state("s1")["generating"]

    before, after = _run(scenario())
    assert before is True
    assert after is False


def test_closing_a_session_drops_it_from_the_feed():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            await mgr.kill_session("s1")
            return mgr.dashboard.since("s1", 0)

    assert _run(scenario()) == []


# --- context figures --------------------------------------------------------


def test_a_reported_context_comes_back_on_the_live_listing():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            await mgr.report_context(
                "s1", tokens=150_000, threshold=300_000, window=1_000_000, now=50.0
            )
            return await mgr.live_dashboard(now=50.0)

    context = _run(scenario())["sessions"][0]["context"]
    assert context["tokens"] == 150_000
    assert context["threshold"] == 300_000
    assert context["window"] == 1_000_000


def test_a_conversation_whose_mind_never_reported_has_no_numbers_at_all():
    """Requirement 7's absent case. A zero here draws a conversation sitting
    at 138k as having its whole window free, which is the one wrong answer
    that looks like good news."""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            return await mgr.live_dashboard(now=50.0)

    context = _run(scenario())["sessions"][0]["context"]
    assert context["tokens"] is None
    assert context["threshold"] is None
    assert context["age_seconds"] is None


def test_the_context_figure_reports_its_age():
    """It is measured once per completed turn, so it is never live. The age
    is what stops it being read as though it were."""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            await mgr.report_context("s1", tokens=10, now=100.0)
            return await mgr.live_dashboard(now=190.0)

    assert _run(scenario())["sessions"][0]["context"]["age_seconds"] == 90.0


def test_reporting_only_a_token_count_leaves_the_window_alone():
    """Fields arrive from different reporters at different times. A partial
    report that blanked the rest would erase the denominator every turn."""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            await mgr.report_context("s1", window=200_000, now=10.0)
            await mgr.report_context("s1", tokens=99, now=20.0)
            return await mgr.live_dashboard(now=20.0)

    context = _run(scenario())["sessions"][0]["context"]
    assert context["window"] == 200_000
    assert context["tokens"] == 99


def test_reporting_against_an_unknown_session_is_refused_not_silently_dropped():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            return await mgr.report_context("nope", tokens=1)

    assert _run(scenario())["ok"] is False


def test_a_mind_reporting_its_own_threshold_is_taken_at_its_word():
    """Requirement 10. The threshold is not a function of the model name —
    the same model caps in two different places depending on whether the
    conversation was started with the long-context pin — so a console that
    derived it from a lookup table would be wrong for half the hive. A
    figure no table could contain proves the number travelled."""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1", model="house-model-7")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            await mgr.report_context("s1", tokens=61_500, threshold=137_000, now=1.0)
            return await mgr.live_dashboard(now=1.0)

    assert _run(scenario())["sessions"][0]["context"]["threshold"] == 137_000


def test_two_conversations_on_one_mind_each_carry_their_own_count():
    """Requirement 7 is per conversation. One mind can hold several at once,
    and a count stored per mind would show both at the larger figure."""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await _session_row(mgr, "s2")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            await mgr._publish_session_event("s2", TEXT_EVENT)
            await mgr.report_context("s1", tokens=10_000, now=1.0)
            await mgr.report_context("s2", tokens=280_000, now=1.0)
            return await mgr.live_dashboard(now=1.0)

    counts = {
        row["session_id"]: row["context"]["tokens"]
        for row in _run(scenario())["sessions"]
    }
    assert counts == {"s1": 10_000, "s2": 280_000}


def test_a_session_deleted_mid_turn_leaves_the_listing_rather_than_erroring():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            await _session_row(mgr, "s1")
            await mgr._publish_session_event("s1", TEXT_EVENT)
            await mgr._db.execute("DELETE FROM sessions WHERE id = 's1'")
            await mgr._db.commit()
            return await mgr.live_dashboard(now=1.0)

    assert _run(scenario())["sessions"] == []
