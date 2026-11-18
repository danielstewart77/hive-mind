#!/usr/bin/env python3
"""Stop hook — capture this session into the harness fine-tuning dataset.

A thin wrapper around the consumers in the hive-mind repository
(``core.training_capture_claude`` and ``core.training_capture_codex``):

  1. Read the Stop payload (``session_id``, ``transcript_path``) from stdin.
  2. Fork a detached child so the hook returns instantly.
  3. Load the mind folder's ``.env`` for ``MIND_ID`` when the environment
     lacks it.
  4. Wait for the transcript to stop growing (Stop can fire mid-flush).
  5. Call ``capture_session``, which normalizes tool calls and upserts the
     session's rows into the shared training database.

The upsert is keyed by session, so every Stop re-reads the full transcript
and a partial one on an early Stop is harmless.

The installer registers it with the harness it runs under and the repository
it was installed from.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import role_hook_env as env  # noqa: E402

LOG = "training_capture.log"


def run(session_id: str, transcript_path: str, harness: str, app_root: Path) -> None:
    env.load_env()
    path = Path(transcript_path)
    env.wait_for_quiescence(path)
    sys.path.insert(0, str(app_root))
    try:
        if harness == "codex":
            from core.training_capture_codex import capture_session
        else:
            from core.training_capture_claude import capture_session
    except Exception as exc:
        env.log(LOG, f"import failed (app root {app_root}): {exc}")
        return
    try:
        wrote = capture_session(path, session_id=session_id,
                                mind_id=os.environ.get("MIND_ID"))
        env.log(LOG, f"session={session_id} wrote={wrote}")
    except Exception as exc:
        env.log(LOG, f"capture failed session={session_id}: {exc}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True, choices=("claude", "codex", "dsh"))
    parser.add_argument("--app-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
    except Exception:
        return
    session_id = event.get("session_id") or ""
    transcript_path = env.transcript(event, args.harness)
    if not session_id or not transcript_path:
        return
    # Detach so the hook returns instantly; the child does the heavy read.
    if not env.detach():
        return
    try:
        run(session_id, transcript_path, args.harness, args.app_root)
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
