"""Training-turn capture: storage layer.

One SQLite table, ``training_turns``, holding raw harness *turns* for
harness-fidelity fine-tuning. The dataset teaches an open model to *drive*
the Claude Code and Codex CLI harnesses — tool-call syntax, skill
invocations, structural delimiters, and the policy of when to reach for
which tool — not to reproduce a teacher's prose.

The grain is the **turn**: one human message and the assistant's complete
ordered response to it (thinking, tool calls, tool results, final text) up
to the next human message. One turn becomes exactly one row; rows reassemble
into a session by ordering on ``session_id`` then ``turn_index``. See
``docs/training-capture/data-contract.md`` for the authoritative contract.

Two properties define this layer:

- **Lossless and raw.** No sanitization, no curation, no redaction. The row
  stores exactly what the harness emitted, in order, with real tool names
  kept verbatim. Filtering, anonymization, and reasoning-stripping are
  deferred to optional export-time passes over this immutable store.
- **Upsert by ``(session_id, turn_index)``.** Each Stop-hook fire re-reads
  the full transcript and upserts every turn row. A transcript only ever
  grows, so turn rows are added or overwritten, never deleted.

This module is intentionally dumb: it persists what it is given. Transcript
parsing and turn grouping live in the per-harness consumers that call
``upsert_turns``.
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
CREATE TABLE IF NOT EXISTS training_turns (
    id                INTEGER PRIMARY KEY,
    session_id        TEXT NOT NULL,
    turn_index        INTEGER NOT NULL,
    mind_id           TEXT,
    harness           TEXT NOT NULL,
    source_model      TEXT,
    harness_version   TEXT,
    captured_at       INTEGER,
    system_prompt     TEXT,
    user_content      TEXT,
    assistant_blocks  TEXT,
    has_reasoning     INTEGER NOT NULL DEFAULT 0,
    tool_call_count   INTEGER,
    length_tokens     INTEGER,
    quality_flag      TEXT NOT NULL DEFAULT 'pending',
    judge_verdict     TEXT,
    judge_confidence  REAL,
    exclusion_reason  TEXT,
    UNIQUE(session_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_training_turns_harness
    ON training_turns (harness);
CREATE INDEX IF NOT EXISTS idx_training_turns_source_model
    ON training_turns (source_model);
CREATE INDEX IF NOT EXISTS idx_training_turns_has_reasoning
    ON training_turns (has_reasoning);
"""

# Columns written on upsert. ``id`` is autoincrement; the judge/exclusion
# columns are populated by later curation passes, not at capture time, and
# are preserved across re-capture.
_UPSERT_COLUMNS = (
    "session_id",
    "turn_index",
    "mind_id",
    "harness",
    "source_model",
    "harness_version",
    "captured_at",
    "system_prompt",
    "user_content",
    "assistant_blocks",
    "has_reasoning",
    "tool_call_count",
    "length_tokens",
)


@dataclass
class TrainingTurn:
    """One captured harness turn, ready to persist.

    ``assistant_blocks`` is the ordered block array described in the data
    contract (``thinking`` / ``text`` / ``tool_use`` / ``tool_result``); it
    is stored as JSON text. ``has_reasoning``, ``tool_call_count``, and
    ``length_tokens`` are derived from the content by :meth:`from_blocks` so
    they cannot drift from the stored blocks.
    """

    session_id: str
    turn_index: int
    harness: str
    mind_id: str | None = None
    source_model: str | None = None
    harness_version: str | None = None
    captured_at: int | None = None
    system_prompt: str | None = None
    user_content: str = ""
    assistant_blocks: list[dict] = field(default_factory=list)
    has_reasoning: bool = False
    tool_call_count: int = 0
    length_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.harness not in VALID_HARNESSES:
            raise ValueError(
                f"unknown harness {self.harness!r}; "
                f"expected one of {sorted(VALID_HARNESSES)}"
            )

    @classmethod
    def from_blocks(
        cls,
        *,
        session_id: str,
        turn_index: int,
        harness: str,
        user_content: str = "",
        assistant_blocks: list[dict] | None = None,
        **kwargs,
    ) -> "TrainingTurn":
        """Build a turn, deriving the denormalized fields from the blocks.

        ``has_reasoning`` is true when any block is a ``thinking`` block.
        ``tool_call_count`` is the number of ``tool_use`` blocks.
        ``length_tokens`` is a rough size estimate (chars / 4) over the
        ``user_content`` plus the text carried by every block.
        """
        blocks = assistant_blocks or []
        has_reasoning = any(b.get("type") == "thinking" for b in blocks)
        tool_call_count = sum(1 for b in blocks if b.get("type") == "tool_use")
        length_chars = len(user_content) + sum(
            _block_text_len(b) for b in blocks
        )
        return cls(
            session_id=session_id,
            turn_index=turn_index,
            harness=harness,
            user_content=user_content,
            assistant_blocks=blocks,
            has_reasoning=has_reasoning,
            tool_call_count=tool_call_count,
            length_tokens=length_chars // 4,
            **kwargs,
        )

    def _row_values(self) -> tuple:
        return (
            self.session_id,
            self.turn_index,
            self.mind_id,
            self.harness,
            self.source_model,
            self.harness_version,
            self.captured_at,
            self.system_prompt,
            self.user_content,
            json.dumps(self.assistant_blocks, ensure_ascii=False),
            1 if self.has_reasoning else 0,
            self.tool_call_count,
            self.length_tokens,
        )


