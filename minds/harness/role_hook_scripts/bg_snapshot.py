#!/usr/bin/env python3
"""Stop / SubagentStop hook: record what is still running in this session.

A staged rotation must not fire while a background agent or shell is alive —
the respawn kills the pane's process group and takes them with it. The
harness already knows: ``background_tasks`` rides on every Stop and
SubagentStop payload, one entry per backgrounded shell and subagent, each
carrying a live status.

UserPromptSubmit does not carry it, so it is written here for the fire hook
to read. That is not a stale reading by the time it is used: a background
task finishing is injected as a prompt, which produces an assistant turn and
therefore another Stop, which rewrites this file — and the Stop after the
last one drains carries an empty list.

Separate from ``rotation_check.py`` on purpose. That hook forks a detached
child that spends minutes on Ollama calls before it writes anything, and it
returns early on every Stop under the rotation threshold — which is most of
them, and exactly the ones during which background work is launched.

Only a terminal writes one. This hook runs on every Stop of every session on
the host — the operator's own CLI, each Telegram-driven spawn — and only a
pane has a staged rotation to hold back, so anything else would be a file
written for a reader that will never come and never deleted.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rotation_state as state  # noqa: E402


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if os.environ.get("HIVE_SURFACE") != "terminal":
        return
    conversation_id = event.get("session_id") or ""
    if not conversation_id:
        return
    state.record_background_tasks(conversation_id, event.get("background_tasks") or [])
    state.sweep_dead_state()


if __name__ == "__main__":
    main()
