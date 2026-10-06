"""Approve and deny taps, reported to whoever is holding the decision.

Nothing here decides anything: the token was minted by hive-tools, and what
the message ends up saying is what hive-tools answered. A decision this
surface invented would be a decision nobody made.
"""

import re

import pytest

from hive_surfaces import hitl
from hive_surfaces.session_picker import CB_NEW, CB_SWITCH, encode
from hive_surfaces.session_picker import CALLBACK_PATTERN as PICKER_PATTERN


class TestReadingWhichButtonWasTapped:
    def test_an_approve_tap_carries_its_token(self) -> None:
        assert hitl.parse_callback("hitl_approve_abc123") == ("approve", "abc123")

    def test_a_deny_tap_carries_its_token(self) -> None:
        assert hitl.parse_callback("hitl_deny_abc123") == ("deny", "abc123")

    def test_a_payload_belonging_to_another_feature_is_not_ours(self) -> None:
        """Returned, not raised — a picker tap is not an error."""
        assert hitl.parse_callback(encode(CB_SWITCH, "abc")) is None
        assert hitl.parse_callback("") is None


class TestTheTwoHandlersDoNotTakeEachOthersTaps:
    """A handler with no pattern swallows every callback the chat produces."""

    @pytest.mark.parametrize("payload", ["hitl_approve_tok", "hitl_deny_tok"])
    def test_a_held_decision_reaches_only_the_decision_handler(self, payload) -> None:
        assert re.search(hitl.CALLBACK_PATTERN, payload)
        assert not re.search(PICKER_PATTERN, payload)

    @pytest.mark.parametrize("payload", [encode(CB_SWITCH, "abc"), encode(CB_NEW)])
    def test_a_picker_tap_reaches_only_the_picker(self, payload) -> None:
        assert re.search(PICKER_PATTERN, payload)
        assert not re.search(hitl.CALLBACK_PATTERN, payload)


class TestWhatIsSentToTheDecisionHolder:
    def test_the_token_addresses_the_held_decision(self, monkeypatch) -> None:
        monkeypatch.setenv("HIVE_TOOLS_URL", "http://tools.example:9421/")
        monkeypatch.setenv("HIVE_TOOLS_TOKEN", "t0ken")

        url, payload, headers = hitl.respond_request("abc123", "approve")

        assert url == "http://tools.example:9421/hitl/abc123/respond"
        assert payload == {"action": "approve"}
        assert headers == {"Authorization": "Bearer t0ken"}

    def test_no_configured_token_sends_no_authorization_header(self, monkeypatch) -> None:
        """An empty bearer reads as a credential to the far end; absent does not."""
        monkeypatch.delenv("HIVE_TOOLS_TOKEN", raising=False)

        _url, _payload, headers = hitl.respond_request("abc123", "deny")

        assert headers == {}


class TestWhatTheMessageSaysAfterwards:
    def test_an_accepted_approval_reads_as_approved(self) -> None:
        assert hitl.outcome_label("approve", 200) == "Approved"

    def test_an_accepted_denial_reads_as_denied(self) -> None:
        assert hitl.outcome_label("deny", 200) == "Denied"

    def test_a_refusal_names_its_status_and_what_came_back(self) -> None:
        """"Something went wrong" cannot answer "did it actually happen"."""
        label = hitl.outcome_label("approve", 409, "already answered")

        assert "409" in label and "already answered" in label

    def test_a_long_error_body_is_trimmed_rather_than_sent_whole(self) -> None:
        label = hitl.outcome_label("approve", 500, "x" * 500)

        assert len(label) < 200
