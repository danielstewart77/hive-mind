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
