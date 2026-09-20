"""A delegated mind's reply reaches the caller as the text that mind wrote.

One `assistant` event carries several content blocks, and the break between
them is the mind's own paragraphing.
"""

import json
import os
import sys

import pytest

NS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
)
sys.path.insert(0, os.path.join(NS, "comms"))

from inter_mind_api.inter_mind import collect_response  # noqa: E402


def _lines(*events: dict) -> list[str]:
    return [f"data: {json.dumps(e)}" for e in events]


def _assistant(*texts: str) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": t} for t in texts]}}


class TestInterMindResponseJoining:

    def test_separate_blocks_keep_the_break_between_them(self):
        """Two paragraphs from the delegated mind stay two paragraphs."""
        assert collect_response(_lines(_assistant("First.", "Second."))) == (
            "First.\n\nSecond."
        )

    def test_a_block_ending_in_a_newline_still_gets_one_blank_line(self):
        """The break is one blank line, not one plus whatever the text carried."""
        assert collect_response(_lines(_assistant("First.\n", "Second."))) == (
            "First.\n\nSecond."
        )

    def test_a_sub_delegates_turn_is_not_part_of_the_reply(self):
        """Only the mind we asked speaks in the response we return."""
        events = _lines(
            _assistant("Mine."),
            {**_assistant("Not mine."), "parent_tool_use_id": "toolu_1"},
        )

        assert collect_response(events) == "Mine."

    def test_the_result_event_answers_a_turn_with_no_assistant_text(self):
        """A tool-only turn still returns what the gateway reported."""
        events = _lines({"type": "result", "result": "Done, nothing to say."})

        assert collect_response(events) == "Done, nothing to say."
