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
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get("HIVE_MIND_APP_ROOT", "/usr/src/app"))

import dsh_transcript
import hive_mind_env as hive

from minds import rotation_policy

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


def _runtime_path() -> Path:
    return Path(os.environ.get("HIVE_RUNTIME_PATH") or (hive.MIND_DIR / "runtime.yaml"))


def _positive_capacity(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _proxy_window(model: str) -> int | None:
    """Look up an unspawned model through this mind's credential-filtered listing."""
    root, key = hive.proxy()
    if not root or not key or not model:
        return None
    url = f"{root}/v1/models?{urllib.parse.urlencode({'harness': 'dsh'})}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode())
    except Exception:  # noqa: BLE001 — unknown capacity disables proactive rotation
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("id") == model:
            return _positive_capacity(row.get("context_window"))
    return None


def _window(measured: dict) -> int | None:
    # A dsh context event is the strongest evidence: its harness has already
    # measured this model. Spawn metadata is usable only for that same model.
    actual = _positive_capacity(measured.get("window"))
    if actual is not None:
        return actual
    model = str(measured.get("model") or "")
    spawned_model = os.environ.get("HIVE_MODEL", "")
    spawned_harness = os.environ.get("HIVE_HARNESS", "")
    if model and model == spawned_model and spawned_harness in ("", "dsh"):
        try:
            raw = os.environ.get("HIVE_MODEL_CONTEXT_WINDOW")
            if raw and re.fullmatch(r"[0-9]+", raw):
                return _positive_capacity(int(raw))
        except ValueError:
            pass
    return _proxy_window(model)


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


def delegate_turn(event: dict) -> bool:
    """Whether this Stop is for a delegate's turn rather than the mind's.

    dsh fires every hook for a delegate's turn with the mind's environment,
    so without asking, a delegate's log over the threshold would report its
    size against the mind's conversation and arm a rotation of it. Only a
    header that says so is a delegate's: an unreadable or missing log is
    judged as the mind's, so the mind's own rotation never stops on it.
    """
    raw = str(event.get("transcript_path") or "")
    if not raw:
        return False
    origin = dsh_transcript.log_origin(Path(raw))
    if origin == "unknown":
        # Judged as the mind's. Logged, because a delegate's log too large to
        # read in time lands here too, and that case should be visible.
        hive.log(NAME, f"unreadable log judged as the mind's "
                       f"sid={event.get('session_id')} path={raw}")
    return origin == "delegate"


def assess(event: dict) -> dict | None:
    """Measure, report, and decide. None when there is no conversation to
    judge, which a delegate's turn never is."""
    conversation_id = str(event.get("session_id") or "")
    if not conversation_id or delegate_turn(event):
        return None
    entries = _entries(event)
    measured = measure(event, entries)
    policy = rotation_policy.read_policy(_runtime_path())
    percentage = policy["percentage"] if policy["status"] == "ok" else None
    window = _window(measured)
    limit = rotation_policy.effective_threshold(window, percentage)
    if policy["status"] != "ok":
        reason = policy.get("reason") or "invalid_value"
        hive.log(NAME, f"invalid_runtime_policy code=invalid_runtime_policy "
                       f"reason={reason} sid={conversation_id}")
    client_type = os.environ.get("SURFACE", "")
    client_ref = os.environ.get("CLIENT_REF", "")
    comms_url, bearer = hive.comms()
    if comms_url and client_type and client_ref:
        report = {
            "client_type": client_type, "client_ref": client_ref,
            "conversation_id": conversation_id, "kind": measured["kind"],
            "tokens": measured["tokens"],
            "model": measured["model"] or None,
            "surface": os.environ.get("HIVE_SURFACE") or None,
        }
        if window:
            report["window"] = window
        if limit is not None:
            report["threshold"] = limit
        try:
            hive.request("POST", f"{comms_url}/sessions/context", bearer, report)
        except Exception as exc:  # noqa: BLE001 — one stale figure, not a failed turn
            hive.log(NAME, f"context report failed sid={conversation_id}: {exc}")
    overflowed = event.get("error_code") == hive.CONTEXT_WINDOW_EXCEEDED
    rotate = bool(
        not overflowed and limit is not None and measured["tokens"] is not None
        and (event.get("force") or measured["tokens"] >= limit)
    )
    return {"conversation_id": conversation_id, "measured": measured,
            "threshold": limit, "rotate": rotate, "client_type": client_type,
            "client_ref": client_ref, "started_at": _started_at(entries)}


def report_death(event: dict, assessed: dict | None) -> bool:
    """Tell the gateway a terminal turn died, so its watcher is woken.

    Only a turn typed into a browser terminal needs this: a chat turn's
    death is seen by the gateway itself. Nothing is stored for it - there is
    no answer - and the gateway counts each report as a stop of its own, so each failure
    is reported once. A turn that failed only because the conversation
    overflowed is being rotated and re-sent, which is not a death: it
    reports nothing, and the re-sent turn's own end is the stop.
    """
    if os.environ.get("HIVE_SURFACE") != "terminal" or not failed(event):
        return False
    if event.get("error_code") == hive.CONTEXT_WINDOW_EXCEEDED:
        return False
    client_type = os.environ.get("SURFACE", "")
    client_ref = os.environ.get("CLIENT_REF", "")
    comms_url, bearer = hive.comms()
    if not (comms_url and client_type and client_ref):
        return False
    try:
        hive.request("POST", f"{comms_url}/sessions/record-turn", bearer, {
            "client_type": client_type, "client_ref": client_ref,
            "role": "assistant", "died": True,
            "conversation_id": str(event.get("session_id") or "") or None,
        })
    except Exception as exc:  # noqa: BLE001 — the gateway's slow check finds it
        hive.log(NAME, f"death report failed sid={event.get('session_id')}: {exc}")
        return False
    return True


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
        report_death(event, assessed)
        if assessed and assessed["rotate"]:
            sys.stdout.write(json.dumps({"rotate": True}) + "\n")
        return
    if not hive.detach():
        return
    try:
        assessed = assess(event)
        report_death(event, assessed)
        if assessed and assessed["rotate"]:
            arm(assessed)
    except Exception as exc:  # noqa: BLE001 — a hook never fails the turn
        hive.log(NAME, f"exception: {exc!r}")
    os._exit(0)


if __name__ == "__main__":
    main()
