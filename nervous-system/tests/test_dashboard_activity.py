"""The whole turn on the feed, not just the half of it that was speech.

The feed used to carry assistant text and drop everything else, so a mind
inside a ten-minute tool chain rendered as an empty column and then as no
column at all. What the dashboard is for is seeing the work, so the filter
is gone: tool calls, their results and the sub-mind turns underneath them
all reach it, typed so a reader can tell them apart.
"""

from __future__ import annotations

import pytest

from comms.dashboard import LiveFeed


TEXT = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
}
TOOL_USE = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "pytest -q"},
            }
        ],
    },
}
TOOL_RESULT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "395 passed"}
        ],
    },
}


@pytest.fixture()
def feed():
    return LiveFeed()


# --- R2, R3: everything appears, nothing withheld ---------------------------


class TestTheWholeTurnIsCarried:
    def test_a_tool_call_reaches_the_feed_carrying_its_command(self, feed):
        """R2, R3. The command is the thing worth watching while it runs."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", TOOL_USE, now=1.0)

        carried = feed.since("s1", 0)
        assert [block["kind"] for block in carried] == ["tool_use"]
        assert "pytest -q" in carried[0]["text"]

    def test_a_tool_result_reaches_the_feed_carrying_its_output(self, feed):
        """R2, R3."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", TOOL_RESULT, now=1.0)

        carried = feed.since("s1", 0)
        assert [block["kind"] for block in carried] == ["tool_result"]
        assert carried[0]["text"] == "395 passed"

    def test_an_event_with_no_content_carries_nothing(self, feed):
        """R2. Liveness, not a block: a `result` ends a turn and says
        nothing, so appending an empty block for it would put a blank row
        in the column on every turn."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", {"type": "result", "subtype": "success"}, now=1.0)

        assert feed.since("s1", 0) == []

    def test_speech_and_the_tool_call_beside_it_are_carried_as_two_blocks(self, feed):
        """R2. The usual shape of a turn, and the two halves are different
        things: one is prose, one is a command."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe(
            "s1",
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me look."},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls /etc"},
                        },
                    ],
                },
            },
            now=1.0,
        )

        carried = feed.since("s1", 0)
        assert [block["kind"] for block in carried] == ["text", "tool_use"]
        assert carried[0]["text"] == "Let me look."
        assert "ls /etc" in carried[1]["text"]


# --- R4: the column stays through silence -----------------------------------


