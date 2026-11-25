"""A record of a mind's key being replaced, kept so a restart half-way is not silent.

Replacing a key changes three services and then a file. A console stopped
between two of those leaves the gateway on the new key and the memory store on
the old, with nothing on any page to say so. Each replacement is written here
before the first service is touched and removed when it ends cleanly, so the
console, on its next start, finds the ones that did not end and puts the
services back in step with the mind's stored key (see
`holder_credentials.recover_replacements`). The same record is what the
cut-off and the move of existing minds reuse.

Holds no key and no digest of one: only which mind, which services were
switched, and how it ended.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

JOURNAL_DIR = Path(os.environ.get("HIVE_KEY_JOURNAL_DIR", "/data/key-journal"))

OPEN = "started"
ENDED = frozenset({"restored", "needs_attention"})


def _path(operation_id: str) -> Path:
    return JOURNAL_DIR / (str(uuid.UUID(operation_id)) + ".json")


def _write(record: dict[str, Any]) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _path(record["operation_id"])
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def begin(kind: str, mind: str, services: list[str]) -> dict[str, Any]:
    record = {
        "operation_id": str(uuid.uuid4()), "kind": kind, "mind": mind,
        "planned": services, "switched": [], "status": OPEN,
        "detail": "", "started_at": time.time(),
    }
    _write(record)
    return record


def update(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    record.update(changes)
    _write(record)
    return record


def finish(record: dict[str, Any]) -> None:
    """The operation ended with every service and the stored key in step."""
    try:
        _path(record["operation_id"]).unlink()
    except FileNotFoundError:
        pass


def unfinished() -> list[dict[str, Any]]:
    """Records the console was stopped in the middle of, oldest first."""
    found = []
    if JOURNAL_DIR.is_dir():
        for path in sorted(JOURNAL_DIR.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if record.get("status") == OPEN:
                found.append(record)
    return sorted(found, key=lambda r: r.get("started_at", 0))


def needing_attention(mind: str | None = None) -> list[dict[str, Any]]:
    found = []
    if JOURNAL_DIR.is_dir():
        for path in sorted(JOURNAL_DIR.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if record.get("status") == "needs_attention" and (mind is None or record.get("mind") == mind):
                found.append(record)
    return found


def clear(mind: str) -> None:
    """A replacement succeeded: earlier records for this mind that needed
    attention are settled by it."""
    if JOURNAL_DIR.is_dir():
        for path in JOURNAL_DIR.glob("*.json"):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if record.get("mind") == mind:
                path.unlink(missing_ok=True)