def _block_text_len(block: dict) -> int:
    """Rough char count of a single assistant block for token estimation."""
    btype = block.get("type")
    if btype in ("thinking", "text"):
        return len(block.get("text") or "")
    if btype == "tool_use":
        return len(json.dumps(block.get("input") or {}, ensure_ascii=False))
    if btype == "tool_result":
        content = block.get("content")
        return len(content if isinstance(content, str) else str(content or ""))
    return 0


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


def upsert_turn(db_path: str | Path, turn: TrainingTurn) -> None:
    """Insert or replace one row keyed by ``(session_id, turn_index)``.

    On conflict the capture-time columns are overwritten with the latest
    transcript shape while ``id`` is preserved. The curation columns
    (``quality_flag``, ``judge_*``, ``exclusion_reason``) are left untouched
    so a re-capture of a turn does not clobber a verdict already assigned by
    a later pass.
    """
    init_db(db_path)
    placeholders = ", ".join("?" for _ in _UPSERT_COLUMNS)
    columns = ", ".join(_UPSERT_COLUMNS)
    updates = ", ".join(
        f"{col} = excluded.{col}"
        for col in _UPSERT_COLUMNS
        if col not in ("session_id", "turn_index")
    )
    sql = (
        f"INSERT INTO training_turns ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(session_id, turn_index) DO UPDATE SET {updates}"
    )
    with connect(db_path) as conn:
        conn.execute(sql, turn._row_values())


def upsert_turns(db_path: str | Path, turns: list[TrainingTurn]) -> None:
    """Upsert a list of turn rows in one connection."""
    init_db(db_path)
    placeholders = ", ".join("?" for _ in _UPSERT_COLUMNS)
    columns = ", ".join(_UPSERT_COLUMNS)
    updates = ", ".join(
        f"{col} = excluded.{col}"
        for col in _UPSERT_COLUMNS
        if col not in ("session_id", "turn_index")
    )
    sql = (
        f"INSERT INTO training_turns ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(session_id, turn_index) DO UPDATE SET {updates}"
    )
    with connect(db_path) as conn:
        conn.executemany(sql, [t._row_values() for t in turns])


def get_turns(db_path: str | Path, session_id: str) -> list[dict]:
    """Return a session's rows ordered by ``turn_index``.

    Each row is a dict with ``assistant_blocks`` JSON-decoded and
    ``has_reasoning`` coerced to a bool.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM training_turns WHERE session_id = ? "
            "ORDER BY turn_index",
            (session_id,),
        ).fetchall()
    records: list[dict] = []
    for row in rows:
        record = dict(row)
        record["assistant_blocks"] = (
            json.loads(record["assistant_blocks"])
            if record["assistant_blocks"]
            else []
        )
        record["has_reasoning"] = bool(record["has_reasoning"])
        records.append(record)
    return records


def count_turns(db_path: str | Path, harness: str | None = None) -> int:
    """Count turn rows, optionally filtered by ``harness``."""
    with connect(db_path) as conn:
        if harness is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM training_turns"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM training_turns WHERE harness = ?",
                (harness,),
            ).fetchone()
    return int(row[0])
