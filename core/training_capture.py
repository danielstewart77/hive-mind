"""Training-example capture: storage layer.

One SQLite table, ``training_examples``, holding raw harness sessions for
harness-fidelity fine-tuning. The dataset teaches an open model to *drive*
the Claude Code and Codex CLI harnesses — tool-call syntax, skill
invocations, structural delimiters, and the policy of when to reach for
which tool — not to reproduce a teacher's prose.

Two properties define this layer:

- **Lossless and raw.** No sanitization, no curation, no redaction. The row
  stores exactly what the harness emitted. Filtering and key-substitution
  are deliberately deferred to optional export-time passes over this
  immutable store (see the spark_to_bloom backlog memo
  ``clawd-harness-finetuning.md``).
- **Upsert by ``session_id``.** Each Stop-hook fire re-reads the full
  transcript and upserts one row keyed by ``session_id``. Whatever shape the
  session has at its final turn is what persists, so the capture path does
  not depend on a ``SessionEnd`` primitive and cannot race a closing session.

This module is intentionally dumb: it persists what it is given. Transcript
parsing and tool-call normalization live in the per-harness consumers that
call ``upsert_example``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Harness identifiers stored in the ``harness`` column.
HARNESS_CLAUDE_CODE = "claude_code"
HARNESS_CODEX = "codex"
VALID_HARNESSES = frozenset({HARNESS_CLAUDE_CODE, HARNESS_CODEX})

SCHEMA = """
CREATE TABLE IF NOT EXISTS training_examples (
    id                INTEGER PRIMARY KEY,
    session_id        TEXT NOT NULL UNIQUE,
    mind_id           TEXT,
    harness           TEXT NOT NULL,
    source_model      TEXT,
    harness_version   TEXT,
    captured_at       INTEGER,
    system_prompt     TEXT,
    turns             TEXT,
    turn_count        INTEGER,
    tool_call_count   INTEGER,
    length_tokens     INTEGER,
    quality_flag      TEXT NOT NULL DEFAULT 'pending',
    judge_verdict     TEXT,
    judge_confidence  REAL,
    exclusion_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_training_examples_harness
    ON training_examples (harness);
CREATE INDEX IF NOT EXISTS idx_training_examples_source_model
    ON training_examples (source_model);
"""

# Columns written on upsert. ``id`` is autoincrement; the judge/exclusion
# columns are populated by later curation passes, not at capture time.
_UPSERT_COLUMNS = (
    "session_id",
    "mind_id",
    "harness",
    "source_model",
    "harness_version",
    "captured_at",
    "system_prompt",
    "turns",
    "turn_count",
    "tool_call_count",
    "length_tokens",
)


@dataclass
class TrainingExample:
    """One captured harness session, ready to persist.

    ``turns`` is the normalized turn array (a list of dicts); it is stored as
    JSON text. ``turn_count`` and ``tool_call_count`` are derived from it by
    :meth:`from_turns` so they cannot drift from the stored turns.
    """

    session_id: str
    harness: str
    mind_id: str | None = None
    source_model: str | None = None
    harness_version: str | None = None
    captured_at: int | None = None
    system_prompt: str | None = None
    turns: list[dict] = field(default_factory=list)
    turn_count: int = 0
    tool_call_count: int = 0
    length_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.harness not in VALID_HARNESSES:
            raise ValueError(
                f"unknown harness {self.harness!r}; "
                f"expected one of {sorted(VALID_HARNESSES)}"
            )

    @classmethod
    def from_turns(
        cls,
        *,
        session_id: str,
        harness: str,
        turns: list[dict],
        **kwargs,
    ) -> "TrainingExample":
        """Build an example, deriving the counts from ``turns``.

        ``turn_count`` is the number of turns. ``tool_call_count`` sums the
        ``tool_calls`` entries across all turns, which is the metric that
        matters for prioritizing tool-heavy sessions.
        """
        tool_calls = sum(len(t.get("tool_calls") or []) for t in turns)
        return cls(
            session_id=session_id,
            harness=harness,
            turns=turns,
            turn_count=len(turns),
            tool_call_count=tool_calls,
            **kwargs,
        )

    def _row_values(self) -> tuple:
        return (
            self.session_id,
            self.mind_id,
            self.harness,
            self.source_model,
            self.harness_version,
            self.captured_at,
            self.system_prompt,
            json.dumps(self.turns, ensure_ascii=False),
            self.turn_count,
            self.tool_call_count,
            self.length_tokens,
        )


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with a ``Row`` factory and foreign keys on."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create the table and indexes if they do not already exist."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def upsert_example(db_path: str | Path, example: TrainingExample) -> None:
    """Insert or replace a row keyed by ``session_id``.

    On conflict the capture-time columns are overwritten with the latest
    transcript shape while ``id`` is preserved. The curation columns
    (``quality_flag``, ``judge_*``, ``exclusion_reason``) are left untouched
    so a re-capture of a session does not clobber a verdict already assigned
    by a later pass.
    """
    init_db(db_path)
    placeholders = ", ".join("?" for _ in _UPSERT_COLUMNS)
    columns = ", ".join(_UPSERT_COLUMNS)
    updates = ", ".join(
        f"{col} = excluded.{col}"
        for col in _UPSERT_COLUMNS
        if col != "session_id"
    )
    sql = (
        f"INSERT INTO training_examples ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(session_id) DO UPDATE SET {updates}"
    )
    with connect(db_path) as conn:
        conn.execute(sql, example._row_values())


def get_example(db_path: str | Path, session_id: str) -> dict | None:
    """Return one row as a dict (with ``turns`` decoded), or ``None``."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM training_examples WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["turns"] = json.loads(record["turns"]) if record["turns"] else []
    return record


def count_examples(db_path: str | Path, harness: str | None = None) -> int:
    """Count rows, optionally filtered by ``harness``."""
    with connect(db_path) as conn:
        if harness is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM training_examples"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM training_examples WHERE harness = ?",
                (harness,),
            ).fetchone()
    return int(row[0])
