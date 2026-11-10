#!/usr/bin/env python3
"""Stop hook: measure the conversation, report it, and arm a rotation when full.

Every turn's size goes to the gateway (``POST /sessions/context``), which
draws it against the rotation point and learns the opening's overhead from
it. When the size reaches this model's threshold the conversation is armed
(``POST /sessions/arm-rotation``) and the gateway replaces it on the next user
turn, rendering the handover from this mind's own transcript. A terminal pane
never reaches the gateway's chat path, so for a pane the arm is also staged
on disk for ``rotation_fire`` to fire on the next typed prompt.

What the hook decides on:

* the size: the ``context`` figure dsh puts on its Stop payload, else the
  last figure the transcript recorded, plus an estimate of what a failed turn
  added after it;
* the threshold: ``HIVE_ROTATION_THRESHOLD_TOKENS``, which the mind server
  sets on every spawn from the proxy's row for the model. A model with no
  threshold is reported and never armed — rotating on a guess ejects a
  conversation that may be nearly empty.

Not armed here: a turn refused for size (the gateway's overflow path owns
it) and a turn with no conversation. In a goal run the answer is the hook's
stdout, ``{"rotate": true}``, and dsh ends the run for the gateway to carry
onto a fresh conversation; every other Stop is answered at once and decided
in a detached child.

The adapter runs this same command after a turn that fired no Stop, with
``stop_reason: "error"`` and the turn's ``error_code``.
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

NAME = "rotation_check"

#: Characters per token, for the part of a failed turn nothing counted.
CHARS_PER_TOKEN = 4

_MEASURED_KINDS = ("usage", "estimated")
_USAGE_FIELDS = ("input_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens", "output_tokens")


def _counted(figure: object) -> int:
    """A context figure's token count, or 0 when it carries none (zero is none)."""
    if not isinstance(figure, dict) or figure.get("kind") not in _MEASURED_KINDS:
        return 0
    try:
        tokens = int(figure.get("tokens") or 0)
    except (TypeError, ValueError):
        return 0
    return max(tokens, 0)


def _usage_total(entry: dict) -> int:
    usage = (entry.get("message") or {}).get("usage") or {}
    return sum(int(usage.get(k) or 0) for k in _USAGE_FIELDS)


def _added_chars(entries: list[dict]) -> int:
    total = 0
    for entry in entries:
        content = (entry.get("message") or {}).get("content")
        if content:
            total += len(content) if isinstance(content, str) else len(json.dumps(content))
    return total


def failed(event: dict) -> bool:
    return event.get("stop_reason") == "error" or event.get("hook_event_name") == "StopFailure"


def measure(event: dict, entries: list[dict]) -> dict:
    """``{"tokens", "kind", "model", "window"}`` for the conversation after this turn.

    ``tokens`` is None and ``kind`` "none" when nothing measured it. A count
    from before this turn began is not a count for this turn, except after a
    failed one: a failed call reports nothing, and what it was refused over is
    still in the conversation.
    """
    given = event.get("context") if isinstance(event.get("context"), dict) else {}
    model = str(given.get("model") or event.get("model") or "")
    window = given.get("window")
    if _counted(given):
        return {"tokens": _counted(given), "kind": given["kind"], "model": model,
                "window": window}
    start = 0
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].get("type") == "turn_start":
            start = i + 1
            break
    for entry in reversed(entries[start:]):
        figure = entry.get("context") if entry.get("type") == "context" else None
        if figure and _counted(figure):
            return {"tokens": _counted(figure), "kind": figure["kind"],
                    "model": model or str(figure.get("model") or ""),
                    "window": window or figure.get("window")}
        if entry.get("type") == "assistant" and _usage_total(entry):
            return {"tokens": _usage_total(entry), "kind": "usage", "model": model,
                    "window": window}
    if failed(event):
        for i in range(len(entries) - 1, -1, -1):
            entry = entries[i]
            anchor = (_counted(entry.get("context")) if entry.get("type") == "context"
                      else _usage_total(entry) if entry.get("type") == "assistant" else 0)
            if anchor:
                added = _added_chars(entries[i + 1:]) // CHARS_PER_TOKEN
                return {"tokens": anchor + added, "kind": "estimated", "model": model,
                        "window": window}
    return {"tokens": None, "kind": "none", "model": model, "window": window}


