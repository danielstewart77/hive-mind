"""The hive-wide live feed the dashboard reads.

`stream_session_events` is per-session and hands an observer *every* harness
event unfiltered — `tool_use` inputs and `tool_result` bodies included — out
of a 200-slot queue that silently discards its oldest entry when full. None
of that is safe for a page that renders many conversations at once behind a
shared login, so the dashboard reads this instead:

- text only, so tool traffic never leaves comms on this path (req 21)
- sequenced, so a discarded run renders as a gap rather than as prose that
  reads perfectly and is missing its middle (req 22)
- an explicit generating state with a start, an end, and an expiry, because
  `sessions.status` is written to 'running' and never written back (req 23)
- a quiet state, because ten minutes inside a tool chain is silence, not
  stopping (req 24)
"""

from __future__ import annotations

import pytest

from comms.dashboard import LiveFeed


ASSISTANT_TEXT = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
}
TOOL_USE = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "cat ~/.ssh/id_rsa"}}
        ],
    },
}
TOOL_RESULT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [{"type": "tool_result", "content": "ssh-rsa AAAAB3Nza..."}],
    },
}
THINKING = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}]},
}


@pytest.fixture()
def feed():
    return LiveFeed()


# --- Requirement 21: text only ---------------------------------------------


class TestOnlyTextLeaves:
    def test_assistant_text_is_carried(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1.0)

        assert [e["text"] for e in feed.since("s1", 0)] == ["hello"]

    def test_a_tool_call_never_reaches_the_feed(self, feed):
        """A bash command the mind ran is not assistant speech, and this page
        answers to any logged-in console account."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", TOOL_USE, now=1.0)

        assert feed.since("s1", 0) == []

    def test_a_tool_result_never_reaches_the_feed(self, feed):
        """Tool results carry file contents and command output verbatim."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", TOOL_RESULT, now=1.0)

        assert feed.since("s1", 0) == []

    def test_thinking_is_not_carried(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", THINKING, now=1.0)

        assert feed.since("s1", 0) == []

    def test_a_mixed_block_carries_its_text_and_drops_its_tool_call(self, feed):
        """The usual shape of a real turn: the model says something, then
        calls a tool. Dropping the whole event would lose the speech;
        carrying it whole would leak the command."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe(
            "s1",
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me look."},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls /etc/shadow"}},
                    ],
                },
            },
            now=1.0,
        )

        carried = feed.since("s1", 0)
        assert [e["text"] for e in carried] == ["Let me look."]
        assert "shadow" not in str(carried)

    def test_a_whole_block_from_a_terminal_tailer_is_carried(self, feed):
        """`publish_pty_text` posts finished blocks in a different shape from
        the chat path's deltas. Both have to land, or a terminal conversation
        renders as an empty column while it is visibly writing."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", {"type": "assistant", "content": "from the pane", "block_complete": True}, now=1.0)

        assert [e["text"] for e in feed.since("s1", 0)] == ["from the pane"]


# --- Requirement 22: sequenced, and gaps are visible ------------------------


