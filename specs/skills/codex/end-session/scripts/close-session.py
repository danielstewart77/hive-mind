#!/usr/bin/env python3
"""Resolve this Codex thread and delete its gateway session after a delay."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


ENV_PATH = Path(
    os.environ.get("HIVE_PROJECT_DIR", "")
) / ".env"


def load_env() -> None:
    if not ENV_PATH.is_file():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def gateway_request(path: str, method: str = "GET") -> object:
    token = os.environ.get("COMMS_BEARER_TOKEN", "")
    base_url = os.environ.get("COMMS_URL", "")
    if not token or not base_url:
        raise RuntimeError("COMMS_BEARER_TOKEN and COMMS_URL are required")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
    return json.loads(body) if body else {}


def resolve_session() -> str:
    """The gateway session id for the conversation this script runs inside.

    ``HIVE_SESSION_ID`` is the gateway's own row id, stamped on the harness
    process by the mind at spawn — no lookup, no ambiguity. It is preferred
    when present. Otherwise fall back to matching the codex thread id, which
    codex exports into every shell tool call it runs (but not into hook
    processes, which read their identity off stdin instead), against the
    ``harness_sid`` the mind reported to the gateway.
    """
    session_id = os.environ.get("HIVE_SESSION_ID", "")
    if session_id:
        return session_id

    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if not thread_id:
        raise RuntimeError("neither HIVE_SESSION_ID nor CODEX_THREAD_ID is set")
    sessions = gateway_request("/sessions")
    if not isinstance(sessions, list):
        raise RuntimeError("gateway returned an invalid session list")
    matches = [
        row
        for row in sessions
        if isinstance(row, dict)
        and row.get("harness_sid") == thread_id
        and row.get("status") != "closed"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one live Codex session, found {len(matches)}")
    session_id = matches[0].get("id")
    if not session_id:
        raise RuntimeError("matched session has no id")
    return str(session_id)


def delayed_delete(session_id: str, delay: float) -> int:
    time.sleep(delay)
    gateway_request(f"/sessions/{session_id}", method="DELETE")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolve-only", action="store_true")
    parser.add_argument("--delete")
    parser.add_argument("--delay", type=float, default=8.0)
    args = parser.parse_args()
    load_env()
    try:
        if args.delete:
            return delayed_delete(args.delete, args.delay)

        session_id = resolve_session()
        if args.resolve_only:
            print(session_id)
            return 0

        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--delete",
                session_id,
                "--delay",
                str(args.delay),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        print(f"closing gateway session: {session_id}")
        return 0
    except (RuntimeError, OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
