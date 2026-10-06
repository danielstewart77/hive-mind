"""Approve or deny a held action from the chat it was announced in.

Some tools do not fire until a person says so — sending mail as the operator,
for one. The holder of that decision is hive-tools: it mints a token, posts the
question into chat with two buttons, and waits. This is the other end of those
buttons.

Nothing here decides anything. It reads which button was tapped, tells
hive-tools, and reports back what hive-tools said. A decision this surface
invented would be a decision nobody made.
"""

from __future__ import annotations

import os

APPROVE_PREFIX = "hitl_approve_"
DENY_PREFIX = "hitl_deny_"

# What a `CallbackQueryHandler` is registered with. Narrow on purpose: a
# handler with no pattern swallows every callback the chat produces, and the
# session picker's buttons travel through the same channel.
CALLBACK_PATTERN = r"^hitl_(approve|deny)_"

DEFAULT_TOOLS_URL = "http://127.0.0.1:9421"


def parse_callback(data: str) -> tuple[str, str] | None:
    """The action and token a tapped button carries, or None if it is not ours.

    Returned rather than raised: this runs against whatever payload arrived,
    and a payload belonging to another feature is not an error.
    """
    if data.startswith(APPROVE_PREFIX):
        return "approve", data[len(APPROVE_PREFIX):]
    if data.startswith(DENY_PREFIX):
        return "deny", data[len(DENY_PREFIX):]
    return None


def tools_url() -> str:
    """Where hive-tools is holding the decision."""
    return os.environ.get("HIVE_TOOLS_URL", DEFAULT_TOOLS_URL).rstrip("/")


def _tools_token() -> str:
    return os.environ.get("HIVE_TOOLS_TOKEN", "")


def respond_request(token: str, action: str) -> tuple[str, dict, dict]:
    """The URL, JSON body and headers for reporting one decision."""
    headers = {"Authorization": f"Bearer {_tools_token()}"} if _tools_token() else {}
    return f"{tools_url()}/hitl/{token}/respond", {"action": action}, headers


def outcome_label(action: str, status: int, body: str = "") -> str:
    """What the message says once hive-tools has answered.

    A failure is named with its status and the start of whatever came back,
    because the operator's next question is always whether the thing actually
    happened — and "something went wrong" cannot answer it.
    """
    if status == 200:
        return "Approved" if action == "approve" else "Denied"
    return f"Error {status}: {body[:120]}"