class TestGapsAreVisible:
    def test_each_block_carries_an_increasing_sequence(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1.0)
        feed.observe("s1", ASSISTANT_TEXT, now=2.0)

        assert [e["seq"] for e in feed.since("s1", 0)] == [1, 2]

    def test_a_reader_can_resume_from_where_it_left_off(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        for _ in range(3):
            feed.observe("s1", ASSISTANT_TEXT, now=1.0)

        assert [e["seq"] for e in feed.since("s1", 2)] == [3]

    def test_dropping_old_blocks_leaves_a_gap_a_reader_can_detect(self, feed):
        """The buffer is bounded — a page open for days cannot hold every
        block of every conversation. What it must not do is close over the
        hole: sequence numbers make the discarded run detectable, so the
        column can show a break instead of joining two halves of different
        sentences into fluent-looking nonsense."""
        feed = LiveFeed(buffer_blocks=3)
        feed.begin("s1", mind_id="skippy", now=1.0)
        for _ in range(5):
            feed.observe("s1", ASSISTANT_TEXT, now=1.0)

        carried = feed.since("s1", 0)

        assert [e["seq"] for e in carried] == [3, 4, 5]
        assert feed.state("s1", now=1.0)["first_available_seq"] == 3

    def test_sequences_are_per_conversation_not_shared(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.begin("s2", mind_id="mordecai", now=1.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1.0)
        feed.observe("s2", ASSISTANT_TEXT, now=1.0)

        assert feed.since("s1", 0)[0]["seq"] == 1
        assert feed.since("s2", 0)[0]["seq"] == 1

    def test_one_conversations_text_never_lands_in_anothers(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.begin("s2", mind_id="mordecai", now=1.0)
        feed.observe("s1", {"type": "assistant", "content": "A1"}, now=1.0)
        feed.observe("s2", {"type": "assistant", "content": "B1"}, now=1.0)
        feed.observe("s1", {"type": "assistant", "content": "A2"}, now=1.0)

        assert [e["text"] for e in feed.since("s1", 0)] == ["A1", "A2"]
        assert [e["text"] for e in feed.since("s2", 0)] == ["B1"]


# --- Requirement 23: generating is explicit, and expires --------------------


class TestGeneratingIsExplicit:
    def test_a_started_conversation_is_generating(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)

        assert feed.state("s1", now=1.0)["generating"] is True

    def test_an_ended_conversation_is_not_generating(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.end("s1", now=2.0)

        assert feed.state("s1", now=2.0)["generating"] is False

    def test_a_turn_that_never_reports_its_end_expires_on_its_own(self, feed):
        """A mind killed mid-turn, or a comms restart, leaves no end event.
        Without the expiry the column stays on screen forever, and the page
        shows four minds working on a hive where nothing is running."""
        feed = LiveFeed(generating_ttl=600.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)

        assert feed.state("s1", now=1599.0)["generating"] is True
        assert feed.state("s1", now=1601.0)["generating"] is False

    def test_output_pushes_the_expiry_back(self, feed):
        """A long turn that is still producing must not time out at its own
        ceiling — the expiry exists to catch silence, not duration."""
        feed = LiveFeed(generating_ttl=600.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1500.0)

        assert feed.state("s1", now=2000.0)["generating"] is True

    def test_a_conversation_never_seen_is_not_generating(self, feed):
        assert feed.state("unknown", now=1.0)["generating"] is False

    def test_the_live_set_names_only_conversations_still_generating(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.begin("s2", mind_id="mordecai", now=1.0)
        feed.end("s2", now=2.0)

        assert [row["session_id"] for row in feed.live(now=2.0)] == ["s1"]

    def test_the_live_set_carries_the_mind_each_conversation_belongs_to(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)

        assert feed.live(now=1.0)[0]["mind_id"] == "skippy"


# --- Requirement 24: quiet is not stopped -----------------------------------


class TestQuietIsNotStopped:
    def test_a_conversation_with_recent_output_is_not_quiet(self, feed):
        feed = LiveFeed(quiet_after=30.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1000.0)

        assert feed.state("s1", now=1020.0)["quiet"] is False

    def test_a_long_tool_chain_reads_as_quiet_and_stays_on_screen(self, feed):
        """Ten minutes inside a build emits no assistant text. Removing the
        column would tell the operator the turn finished."""
        feed = LiveFeed(quiet_after=30.0, generating_ttl=3600.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1000.0)

        state = feed.state("s1", now=1600.0)

        assert state["quiet"] is True
        assert state["generating"] is True

    def test_quiet_is_measured_from_the_last_output_not_from_the_start(self, feed):
        feed = LiveFeed(quiet_after=30.0, generating_ttl=3600.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1500.0)

        assert feed.state("s1", now=1510.0)["quiet"] is False


# --- Rotation ---------------------------------------------------------------


class TestRotation:
    def test_a_new_conversation_under_one_session_clears_the_old_text(self, feed):
        """A terminal rotation keeps the session row and swaps the harness
        conversation beneath it. Carrying the old column forward would show
        the replaced conversation's words under a context count that just
        reset to nearly nothing."""
        feed.begin("s1", mind_id="skippy", conversation_id="conv-1", now=1.0)
        feed.observe("s1", {"type": "assistant", "content": "old"}, now=1.0)

        feed.begin("s1", mind_id="skippy", conversation_id="conv-2", now=2.0)

        assert feed.since("s1", 0) == []

    def test_the_same_conversation_resuming_keeps_its_text(self, feed):
        """Two turns in one conversation are not a rotation. Clearing on
        every `begin` would blank the column on every user message."""
        feed.begin("s1", mind_id="skippy", conversation_id="conv-1", now=1.0)
        feed.observe("s1", {"type": "assistant", "content": "first"}, now=1.0)

        feed.begin("s1", mind_id="skippy", conversation_id="conv-1", now=2.0)

        assert [e["text"] for e in feed.since("s1", 0)] == ["first"]


# --- Forgetting -------------------------------------------------------------


class TestForgetting:
    def test_a_closed_conversation_is_dropped_entirely(self, feed):
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1.0)

        feed.forget("s1")

        assert feed.since("s1", 0) == []
        assert feed.state("s1", now=1.0)["generating"] is False

    def test_conversations_idle_past_the_retention_window_are_swept(self, feed):
        """A hive running for weeks must not accumulate a buffer per session
        it once saw. The sweep is what bounds the process, and it only ever
        takes conversations that are neither generating nor recent."""
        feed = LiveFeed(generating_ttl=60.0, retain_after_end=300.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.end("s1", now=1000.0)

        feed.sweep(now=1000.0 + 301.0)

        assert feed.since("s1", 0) == []

    def test_the_sweep_leaves_a_conversation_that_is_still_generating(self, feed):
        feed = LiveFeed(generating_ttl=3600.0, retain_after_end=1.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.observe("s1", ASSISTANT_TEXT, now=1000.0)

        feed.sweep(now=1100.0)

        assert [e["text"] for e in feed.since("s1", 0)] == ["hello"]
