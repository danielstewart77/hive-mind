"""Inter-mind communication tools — direct delegation between minds.

Designed for direct FastMCP registration (no @tool() decorator).
"""

import json
import logging
import os
from typing import Optional

import requests  # type: ignore[import-untyped]

GATEWAY_URL = os.environ.get("HIVE_MIND_SERVER_URL", os.environ.get("GATEWAY_URL", "http://localhost:8420"))
logger = logging.getLogger(__name__)


def delegate_to_mind(
    mind_id: str,
    message: str,
    mode: str = "verbatim",
    chain: Optional[list] = None,
) -> str:
    """Delegate a message to another mind for direct inter-mind communication.

    Args:
        mind_id: Target mind to delegate to.
        message: Message to send.
        mode: Response mode hint -- "verbatim", "paraphrase", or "silent".
        chain: List of mind_ids already in the call chain (for loop prevention).

    Returns:
        JSON string with the mind's response and metadata.
    """
    if chain is None:
        chain = []

    # Cycle prevention
    if mind_id in chain:
        return json.dumps({"error": f"Cycle detected: {mind_id} already in chain {chain}"})

    # 1-hop limit for Phase 4b
    if len(chain) >= 1:
        return json.dumps({"error": f"Hop limit exceeded: chain length {len(chain)} >= 1"})

    try:
        # Create or find a session for the target mind
        create_resp = requests.post(
            f"{GATEWAY_URL}/sessions",
            json={
                "owner_type": "inter_mind",
                "owner_ref": f"delegate-{mind_id}",
                "client_ref": f"delegate-{mind_id}",
                "mind_id": mind_id,
            },
            timeout=30,
        )
        create_resp.raise_for_status()
        session_id = create_resp.json()["id"]

        # Send message
        msg_resp = requests.post(
            f"{GATEWAY_URL}/sessions/{session_id}/message",
            json={"content": message},
            timeout=120,
            stream=True,
        )
        msg_resp.raise_for_status()

        # Collect response
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
            "response": response_text,
            "mode": mode,
            "chain": chain + [mind_id],
            "inter_mind": True,
        })

    except Exception as e:
        logger.exception("delegate_to_mind failed for mind=%s", mind_id)
        return json.dumps({"error": str(e)})


INTER_MIND_TOOLS = [delegate_to_mind]
