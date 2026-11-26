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


def is_open(record: dict[str, Any]) -> bool:
    """The record is still on disk and unfinished (a snapshot may be stale)."""
    try:
        return json.loads(_path(record["operation_id"]).read_text()).get("status") == OPEN
    except (OSError, ValueError):
        return False


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
    """Records the person has to look at: a put-back that failed, or a
    recovery on start that could not finish (it is tried again on every start)."""
    found = []
    if JOURNAL_DIR.is_dir():
        for path in sorted(JOURNAL_DIR.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if record.get("detail") and (mind is None or record.get("mind") == mind):
                found.append(record)
    return found


def open_for(mind: str) -> bool:
    """An earlier operation on this mind was left unfinished or needs attention."""
    return any(r.get("kind") == "replace"
               for r in needing_attention(mind) + [u for u in unfinished() if u.get("mind") == mind])


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


# A mind whose key is not kept on this console (one on another machine) is
# registered by pasting its key on its page: the three services take it and
# this console never stores it. That is the one moment the console can know such
# a mind was moved, so it keeps a plain record of it (the mind's name, no key,
# no digest) beside the journal, which is the same volume.
_REGISTERED_DIR = "registered-not-stored"


def _registered_path(mind: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in mind.strip().lower())
    return JOURNAL_DIR / _REGISTERED_DIR / safe


def mark_registered_not_stored(mind: str) -> None:
    path = _registered_path(mind)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text("registered, key not stored here\n")


def registered_not_stored(mind: str) -> bool:
    try:
        return _registered_path(mind).is_file()
    except OSError:
        return False


def unmark_registered_not_stored(mind: str) -> None:
    try:
        _registered_path(mind).unlink(missing_ok=True)
    except OSError:
        pass
