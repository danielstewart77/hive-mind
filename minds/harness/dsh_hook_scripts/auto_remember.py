#!/usr/bin/env python3
"""Stop hook: capture what this turn taught into the hive's memory.

The last thing the user said and the last thing the mind answered are
classified in one structured call through the hive's inference proxy
(``/v1/chat/completions`` with a json_schema ``response_format``, the mind's
proxy key, the model ``HIVE_CLASSIFIER_MODEL``) against four independent
branches, and each branch that applies is written
to lucent (``/memory/store``, ``tier: contextual``, ``source: session``,
``mind_id`` this mind's UUID):

* feedback — a stable preference or correction, as data class ``feedback``;
* person — prose about a named person, kept only when the name resolves to
  exactly one Person node, as ``current-state`` tagged ``[About <Name>]``;
* tech — a durable fact about the system now or planned, as
  ``current-state`` or ``future-state``;
* self — the mind's own identity, which this sweep never writes.

A chunk the classifier says would be a typed graph node or edge is dropped:
structured facts are written by explicit skills, not by a sweep. A delegate's
turn is skipped, since every hook fires for it under the mind's identity.

The tools service is not involved: a hive installed without it captures
the same. A mind without a proxy address, key or classifier model captures
nothing and logs why. Every run is one
line in ``<state dir>/runs.jsonl``. The work runs detached, so the turn is
never held.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dsh_transcript  # noqa: E402
import hive_mind_env as hive  # noqa: E402

NAME = "auto_remember"

SCHEMA = {
    "type": "object",
    "properties": {
        "feedback": {"type": "object", "properties": {
            "present": {"type": "boolean"}, "content": {"type": "string"},
            "reason": {"type": "string"}},
            "required": ["present", "content", "reason"], "additionalProperties": False},
        "person": {"type": "object", "properties": {
            "present": {"type": "boolean"}, "name": {"type": "string"},
            "prose": {"type": "string"}, "fits_schema": {"type": "boolean"},
            "reason": {"type": "string"}},
            "required": ["present", "name", "prose", "fits_schema", "reason"],
            "additionalProperties": False},
        "tech": {"type": "object", "properties": {
            "present": {"type": "boolean"},
            "data_class": {"type": "string", "enum": ["current-state", "future-state", ""]},
            "prose": {"type": "string"}, "fits_schema": {"type": "boolean"},
            "reason": {"type": "string"}},
            "required": ["present", "data_class", "prose", "fits_schema", "reason"],
            "additionalProperties": False},
        "self": {"type": "object", "properties": {
            "present": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["present", "reason"], "additionalProperties": False},
    },
    "required": ["feedback", "person", "tech", "self"],
    "additionalProperties": False,
}

PROMPT = """Classify the conversation turn below against four branches. Branches are independent — set present=true for every branch that applies. Default to present=false for all four; most turns are routine work that should pass through cleanly.

## Branches

1. feedback — the turn contains a stable preference, correction, validated approach, or pushback on the agent that should shape future behavior. If present, write `content` per the feedback spec's "Chunk shape" (imperative rule + Why + How to apply, no transcript quoting).

2. person — the turn contains prose ABOUT a specific named person in the user's life (e.g. "Gary likes woodworking"). Set `name` to the person's first name. Set `prose` to a self-contained sentence about that person (do not quote the transcript). Set `fits_schema` true only if the fact would be a typed node/edge in the KG (a property like phone/email/employer, or an explicit relationship edge) — those belong in a structured graph write, NOT this sweep.

3. tech — the turn contains a durable fact about the system, codebase, configuration, or planned future state. Set `data_class` to "current-state" for facts about the present, "future-state" for plans/intentions. Set `prose` to a self-contained sentence stating the fact. Set `fits_schema` true only if the fact would be a typed KG node/edge (Device property, Mind property, etc.).

4. self — the turn is about the agent's own identity, voice, or behavior. Set present=true and skip content fields; this sweep does not store it.

When present=false for a branch, leave string fields empty and give a one-line reason. When fits_schema=true on person/tech, the sweep drops the chunk (structured facts are written by explicit skills) — but still report present=true so the routing log is accurate.

### Turn
{turn}

### Feedback spec
{spec}

