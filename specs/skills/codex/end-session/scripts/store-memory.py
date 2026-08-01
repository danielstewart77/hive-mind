#!/usr/bin/env python3
"""Store one already-classified durable memory for the current mind."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request


ENV_PATH = Path(
    os.environ.get("HIVE_PROJECT_DIR", "")
) / ".env"
VALID_CLASSES = {"feedback", "current-state", "future-state"}


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


def fail(message: str, code: int = 1) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return code


def main() -> int:
    data_class = sys.argv[1] if len(sys.argv) == 2 else ""
    if data_class not in VALID_CLASSES:
        return fail(
            "data class must be feedback, current-state, or future-state", 2
        )

    content = sys.stdin.read().strip()
    if not content:
        return fail("empty memory content", 2)

    load_env()
    token = os.environ.get("LUCENT_BEARER_TOKEN", "")
    mind_id = os.environ.get("MIND_ID", "")
    base_url = os.environ.get("LUCENT_URL_SELF", "http://127.0.0.1:8425")
    if not token or not mind_id:
        return fail("LUCENT_BEARER_TOKEN and MIND_ID are required")

    payload = json.dumps(
        {
            "content": content,
            "data_class": data_class,
            "tier": "contextual",
            "mind_id": mind_id,
            "source": "user",
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/memory/store",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        return fail(f"Lucent memory write failed: {exc}")

    entry_id = result.get("id")
    if not entry_id:
        return fail("Lucent response did not include a memory id")
    print(f"saved {data_class} memory {entry_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