class TestActivityCountsAsLife:
    def test_a_conversation_working_through_tools_is_not_quiet(self, feed):
        """R4. Quiet is measured from the last thing that happened, not the
        last thing that was said — otherwise a build reads as a stopped
        turn and the console drops the column mid-work."""
        feed = LiveFeed(quiet_after=45.0, generating_ttl=3600.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.frame("s1")
        feed.observe("s1", TEXT, now=1000.0)
        feed.observe("s1", TOOL_USE, now=1090.0)

        assert feed.state("s1", now=1100.0)["quiet"] is False

    def test_a_conversation_past_the_threshold_with_no_activity_is_quiet(self, feed):
        """R4. The other side of the same boundary: nothing at all for long
        enough is still quiet, and hiding that would make the column lie."""
        feed = LiveFeed(quiet_after=45.0, generating_ttl=3600.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.frame("s1")
        feed.observe("s1", TEXT, now=1000.0)
        feed.observe("s1", TOOL_USE, now=1090.0)

        assert feed.state("s1", now=1140.0)["quiet"] is True


# --- R6, R7: a sub-mind's work, attributed ----------------------------------


class TestSubMindWork:
    def test_a_terminals_activity_blocks_are_carried_with_their_attribution(self, feed):
        """R6, R7. A terminal's work arrives already typed from the mind's
        tailer, because a pty publishes no harness events of its own."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe_blocks(
            "s1",
            [
                {"kind": "text", "text": "Found three.", "agent": "agent-7"},
                {"kind": "tool_use", "text": "grep -rn", "name": "Bash", "agent": None},
            ],
            now=1.0,
        )

        carried = feed.since("s1", 0)
        assert [block["agent"] for block in carried] == ["agent-7", None]
        assert [block["kind"] for block in carried] == ["text", "tool_use"]

    def test_blocks_for_a_conversation_the_feed_never_opened_are_dropped(self, feed):
        """R6. A block for an unknown session has no column to land in, and
        inventing one would put a conversation on screen that comms has no
        record of."""
        feed.observe_blocks("ghost", [{"kind": "text", "text": "hi"}], now=1.0)

        assert feed.since("ghost", 0) == []


# --- R4: liveness, not just the quiet flag ----------------------------------


class TestALiveTurnIsNotDroppedMidToolCall:
    def test_a_terminal_waiting_on_a_long_tool_call_is_still_generating(self):
        """R4. The harness writes nothing between issuing a tool call and
        its result, and a terminal has no `result` event to frame it — so
        the unframed ceiling is measured against a silence the turn cannot
        help. Measured on this host: 45 of 4,490 real tool gaps exceed 90
        seconds, the largest ten minutes.
        """
        feed = LiveFeed(unframed_ttl=90.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.observe_blocks(
            "s1", [{"kind": "tool_use", "text": "pytest", "name": "Bash"}], now=1000.0
        )

        assert feed.state("s1", now=1400.0)["generating"] is True

    def test_a_terminal_whose_last_word_was_prose_still_expires(self):
        """R4, the other side. An unanswered tool call is the only thing
        that earns the longer ceiling; a pane that stopped talking after a
        sentence is done, and holding its column would crowd out minds that
        are working."""
        feed = LiveFeed(unframed_ttl=90.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.observe_blocks("s1", [{"kind": "text", "text": "all done"}], now=1000.0)

        assert feed.state("s1", now=1400.0)["generating"] is False

    def test_a_tool_call_that_came_back_stops_holding_the_column_open(self):
        """R4. The result closes it: what follows is ordinary silence."""
        feed = LiveFeed(unframed_ttl=90.0)
        feed.begin("s1", mind_id="skippy", now=1000.0)
        feed.observe_blocks(
            "s1", [{"kind": "tool_use", "text": "pytest", "name": "Bash"}], now=1000.0
        )
        feed.observe_blocks(
            "s1", [{"kind": "tool_result", "text": "395 passed"}], now=1010.0
        )

        assert feed.state("s1", now=1400.0)["generating"] is False


# --- R3: the cap belongs on both paths --------------------------------------


class TestTheBufferIsBounded:
    def test_an_oversized_block_is_trimmed_before_it_is_stored(self):
        """R3. Measured on this host: the largest single rendered block is
        1,267,087 bytes, and an image tool_result renders to hundreds of KB
        of base64. Four hundred of those per conversation is the process."""
        feed = LiveFeed()
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe_blocks(
            "s1", [{"kind": "tool_result", "text": "z" * 200_000}], now=1.0
        )

        stored = feed.since("s1", 0)[0]
        assert stored["trimmed"] is True
        assert len(stored["text"].encode("utf-8")) <= 120_000

    def test_a_block_within_the_cap_is_stored_whole(self):
        """R3. The other side of the same boundary — nothing is trimmed for
        being merely long."""
        feed = LiveFeed()
        feed.begin("s1", mind_id="skippy", now=1.0)
        body = "z" * 119_999
        feed.observe_blocks("s1", [{"kind": "tool_result", "text": body}], now=1.0)

        stored = feed.since("s1", 0)[0]
        assert stored["trimmed"] is False
        assert stored["text"] == body


# --- R2: the chat path has to observe the events results actually ride on ---


class TestTheChatPathCarriesResults:
    def test_a_tool_result_event_is_observed_rather_than_only_opening_a_turn(self, feed):
        """R2. Every harness `tool_result` arrives on an entry of type
        `user` — 326 of 326 in this host's transcripts — so a fan-out that
        treats `user` as "open the turn and stop" shows commands going out
        and nothing coming back."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", TOOL_RESULT, now=2.0)

        assert [block["kind"] for block in feed.since("s1", 0)] == ["tool_result"]

    def test_an_events_absence_of_content_still_counts_as_life(self, feed):
        """R2. `result` closes a turn and says nothing: it must move the
        expiry without putting a blank row in the column."""
        feed.begin("s1", mind_id="skippy", now=1.0)
        feed.observe("s1", {"type": "result", "subtype": "success"}, now=50.0)

        assert feed.since("s1", 0) == []
        assert feed.state("s1", now=50.0)["last_block_at"] is None
