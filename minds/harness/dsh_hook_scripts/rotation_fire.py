#!/usr/bin/env python3
"""UserPromptSubmit hook: fire a staged terminal rotation on a typed prompt.

A pane is a raw byte bridge and never reaches the gateway's chat path, so a
rotation ``rotation_check`` armed for it is fired from here: the prompt the
user just typed goes to ``POST /sessions/fire-rotation`` and the gateway
respawns the pane onto a fresh conversation that opens on the handover and
that prompt.

It fires only on a prompt a person typed — the bridge reports each message's
source in ``prompt_sources``, and a background notice or a hook's injected
context is not a turn to rotate on. It never holds the prompt: the request
runs in a detached child, because the respawn it triggers kills this pane's
process group, and the message meanwhile reaches the harness running now.
One claim per staged rotation, so two queued prompts cannot both fire it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hive_mind_env as hive  # noqa: E402

NAME = "rotation_fire"

#: What the dsh bridge calls a message a person typed.
TYPED_SOURCE = "user"

#: Envelope tags of text the harness submits on nobody's behalf, for a
#: payload that reports no sources.
INJECTED_PREFIXES = ("<task-notification", "<system-reminder", "<command-name",
                     "<local-command-")


def typed(event: dict) -> bool:
    sources = event.get("prompt_sources")
    if isinstance(sources, list) and sources:
        return all(source == TYPED_SOURCE for source in sources)
    prompt = str(event.get("prompt") or "").lstrip()
    return bool(prompt) and not prompt.startswith(INJECTED_PREFIXES)


def claim(conversation_id: str) -> dict | None:
    """Take the staged rotation for firing; None when there is none to take."""
    marker = hive.read_marker(conversation_id)
    if marker is None:
        return None
    if marker.get("state") == hive.STATE_FIRING and (
        time.time() - float(marker.get("updated_at") or 0) < hive.FIRE_LEASE_SECONDS
    ):
        return None
    marker["state"] = hive.STATE_FIRING
    hive.write_marker(conversation_id, **{k: v for k, v in marker.items()
                                         if k not in ("conversation_id", "updated_at")})
    return marker


def fire(marker: dict, conversation_id: str, prompt: str) -> bool:
    """Ask the gateway to swap the pane; settle the marker on its answer."""
    comms_url, bearer = hive.comms()
    try:
        reply = hive.request("POST", f"{comms_url}/sessions/fire-rotation", bearer, {
            "client_type": marker.get("client_type") or "",
            "client_ref": marker.get("client_ref") or "",
            "conversation_id": conversation_id,
            "prompt": prompt,
        }, timeout=60)
    except Exception as exc:  # noqa: BLE001 — still staged; the next prompt retries
        reply = {"ok": False, "error": str(exc)}
    if reply.get("rotated"):
        hive.clear_marker(conversation_id)
        hive.log(NAME, f"fired sid={conversation_id}")
        return True
    hive.write_marker(conversation_id, **{**{k: v for k, v in marker.items()
                                            if k not in ("conversation_id", "updated_at")},
                                          "state": hive.STATE_READY})
    hive.log(NAME, f"not fired sid={conversation_id}: {reply}")
    return False


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(event, dict) or os.environ.get("HIVE_SURFACE") != "terminal":
        return
    conversation_id = str(event.get("session_id") or "")
    if not conversation_id or not typed(event):
        return
    hive.load_env()
    marker = claim(conversation_id)
    if marker is None:
        return
    if not hive.detach():
        return
    try:
        fire(marker, conversation_id, str(event.get("prompt") or ""))
    except Exception as exc:  # noqa: BLE001 — a hook never fails the prompt
        hive.log(NAME, f"exception: {exc!r}")
    os._exit(0)


if __name__ == "__main__":
    main()
