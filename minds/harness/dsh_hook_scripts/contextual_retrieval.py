#!/usr/bin/env python3
"""UserPromptSubmit hook — inject standing rules + similarity-relevant memory + known-persons cue.

Three unconditional reads from lucent on every user turn:

1. ``/memory/list?tier=standing`` — every standing-tier row (filtered
   client-side to this mind's UUID + the ``shared`` sentinel). These
   are the always-on behavior rules; emitted as ``<standing-rules>``.
2. ``POST /memory/retrieve`` (body, never a query string — a prompt
   in a URL lands in Zeek's http.log and Loki verbatim) — top-k similarity matches, filtered
   client-side to this mind's UUID + the ``shared`` sentinel (same
   keep-set as standing; another mind's session captures must never
   leak into this mind's context), score>=0.50.
   Emitted as ``<relevant-memory>``.
3. ``POST /graph/query`` (body) carrying every candidate name in the
   prompt at once —
   for each name that resolves to exactly one Person node, pull
   scalar properties and the list of edge types present (no targets).
   Emitted as ``<known-persons>`` cue so the agent knows what KG data
   is available without dumping the full one-hop subgraph.

All blocks concatenated into a single
``hookSpecificOutput.additionalContext`` payload (``systemMessage`` is
UI-only and never reaches the model).
Short timeout, unconditional. On any failure, return empty (never block).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hive_mind_env as hive  # noqa: E402

# Capitalized tokens that are common English words / pronouns / sentence-starters,
# never actual person names worth a KG round-trip.
_NAME_STOPWORDS = {
    "I", "I'd", "I'll", "I'm", "I've", "It", "It's", "Its", "He", "She", "They",
    "We", "You", "Your", "My", "Me", "Mine", "His", "Her", "Their", "Our",
    "A", "An", "The", "Yes", "No", "OK", "Okay", "Now", "Then", "And", "But",
    "Or", "If", "So", "What", "When", "Where", "Why", "How", "Who", "Which",
    "This", "That", "These", "Those", "Here", "There", "Today", "Tomorrow",
    "Yesterday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Saturday", "Sunday",
}


# The hook treats every capitalised word as a candidate, so a pasted file or
# a block of log lines yields hundreds. They go to lucent in one request, and
# the ceiling matches the route's so the two cannot disagree about which
# prompts are answerable. Candidates dedupe case-insensitively because the
# store matches COLLATE NOCASE: "Claude" and "CLAUDE" are one lookup, and
# counting them separately would spend the ceiling on the same query twice.
_MAX_CANDIDATES = 256

# A name longer than the route will look up is dropped here rather than sent.
# The route answers it as its own refusal, but there is no reason to spend a
# slot in the request on a token that cannot be a name — a pasted sequence or
# a long generated identifier tokenises into one.
_MAX_NAME_CHARS = 200

# A batch answer is bounded here as well as by the ceiling on what is asked:
# the route traverses as deep as it is told to, and an unbounded read inside a
# hook that gates prompt submission is a pane frozen with nothing on screen.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _candidate_names(prompt: str) -> tuple[list[str], int]:
    """Extract capitalized words that might be person first names.

    Heuristic: any word starting with an uppercase letter, stripped of
    surrounding punctuation, minus the stopword list. Sentence-initial
    caps are still included — we'd rather pay a few extra KG lookups
    than miss a name at the start of a turn. False positives drop out
    when a name resolves to something other than exactly one Person.

    Returns the candidates and the number of distinct further candidates the
    ceiling excluded, so the caller can say the list is partial. Over the
    ceiling the kept ones come from both ends of the prompt, not the first
    N in token order.
    """
    if not prompt:
        return [], 0
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-]*", prompt)
    seen: list[str] = []
    seen_set: set[str] = set()
    dropped = 0
    for tok in tokens:
        if not tok or not tok[0].isupper():
            continue
        if tok in _NAME_STOPWORDS:
            continue
        if len(tok) > _MAX_NAME_CHARS:
            continue
        key = tok.lower()
        if key in seen_set:
            continue
        seen.append(tok)
        seen_set.add(key)
    if len(seen) <= _MAX_CANDIDATES:
        return seen, 0
    # A prompt over the ceiling is a paste with a question at one end of it,
    # and the question carries the names the turn is actually about. Keeping
    # the first N in token order drops exactly those when the paste comes
    # first: measured live, a pasted module followed by "what did Maurice
    # say" lost Maurice and kept a name that happened to appear early inside
    # the paste. Both ends survive instead.
    head = _MAX_CANDIDATES // 2
    tail = _MAX_CANDIDATES - head
    return seen[:head] + seen[-tail:], len(seen) - _MAX_CANDIDATES


def _query_persons(
    lucent_url: str, auth: dict[str, str], names: list[str]
) -> dict[str, dict]:
    """Resolve every candidate name in one request; keep the lone Persons.

    A name is kept when it resolves to exactly one node and that node is a
    Person. A miss, an ambiguous name, an Organization — all drop out, which
    is what makes the capitalised-word heuristic safe to be greedy with.

    The names travel in the body. On a query string they landed verbatim in
    Zeek's http.log, in uvicorn's access log and in Loki, where the sentinel
    read the hive's own people back as network events.
    """
    if not names:
        return {}
    payload = json.dumps({"names": names}).encode()
    headers = dict(auth)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{lucent_url}/graph/query", data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            # Bounded: one name at depth 1 is a few KB, but the route will
            # traverse as deep as it is asked to, and an unbounded read on a
            # prompt-gating hook is a frozen pane with nothing on screen.
            raw = resp.read(_MAX_RESPONSE_BYTES)
        body = json.loads(raw.decode())
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    kept: dict[str, dict] = {}
    for entry in body.get("results") or []:
        if not isinstance(entry, dict) or entry.get("count") != 1:
            continue
        matches = entry.get("matches") or []
        if len(matches) != 1:
            continue
        props = matches[0].get("properties") or {}
        if props.get("type") != "Person":
            continue
        kept[entry.get("entity") or ""] = matches[0]
    return kept


def _load_env() -> None:
    hive.load_env()


def _read_event() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _extract_prompt(event: dict) -> str:
    """Pull the user prompt out of the hook event. Harness shape varies."""
    for key in ("user_prompt", "prompt", "message"):
        v = event.get(key)
        if isinstance(v, str) and v.strip():
            return v
    tool_input = event.get("tool_input") or {}
    if isinstance(tool_input, dict):
        for key in ("user_prompt", "prompt"):
            v = tool_input.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _retrieve(lucent_url: str, auth: dict[str, str], *, query: str,
              k: int, min_score: float, mind_id: str) -> list[dict]:
    # Over-fetch, then apply the same mind_id keep-set as standing:
    # rows another mind's session wrote are its perspective, not ours.
    payload = json.dumps({
        "query": query,
        "k": k * 3,
        "min_score": min_score,
        "mode": "hybrid",
        "debug": True,
    }).encode()
    url = f"{lucent_url}/memory/retrieve"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={**auth, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode())
    except Exception:
        return []
    memories = body.get("memories") if isinstance(body, dict) else None
    if not isinstance(memories, list):
        return []
    keep = {mind_id, "shared"} if mind_id else {"shared"}
    return [m for m in memories if m.get("mind_id") in keep][:k]


def _load_standing(lucent_url: str, auth: dict[str, str], *,
                   mind_id: str) -> list[dict]:
    """Pull all standing-tier rows for this mind + the ``shared`` sentinel."""
    params = urllib.parse.urlencode({"tier": "standing", "limit": 100})
    url = f"{lucent_url}/memory/list?{params}"
    req = urllib.request.Request(url, headers=auth, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = json.loads(resp.read().decode())
    except Exception:
        return []
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return []
    keep = {mind_id, "shared"} if mind_id else {"shared"}
    return [e for e in entries if e.get("mind_id") in keep]


def _bullets(rows: list[dict]) -> str:
    return "\n".join(
        f"- {(r.get('content') or '').strip()}"
        for r in rows
        if (r.get("content") or "").strip()
    )


def _bullets_debug(rows: list[dict]) -> str:
    """Render relevant-memory bullets with bucket + score annotations.

    Each row gets a leading ``[bucket cos=X rec=Y bm=Z]`` tag so we can
    see in-context which retrieval signal surfaced each chunk.
    """
    out: list[str] = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        d = r.get("debug") or {}
        bucket = d.get("bucket", "?")
        cos = d.get("cosine")
        rec = d.get("recency_mult")
        bm = d.get("bm25")
        tag = f"[{bucket}"
        if cos is not None:
            tag += f" cos={cos}"
        if rec is not None:
            tag += f" rec={rec}"
        if bm is not None and bm > 0:
            tag += f" bm={bm}"
        tag += "]"
        out.append(f"- {tag} {content}")
    return "\n".join(out)


_PERSON_HIDE_KEYS = {
    "name", "type", "superseded", "created_at", "updated_at",
}


def _format_person(match: dict) -> str:
    props = match.get("properties") or {}
    name = props.get("name") or props.get("first_name") or "?"
    prop_pairs = [
        f"{k}={v}"
        for k, v in props.items()
        if k not in _PERSON_HIDE_KEYS and v not in (None, "", False)
    ]
    prop_pairs.sort()
    edge_types = sorted({
        via.get("type")
        for conn in (match.get("connections") or [])
        for via in (conn.get("via") or [])
        if via.get("type")
    })
    prop_str = ", ".join(prop_pairs) if prop_pairs else "(none)"
    edge_str = ", ".join(edge_types) if edge_types else "(none)"
    return (
        f"- {name} (Person)\n"
        f"  properties: {prop_str}\n"
        f"  edges_available: {edge_str}"
    )


def _known_persons_block(lucent_url: str, auth: dict[str, str], prompt: str) -> str:
    """Render one entry per person the prompt named, each person once.

    Dedupe is by node name: a turn naming someone by both first and last name
    resolves both to the same node, and rendering them separately is how the
    same person used to arrive in the cue two and three times over.

    A prompt past the candidate ceiling says so. The cue is a claim about who
    the prompt mentioned, and one that quietly covers the first 256 words is
    a claim it cannot support — silently, on exactly the pasted-document
    prompts most likely to name someone.
    """
    candidates, dropped = _candidate_names(prompt)
    if not candidates:
        return ""
    resolved = _query_persons(lucent_url, auth, candidates)
    seen_nodes: set[str] = set()
    rendered: list[str] = []
    for name in candidates:
        match = resolved.get(name)
        if match is None:
            continue
        node_name = (match.get("properties") or {}).get("name") or name
        if node_name in seen_nodes:
            continue
        seen_nodes.add(node_name)
        rendered.append(_format_person(match))
    if dropped:
        rendered.append(
            f"({dropped} further capitalised words in this prompt were not "
            f"looked up — the prompt is past the candidate ceiling)"
        )
    if not rendered:
        return ""
    return "<known-persons>\n" + "\n".join(rendered) + "\n</known-persons>"


def format_injection(prompt: str) -> str:
    """Return ``<standing-rules>`` + ``<relevant-memory>`` blocks, or '' when nothing applies."""
    if not prompt or not prompt.strip():
        return ""

    _load_env()
    lucent_url, bearer = hive.lucent()
    if not lucent_url:
        return ""
    auth = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    mind_id = os.environ.get("MIND_ID", "")

    standing = _load_standing(lucent_url, auth, mind_id=mind_id)
    standing_bullets = _bullets(standing)

    k = 5
    memories = _retrieve(lucent_url, auth, query=prompt, k=k, min_score=0.50,
                         mind_id=mind_id)
    # Hybrid endpoint pre-orders by bucket; preserve that order for clarity.
    memory_bullets = _bullets_debug(memories[:k])

    persons_block = _known_persons_block(lucent_url, auth, prompt)

    blocks = []
    if standing_bullets:
        blocks.append(f"<standing-rules>\n{standing_bullets}\n</standing-rules>")
    if persons_block:
        blocks.append(persons_block)
    if memory_bullets:
        blocks.append(f"<relevant-memory>\n{memory_bullets}\n</relevant-memory>")
    return "\n\n".join(blocks)


def main() -> None:
    event = _read_event()
    injection = format_injection(_extract_prompt(event))
    if injection:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": injection,
            }
        }))
    sys.exit(0)


if __name__ == "__main__":
    main()
