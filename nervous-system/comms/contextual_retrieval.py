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
LUCENT_BEARER = os.environ.get("LUCENT_BEARER_TOKEN", "")
_AUTH_HEADERS = {"Authorization": f"Bearer {LUCENT_BEARER}"} if LUCENT_BEARER else {}
DATA_CLASS = "feedback"
TOP_K = 3
MIN_SCORE = 0.65
REQUEST_TIMEOUT = 2  # seconds — never block the turn


def format_injection(prompt: str) -> str:
    """Return a <behavior-rules> block for the given prompt, or "" when nothing applies."""
    if not prompt or not prompt.strip():
        return ""
    try:
        resp = requests.get(
            f"{LUCENT_URL}/memory/retrieve",
            params={
                "query": prompt,
                "data_class": DATA_CLASS,
                "k": TOP_K,
                "min_score": MIN_SCORE,
            },
            headers=_AUTH_HEADERS,
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
