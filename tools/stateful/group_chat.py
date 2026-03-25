"""Group chat tools — forward messages between minds in a group session.

Designed for direct FastMCP registration (no @tool() decorator).
"""

import json
import logging
import os

import requests  # type: ignore[import-untyped]

GATEWAY_URL = os.environ.get("HIVE_MIND_SERVER_URL", os.environ.get("GATEWAY_URL", "http://localhost:8420"))
logger = logging.getLogger(__name__)


def forward_to_mind(mind_id: str, message: str, group_session_id: str) -> str:
    """Forward a message to a specific mind within a group session.

    Args:
        mind_id: Target mind to forward to (e.g. "nagatha").
        message: Message content to send.
        group_session_id: The group session this exchange belongs to.

    Returns:
        JSON string with the mind's response text and metadata.
    """
    try:
        # Look up existing child sessions for this mind in the group
        sessions_resp = requests.get(
            f"{GATEWAY_URL}/sessions",
            params={"status": "running"},
            timeout=10,
        )
        sessions_resp.raise_for_status()
        sessions = sessions_resp.json()

        # Find existing child session for this mind in this group
        child_session_id = None
        for s in sessions:
            if (s.get("mind_id") == mind_id
                    and s.get("owner_ref") == group_session_id
                    and s.get("status") != "closed"):
                child_session_id = s["id"]
                break

        # Create child session if none exists
        if not child_session_id:
            create_resp = requests.post(
                f"{GATEWAY_URL}/sessions",
                json={
                    "owner_type": "group",
                    "owner_ref": group_session_id,
                    "client_ref": f"group-{group_session_id}-{mind_id}",
                    "mind_id": mind_id,
                },
                timeout=30,
            )
            create_resp.raise_for_status()
            child_session_id = create_resp.json()["id"]

        # Send message to the child session
        msg_resp = requests.post(
            f"{GATEWAY_URL}/sessions/{child_session_id}/message",
            json={"content": message},
            timeout=120,
            stream=True,
        )
        msg_resp.raise_for_status()

        # Collect SSE response text
        response_text = ""
        for line in msg_resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line.removeprefix("data: "))
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        response_text += block.get("text", "")
            elif event.get("type") == "result":
                if not response_text:
                    response_text = event.get("result", "")

        return json.dumps({
            "mind_id": mind_id,
            "group_session_id": group_session_id,
            "response": response_text,
            "session_id": child_session_id,
        })

    except Exception as e:
        logger.exception("forward_to_mind failed for mind=%s", mind_id)
        return json.dumps({"error": str(e)})


GROUP_CHAT_TOOLS = [forward_to_mind]
