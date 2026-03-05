"""Archive store for world-event entries.

JSON file-backed archive store. Entries are preserved here when
archived from the active Neo4j graph. The abstraction uses a simple
class interface so the backend can be swapped to SQLite or a separate
Neo4j label later without changing call sites.

Default path: /usr/src/app/data/world_events_archive.json
"""

from __future__ import annotations

import fcntl
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ARCHIVE_PATH = Path("/usr/src/app/data/world_events_archive.json")


@dataclass
class ArchivedEntry:
    """A single archived memory entry."""

    original_id: str          # Neo4j elementId at time of archive
    content: str
    data_class: str
    tags: str
    source: str
    agent_id: str
    created_at: int           # original created_at timestamp
    archived_at: str          # ISO 8601 datetime
    original_metadata: dict   # full original node properties


class ArchiveStore:
    """JSON file-backed archive store.

    Thread-safe via fcntl.flock on every read/write cycle.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_ARCHIVE_PATH

    def save(self, entry: ArchivedEntry) -> None:
        """Append an entry to the archive file.

        Creates the file if it does not exist. Uses file locking
        to prevent corruption from concurrent writes.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        with open(self._path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                entries: list[dict] = json.loads(raw) if raw.strip() else []
                entries.append(asdict(entry))
                f.seek(0)
                f.truncate()
                json.dump(entries, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def list_all(self) -> list[ArchivedEntry]:
        """Return all archived entries. Returns [] if file missing or empty."""
        if not self._path.exists():
            return []

        try:
            with open(self._path) as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    raw = f.read()
                    entries = json.loads(raw) if raw.strip() else []
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return [ArchivedEntry(**e) for e in entries]
        except (json.JSONDecodeError, FileNotFoundError):
            logger.warning("Failed to read archive at %s", self._path)
            return []

    def get(self, original_id: str) -> ArchivedEntry | None:
        """Look up an archived entry by its original Neo4j element ID."""
        for entry in self.list_all():
            if entry.original_id == original_id:
                return entry
        return None
