#!/usr/bin/env python3
"""Stop hook — bump usage telemetry for skills invoked this session.

Detects which skills fired in the transcript (``core.skill_telemetry_detect``
in the hive-mind repository) and bumps their ``use_count`` and
``last_used_at`` in the sidecar ``<harness home>/skills/.usage.json``, so the
curator ages skills by real activity instead of archiving used ones.

The installer registers it with the harness it runs under and the repository
it was installed from. Best-effort and silent — a failure logs and returns,
never blocking the Stop event.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import role_hook_env as env  # noqa: E402

LOG = "skill_telemetry.log"


def _load_telemetry(app_root: Path):
    path = app_root / "tools" / "stateless" / "skill_telemetry" / "skill_telemetry.py"
    spec = importlib.util.spec_from_file_location("skill_telemetry_tool", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill_telemetry from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(transcript_path: str, harness: str, app_root: Path) -> None:
    path = Path(transcript_path)
    env.wait_for_quiescence(path)
    sys.path.insert(0, str(app_root))
    try:
        from core import skill_telemetry_detect as detect
    except Exception as exc:
        env.log(LOG, f"import detect failed (app root {app_root}): {exc}")
        return
    try:
        names = (detect.detect_codex_skills(path) if harness == "codex"
                 else detect.detect_claude_skills(path))
    except Exception as exc:
        env.log(LOG, f"detect failed: {exc}")
        return
    if not names:
        return
    try:
        bumped = _load_telemetry(app_root).bump_skills(env.HOME, names)
        env.log(LOG, f"bumped={sorted(bumped)}")
    except Exception as exc:
        env.log(LOG, f"bump failed: {exc}")


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
    transcript_path = env.transcript(event, args.harness)
    if not transcript_path:
        return
    # Detach so the hook returns instantly; the child does the heavy read.
    if not env.detach():
        return
    try:
        run(transcript_path, args.harness, args.app_root)
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