def threshold() -> int | None:
    try:
        value = int(os.environ.get("HIVE_ROTATION_THRESHOLD_TOKENS") or 0)
    except ValueError:
        return None
    return value if value > 0 else None


def _window(measured: dict) -> int | None:
    for raw in (measured.get("window"), os.environ.get("HIVE_MODEL_CONTEXT_WINDOW")):
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _entries(event: dict) -> list[dict]:
    path = dsh_transcript.resolve(event)
    if path is None:
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _started_at(entries: list[dict]) -> float | None:
    for entry in entries:
        if isinstance(entry.get("timestamp"), (int, float)):
            return float(entry["timestamp"])
    return None


def assess(event: dict) -> dict | None:
    """Measure, report, and decide. None when there is no conversation to judge."""
    conversation_id = str(event.get("session_id") or "")
    if not conversation_id:
        return None
    entries = _entries(event)
    measured = measure(event, entries)
    limit = threshold()
    window = _window(measured)
    client_type = os.environ.get("SURFACE", "")
    client_ref = os.environ.get("CLIENT_REF", "")
    comms_url, bearer = hive.comms()
    if comms_url and client_type and client_ref:
        report = {
            "client_type": client_type, "client_ref": client_ref,
            "conversation_id": conversation_id, "kind": measured["kind"],
            "tokens": int(measured["tokens"] or 0),
            "model": measured["model"] or None,
            "surface": os.environ.get("HIVE_SURFACE") or None,
        }
        if window:
            report["window"] = window
        if limit:
            report["threshold"] = limit
        try:
            hive.request("POST", f"{comms_url}/sessions/context", bearer, report)
        except Exception as exc:  # noqa: BLE001 — one stale figure, not a failed turn
            hive.log(NAME, f"context report failed sid={conversation_id}: {exc}")
    overflowed = event.get("error_code") == hive.CONTEXT_WINDOW_EXCEEDED
    rotate = bool(
        not overflowed and limit and measured["tokens"] is not None
        and (event.get("force") or measured["tokens"] >= limit)
    )
    return {"conversation_id": conversation_id, "measured": measured,
            "threshold": limit, "rotate": rotate, "client_type": client_type,
            "client_ref": client_ref, "started_at": _started_at(entries)}


def arm(assessed: dict) -> bool:
    """Arm the rotation with the gateway; stage it on disk for a pane."""
    comms_url, bearer = hive.comms()
    sid = assessed["conversation_id"]
    if not (comms_url and assessed["client_type"] and assessed["client_ref"]):
        hive.log(NAME, f"not armed sid={sid}: no gateway or no client")
        return False
    try:
        reply = hive.request("POST", f"{comms_url}/sessions/arm-rotation", bearer, {
            "client_type": assessed["client_type"], "client_ref": assessed["client_ref"],
            "conversation_id": sid, "started_at": assessed["started_at"],
        }, timeout=30)
    except Exception as exc:  # noqa: BLE001 — the next Stop over threshold arms again
        hive.log(NAME, f"arm failed sid={sid}: {exc}")
        return False
    if not reply.get("ok"):
        hive.log(NAME, f"arm refused sid={sid}: {reply}")
        return False
    if os.environ.get("HIVE_SURFACE") == "terminal":
        hive.write_marker(sid, state=hive.STATE_READY, staged_at=time.time(),
                          client_type=assessed["client_type"],
                          client_ref=assessed["client_ref"])
    hive.log(NAME, f"armed sid={sid} tokens={assessed['measured']['tokens']} "
                   f"threshold={assessed['threshold']}")
    return True


def goal_run(event: dict) -> bool:
    return os.environ.get("HIVE_GOAL_RUN") == "1" or bool(event.get("goal"))


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(event, dict) or not event.get("session_id"):
        return
    hive.load_env()
    if goal_run(event):
        # dsh reads this answer before starting the next round, so it is
        # decided here rather than in a detached child with no stdout.
        assessed = assess(event)
        if assessed and assessed["rotate"]:
            sys.stdout.write(json.dumps({"rotate": True}) + "\n")
        return
    if not hive.detach():
        return
    try:
        assessed = assess(event)
        if assessed and assessed["rotate"]:
            arm(assessed)
    except Exception as exc:  # noqa: BLE001 — a hook never fails the turn
        hive.log(NAME, f"exception: {exc!r}")
    os._exit(0)


if __name__ == "__main__":
    main()
