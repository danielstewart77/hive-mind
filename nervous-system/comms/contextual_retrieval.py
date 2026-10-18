"""Per-turn contextual retrieval — Section G of the memory system design.

REQ-044: query data_class=feedback on every user prompt.
REQ-045: top-3 with cosine similarity >= 0.65.
REQ-046: inject as a <behavior-rules> flat-bullet XML block.
REQ-047: short timeout; on failure, return "" so the hook never blocks.
REQ-048: unconditional — no skip path.

The function ``format_injection`` is what the harness hook calls. It takes
a user prompt, queries lucent-api, and returns either a behavior-rules
block ready for systemMessage injection or an empty string.
"""
from __future__ import annotations

import os

import requests

LUCENT_URL = os.environ.get("LUCENT_URL_SELF", "http://127.0.0.1:8425")
def _auth_headers() -> dict:
    """Read per call, not at import.

    A credential read once at import needs a process restart to pick up a
    re-mint, which contradicts the promise the store makes — revocation lands
    on the next request, nothing to restart.
    """
    token = os.environ.get("LUCENT_BEARER_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}
DATA_CLASS = "feedback"
TOP_K = 3
MIN_SCORE = 0.65
REQUEST_TIMEOUT = 2  # seconds — never block the turn


def format_injection(prompt: str, mind_id: str = "") -> str:
    """Return a <behavior-rules> block for the prompt, or "" when none applies.

    `mind_id` names the mind this prompt is being composed for, and is not
    optional in effect: comms holds a `service` credential, so a retrieve that
    names no mind reaches *every* mind's feedback memories, and this is the
    one call in the hive that fires on every single turn. Unnamed, it returns
    nothing rather than quietly reading the union — the rules it injects are
    behavioural, and injecting another person's into a reply is worse than
    injecting none.
    """
    if not prompt or not prompt.strip():
        return ""
    if not str(mind_id or "").strip():
        return ""
    try:
        # Body, never a query string: the prompt is a whole user turn and a
        # URL carrying it is logged verbatim by Zeek, uvicorn and Loki.
        resp = requests.post(
            f"{LUCENT_URL}/memory/retrieve",
            json={
                "query": prompt,
                "data_class": DATA_CLASS,
                "k": TOP_K,
                "min_score": MIN_SCORE,
                "mind_id": mind_id,
            },
            headers=_auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception:  # bootstrap path — never raise
        return ""

    memories = body.get("memories") if isinstance(body, dict) else None
    if not memories:
        return ""

    bullets = "\n".join(
        f"- {(m.get('content') or '').strip()}"
        for m in memories
        if (m.get("content") or "").strip()
    )
    if not bullets:
        return ""
    return f"<behavior-rules>\n{bullets}\n</behavior-rules>"
