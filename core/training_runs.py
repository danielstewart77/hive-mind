"""Ledger of curation, export and fine-tune runs.

The console needs to answer three questions without reading the corpus:
what has been run, what did it produce, and what is running now. That is a
ledger, and it lives in its own database rather than as more columns on
``training_turns`` — a run is about the *corpus*, not about any one turn,
and the capture layer must stay free to upsert turn rows without ever
colliding with pipeline bookkeeping.

Runs are append-only. A finished run is never edited except to move it to a
terminal status and attach its report, so the history of what policy
produced which dataset stays intact after the policy changes.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

KIND_CURATE = "curate"
KIND_EXPORT = "export"
KIND_TRAIN = "train"
VALID_KINDS = frozenset({KIND_CURATE, KIND_EXPORT, KIND_TRAIN})

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})

SCHEMA = """
CREATE TABLE IF NOT EXISTS training_runs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER,
    options       TEXT,
    report        TEXT,
    artifact_path TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_training_runs_kind ON training_runs(kind);
CREATE INDEX IF NOT EXISTS idx_training_runs_started ON training_runs(started_at DESC);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@dataclass
class Run:
    id: str
    kind: str
    status: str
    started_at: int
    finished_at: int | None = None
    options: dict | None = None
    report: dict | None = None
    artifact_path: str | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        return cls(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            options=json.loads(row["options"]) if row["options"] else None,
            report=json.loads(row["report"]) if row["report"] else None,
            artifact_path=row["artifact_path"],
            error=row["error"],
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "options": self.options,
            "report": self.report,
            "artifact_path": self.artifact_path,
            "error": self.error,
            "duration_seconds": (
                self.finished_at - self.started_at if self.finished_at else None
            ),
        }


def start_run(db_path: str | Path, kind: str, options: dict | None = None) -> str:
    """Open a run and return its id."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown run kind {kind!r}; expected one of {sorted(VALID_KINDS)}")
    init_db(db_path)
    run_id = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO training_runs (id, kind, status, started_at, options) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                kind,
                STATUS_RUNNING,
                int(time.time()),
                json.dumps(options or {}, sort_keys=True, default=str),
            ),
        )
        conn.commit()
    return run_id


def finish_run(
    db_path: str | Path,
    run_id: str,
    *,
    status: str,
    report: dict | None = None,
    artifact_path: str | None = None,
    error: str | None = None,
) -> None:
    """Close a run. Only a running run may be closed."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"{status!r} is not a terminal status")
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE training_runs SET status = ?, finished_at = ?, report = ?, "
            "artifact_path = ?, error = ? WHERE id = ? AND status = ?",
            (
                status,
                int(time.time()),
                json.dumps(report, default=str) if report else None,
                artifact_path,
                error,
                run_id,
                STATUS_RUNNING,
            ),
        )
        conn.commit()


def get_run(db_path: str | Path, run_id: str) -> Run | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM training_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return Run.from_row(row) if row else None


def list_runs(
    db_path: str | Path, kind: str | None = None, limit: int = 50
) -> list[Run]:
    init_db(db_path)
    query = "SELECT * FROM training_runs"
    params: list = []
    if kind:
        query += " WHERE kind = ?"
        params.append(kind)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with connect(db_path) as conn:
        return [Run.from_row(row) for row in conn.execute(query, params)]


def latest_run(db_path: str | Path, kind: str) -> Run | None:
    runs = list_runs(db_path, kind=kind, limit=1)
    return runs[0] if runs else None


def reap_stale_runs(db_path: str | Path, older_than_seconds: int = 6 * 3600) -> int:
    """Fail runs that outlived any plausible execution.

    A pipeline process killed mid-run leaves its row ``running`` forever,
    and a stuck run is indistinguishable from a live one in the console.
    Ageing them out keeps the panel honest without inventing a heartbeat.
    """
    init_db(db_path)
    cutoff = int(time.time()) - older_than_seconds
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE training_runs SET status = ?, finished_at = ?, error = ? "
            "WHERE status = ? AND started_at < ?",
            (
                STATUS_FAILED,
                int(time.time()),
                "run exceeded the maximum runtime and was reaped",
                STATUS_RUNNING,
                cutoff,
            ),
        )
        conn.commit()
        return cursor.rowcount