### <kg-schema>
{schema}
### </kg-schema>
"""


def specs_dir() -> Path:
    """The data-class specs: ``SPECS_DIR``, else the repo the mind folder sits in."""
    named = os.environ.get("SPECS_DIR")
    return Path(named) if named else hive.MIND_DIR.parents[1] / "specs" / "data-classes"


def _text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text") or "" for b in content or []
                     if isinstance(b, dict) and b.get("type") == "text")


def last_exchange(entries: list[dict]) -> tuple[str, str]:
    """The last real user message and the last assistant text, in that order.

    A tool's result is a user entry too, and a pure tool call is an assistant
    one; neither carries text, so neither is mistaken for the exchange.
    """
    user = next((_text(e) for e in reversed(entries)
                 if e.get("type") == "user" and _text(e).strip()), "")
    assistant = next((_text(e) for e in reversed(entries)
                      if e.get("type") == "assistant" and not e.get("isCompactSummary")
                      and _text(e).strip()), "")
    return user, assistant


def _schema_block(lucent_url: str, bearer: str) -> str:
    try:
        schema = hive.request("GET", f"{lucent_url}/graph/schema", bearer, timeout=5)
    except Exception:  # noqa: BLE001 — classify without it rather than not at all
        return ""
    types = "\n".join(
        f"- {t.get('name')} ({t.get('kind')}) "
        f"required={','.join((t.get('schema') or {}).get('required') or [])} "
        f"optional={','.join((t.get('schema') or {}).get('optional') or [])}"
        for t in schema.get("types") or [])
    edges = "\n".join(
        f"- {e.get('name')} "
        + " | ".join(f"{p.get('from')}→{p.get('to')}" for p in e.get("endpoints") or [])
        for e in schema.get("edges") or [])
    return f"Node types:\n{types}\n\nEdge types:\n{edges}"


def _one_person(lucent_url: str, bearer: str, name: str) -> bool:
    reply = hive.request("POST", f"{lucent_url}/graph/query", bearer,
                         {"names": [name], "depth": 1}, timeout=5)
    result = (reply.get("results") or [{}])[0]
    matches = result.get("matches") or [{}]
    return result.get("count") == 1 and (matches[0].get("properties") or {}).get("type") == "Person"


def chunks(verdict: dict, person_resolves) -> list[tuple[str, str, str]]:
    """``(branch, data_class, content)`` for every branch worth storing."""
    out = []
    feedback = verdict.get("feedback") or {}
    if feedback.get("present") and (feedback.get("content") or "").strip():
        out.append(("feedback", "feedback", feedback["content"]))
    person = verdict.get("person") or {}
    name, prose = (person.get("name") or "").strip(), (person.get("prose") or "").strip()
    if person.get("present") and not person.get("fits_schema") and name and prose:
        if person_resolves(name):
            out.append(("person", "current-state", f"[About {name}] {prose}"))
    tech = verdict.get("tech") or {}
    if (tech.get("present") and not tech.get("fits_schema")
            and (tech.get("prose") or "").strip()
            and tech.get("data_class") in ("current-state", "future-state")):
        out.append(("tech", tech["data_class"], tech["prose"]))
    return out


def classify(proxy_url: str, key: str, model: str, prompt: str) -> dict:
    """The classifier's verdict, from the proxy's chat route under SCHEMA."""
    reply = hive.request("POST", f"{proxy_url}/v1/chat/completions", key, {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "turn_classification", "strict": True, "schema": SCHEMA}},
    }, timeout=120)
    content = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    verdict = json.loads(content)
    if not isinstance(verdict, dict):
        raise ValueError(f"the classifier answered {type(verdict).__name__}")
    return verdict


def _record(**fields: object) -> None:
    try:
        directory = hive.state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "runs.jsonl", "a") as handle:
            handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                     **fields}) + "\n")
    except OSError:
        pass


def capture(event: dict) -> list[str]:
    """Classify this turn and store what applies; the stored entry ids."""
    sid = str(event.get("session_id") or "")
    raw = str(event.get("transcript_path") or "")
    if not raw or dsh_transcript.is_delegate_log(Path(raw)):
        return []
    path = dsh_transcript.resolve(event)
    if path is None:
        _record(session_id=sid, status="skip", reason="unreadable-transcript")
        return []
    entries = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    user, assistant = last_exchange(entries)
    if not user and not assistant:
        _record(session_id=sid, status="skip", reason="no-content")
        return []
    mind_id = os.environ.get("MIND_ID", "")
    proxy_url, proxy_key = hive.proxy()
    model = os.environ.get("HIVE_CLASSIFIER_MODEL", "").strip()
    lucent_url, bearer = hive.lucent()
    if not (mind_id and proxy_url and proxy_key and model and lucent_url):
        _record(session_id=sid, status="skip", reason="no-classifier-or-memory")
        return []
    try:
        spec = (specs_dir() / "feedback.md").read_text()
    except OSError:
        spec = ""
    prompt = PROMPT.format(turn=f"## User\n{user}\n\n## Assistant\n{assistant}\n",
                           spec=spec, schema=_schema_block(lucent_url, bearer))
    try:
        verdict = classify(proxy_url, proxy_key, model, prompt)
    except Exception as exc:  # noqa: BLE001
        _record(session_id=sid, status="fail", reason="classifier", error=str(exc))
        return []

    def resolves(name: str) -> bool:
        try:
            return _one_person(lucent_url, bearer, name)
        except Exception:  # noqa: BLE001 — an unreachable graph is not a match
            return False

    stored = []
    for branch, data_class, content in chunks(verdict, resolves):
        try:
            reply = hive.request("POST", f"{lucent_url}/memory/store", bearer, {
                "content": content, "data_class": data_class, "tier": "contextual",
                "mind_id": mind_id, "source": "session"}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            _record(session_id=sid, status="fail", reason="lucent-write", branch=branch,
                    error=str(exc))
            continue
        entry = reply.get("id") or reply.get("existing_id") or ""
        _record(session_id=sid, status="pass", branch=branch, data_class=data_class,
                entry_id=entry, deduped=bool(reply.get("deduped")))
        stored.append(str(entry))
    if not stored:
        _record(session_id=sid, status="discard",
                reason=(verdict.get("feedback") or {}).get("reason") or "")
    return stored


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(event, dict) or not event.get("transcript_path"):
        return
    if not hive.detach():
        return
    try:
        hive.load_env()
        capture(event)
    except Exception as exc:  # noqa: BLE001 — a hook never fails the turn
        hive.log(NAME, f"exception: {exc!r}")
    os._exit(0)


if __name__ == "__main__":
    main()
