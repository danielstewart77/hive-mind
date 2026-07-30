"""Curation over the raw training corpus.

``training_capture`` is deliberately lossless: it stores every turn a mind
ever took, including the ones where the harness errored, the user hit escape
mid-thought, or the scheduler ran the same skill for the seven-hundredth
time. A capture layer that filtered would be a capture layer you could not
trust. Curation is therefore a separate, re-runnable pass that decides which
of those rows should reach a trainer — and, critically, it decides that *in
place* by writing verdict columns, never by deleting anything.

Every rule here answers one question: **would training on this row make the
model worse at driving the harness?**

- A malformed or truncated turn teaches broken tool-call syntax.
- An aborted turn teaches the model to stop mid-task.
- A turn that is nothing but failing tool calls teaches flailing.
- The same scheduled skill 704 times teaches the model that every prompt is
  a NetSage alert. This is the dominant defect in the corpus as captured:
  scheduled skills fire on a timer and their turns are near-identical, so
  raw token counts wildly overstate how much *distinct* behaviour is
  present.

Secrets are the exception to the "exclude it" reflex. Contaminated rows are
flagged but kept, because :mod:`core.training_redaction` scrubs at export
time and the underlying turn is usually a perfectly good demonstration of
tool use. Set ``exclude_secret_rows`` if you would rather drop them.

The pass is idempotent: running it twice produces the same verdicts, and
:func:`reset_verdicts` returns every row to ``pending`` so a changed policy
can be re-applied to the whole corpus.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.training_capture import connect
from core.training_redaction import count_secrets_in_row

FLAG_PENDING = "pending"
FLAG_KEEP = "keep"
FLAG_EXCLUDED = "excluded"

REASON_MALFORMED = "malformed_blocks"
REASON_NO_RESPONSE = "no_assistant_response"
REASON_INTERRUPTED = "interrupted"
REASON_HARNESS_ERROR = "harness_error"
REASON_TOO_SHORT = "too_short"
REASON_TOO_LONG = "too_long"
REASON_TOOL_ERRORS = "tool_error_dominant"
REASON_DUPLICATE = "near_duplicate"
REASON_SECRET = "contains_secret"

# Markers the harness itself writes into a transcript when a turn did not
# complete normally. Matched case-insensitively against the turn's text.
_ABORT_MARKERS = (
    "[request interrupted by user]",
    "[request interrupted by user for tool use]",
    "user rejected the tool call",
    "tool use was rejected",
)

_HARNESS_ERROR_MARKERS = (
    "api error:",
    "api error (request timed out",
    "internal server error",
    "overloaded_error",
    "rate_limit_error",
    "context low · run /compact",
    "prompt is too long",
)

# Substrings that mean a tool_result carried a failure rather than output.
_TOOL_ERROR_MARKERS = (
    "command not found",
    "no such file or directory",
    "permission denied",
    "traceback (most recent call last)",
    "error:",
    "<tool_use_error>",
    "exit code 1",
    "exit code 127",
)

# Case-insensitive: normalization lowercases first, so an ISO timestamp's
# ``T`` separator has already become ``t`` by the time this runs.
_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[t ][\d:.,]+(?:[+-]\d{2}:?\d{2}|z)?", re.I
)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NUM_RE = re.compile(r"\b\d[\d,._]*\b")
_WS_RE = re.compile(r"\s+")

MIGRATION = """
ALTER TABLE training_turns ADD COLUMN curation_meta TEXT
"""


@dataclass
class CurationPolicy:
    """Thresholds for one curation pass. Serialized into every run record.

    ``keep_per_cluster`` is the knob that matters most. One is too few —
    a scheduled skill genuinely is part of the job and the model should see
    it — and unbounded is how 704 NetSage runs drown out every hand-written
    debugging session in the corpus.
    """

    min_length_tokens: int = 60
    max_length_tokens: int = 32_000
    keep_per_cluster: int = 3
    max_tool_error_ratio: float = 0.6
    require_tool_call: bool = False
    exclude_secret_rows: bool = False
    harnesses: tuple[str, ...] = ()
    cluster_prefix_chars: int = 1_500

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=list)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class CurationReport:
    """Outcome of one pass. Counts only — never row content."""

    total: int = 0
    kept: int = 0
    excluded: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    clusters: int = 0
    secret_rows: int = 0
    policy_fingerprint: str = ""
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def ensure_curation_schema(db_path: str | Path) -> None:
    """Add ``curation_meta`` if an older database predates it.

    ``judge_verdict`` and ``judge_confidence`` are left alone: the schema
    reserves those for a future LLM judge, and overloading them with
    rule-engine bookkeeping would make that pass impossible to add cleanly.
    """
    with connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(training_turns)")}
        if "curation_meta" not in columns:
            conn.execute(MIGRATION)
            conn.commit()


def normalize_for_cluster(text: str, prefix_chars: int) -> str:
    """Collapse the accidental variation between two runs of one skill.

    Timestamps, UUIDs and bare numbers are what distinguish the 7am briefing
    from the 8am briefing; everything structural about the prompt is
    identical. Blanking them is what lets those two rows land in the same
    cluster.
    """
    lowered = (text or "").lower()
    lowered = _TS_RE.sub("<ts>", lowered)
    lowered = _UUID_RE.sub("<uuid>", lowered)
    lowered = _NUM_RE.sub("<n>", lowered)
    lowered = _WS_RE.sub(" ", lowered).strip()
    return lowered[:prefix_chars]


def _tool_signature(blocks: list[dict]) -> str:
    """The ordered tool names of a turn — its shape, independent of content.

    Two NetSage runs that both read the same log and post the same broker
    message share this signature. A run that instead escalated does not, and
    stays in its own cluster where it will survive collapse.
    """
    names = [b.get("name", "?") for b in blocks if b.get("type") == "tool_use"]
    return "|".join(names)


def cluster_key(user_content: str, blocks: list[dict], prefix_chars: int) -> str:
    """Cluster identity: normalized prompt plus tool-call shape."""
    prompt = normalize_for_cluster(user_content, prefix_chars)
    digest = hashlib.sha256(
        f"{prompt}\x00{_tool_signature(blocks)}".encode()
    ).hexdigest()
    return digest[:20]


def _text_of(blocks: list[dict]) -> str:
    parts: list[str] = []
    for block in blocks:
        for key in ("text", "content"):
            value = block.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _tool_error_ratio(blocks: list[dict]) -> float:
    results = [b for b in blocks if b.get("type") == "tool_result"]
    if not results:
        return 0.0
    failed = 0
    for block in results:
        content = block.get("content")
        text = content if isinstance(content, str) else json.dumps(content or "")
        lowered = text.lower()
        if any(marker in lowered for marker in _TOOL_ERROR_MARKERS):
            failed += 1
    return failed / len(results)


def _ends_cleanly(blocks: list[dict]) -> bool:
    """A complete turn ends by speaking, not by leaving a call unanswered.

    A trailing ``tool_use`` with no matching ``tool_result`` is the exact
    shape of a turn the harness never got to finish — training on it teaches
    the model to emit a call and stop.
    """
    if not blocks:
        return False
    answered = {
        b.get("tool_call_id") for b in blocks if b.get("type") == "tool_result"
    }
    for block in reversed(blocks):
        kind = block.get("type")
        if kind == "text" and (block.get("text") or "").strip():
            return True
        if kind == "tool_use":
            return block.get("id") in answered
        if kind == "tool_result":
            return True
    return False


def evaluate_row(row: sqlite3.Row, policy: CurationPolicy) -> tuple[str, str | None, dict]:
    """Apply every non-duplicate rule to one row.

    Returns ``(flag, exclusion_reason, meta)``. Duplicate collapse needs the
    whole corpus in view, so it runs afterwards in :func:`curate`.
    """
    meta: dict = {}
    raw_blocks = row["assistant_blocks"]
    try:
        blocks = json.loads(raw_blocks) if raw_blocks else []
    except (json.JSONDecodeError, TypeError):
        return FLAG_EXCLUDED, REASON_MALFORMED, meta
    if not isinstance(blocks, list):
        return FLAG_EXCLUDED, REASON_MALFORMED, meta
    if not blocks:
        # An empty array parses fine but carries no assistant behaviour at
        # all — a turn the hook captured before the response materialized.
        # Calling that "malformed" hides a real and distinct outcome.
        return FLAG_EXCLUDED, REASON_NO_RESPONSE, meta

    secrets = count_secrets_in_row(row["user_content"] or "", raw_blocks or "")
    if secrets:
        meta["secret_rules"] = sorted({hit.rule for hit in secrets})
        meta["secret_count"] = len(secrets)

    combined = f"{row['user_content'] or ''}\n{_text_of(blocks)}".lower()

    if any(marker in combined for marker in _ABORT_MARKERS):
        return FLAG_EXCLUDED, REASON_INTERRUPTED, meta
    if any(marker in combined for marker in _HARNESS_ERROR_MARKERS):
        return FLAG_EXCLUDED, REASON_HARNESS_ERROR, meta

    has_speech = any(
        b.get("type") == "text" and (b.get("text") or "").strip() for b in blocks
    )
    has_action = any(b.get("type") == "tool_use" for b in blocks)
    if not has_speech and not has_action:
        return FLAG_EXCLUDED, REASON_NO_RESPONSE, meta
    if not _ends_cleanly(blocks):
        return FLAG_EXCLUDED, REASON_INTERRUPTED, meta

    tool_calls = row["tool_call_count"] or 0
    if policy.require_tool_call and tool_calls == 0:
        return FLAG_EXCLUDED, REASON_TOO_SHORT, meta

    length = row["length_tokens"] or 0
    if length < policy.min_length_tokens and tool_calls == 0:
        return FLAG_EXCLUDED, REASON_TOO_SHORT, meta
    if length > policy.max_length_tokens:
        return FLAG_EXCLUDED, REASON_TOO_LONG, meta

    ratio = _tool_error_ratio(blocks)
    if ratio > policy.max_tool_error_ratio:
        meta["tool_error_ratio"] = round(ratio, 3)
        return FLAG_EXCLUDED, REASON_TOOL_ERRORS, meta

    if secrets and policy.exclude_secret_rows:
        return FLAG_EXCLUDED, REASON_SECRET, meta

    return FLAG_KEEP, None, meta


def _rank(row: sqlite3.Row) -> tuple:
    """Ordering within a cluster; the best exemplars are kept.

    Reasoning first, because a turn that shows its work is worth several
    that do not and only 60% of the corpus has any. Then breadth of tool
    use, then size, then recency — a recent run reflects the current shape
    of the skills and the current tool names.
    """
    return (
        row["has_reasoning"] or 0,
        row["tool_call_count"] or 0,
        row["length_tokens"] or 0,
        row["captured_at"] or 0,
    )


def curate(
    db_path: str | Path,
    policy: CurationPolicy | None = None,
) -> CurationReport:
    """Run a full curation pass, writing verdicts back to every row."""
    policy = policy or CurationPolicy()
    started = time.monotonic()
    ensure_curation_schema(db_path)

    report = CurationReport(policy_fingerprint=policy.fingerprint())
    by_reason: dict[str, int] = defaultdict(int)

    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        query = (
            "SELECT id, session_id, turn_index, harness, captured_at, user_content, "
            "assistant_blocks, has_reasoning, tool_call_count, length_tokens "
            "FROM training_turns"
        )
        params: tuple = ()
        if policy.harnesses:
            placeholders = ",".join("?" for _ in policy.harnesses)
            query += f" WHERE harness IN ({placeholders})"
            params = tuple(policy.harnesses)
        rows = conn.execute(query, params).fetchall()

        verdicts: dict[int, tuple[str, str | None, dict]] = {}
        survivors: dict[str, list[sqlite3.Row]] = defaultdict(list)

        for row in rows:
            report.total += 1
            flag, reason, meta = evaluate_row(row, policy)
            if meta.get("secret_count"):
                report.secret_rows += 1
            verdicts[row["id"]] = (flag, reason, meta)
            if flag == FLAG_KEEP:
                blocks = json.loads(row["assistant_blocks"])
                key = cluster_key(
                    row["user_content"] or "", blocks, policy.cluster_prefix_chars
                )
                meta["cluster"] = key
                survivors[key].append(row)

        report.clusters = len(survivors)

        # Duplicate collapse. Only rows that survived every other rule are
        # candidates, so a cluster never spends its budget on a broken turn.
        for key, members in survivors.items():
            if len(members) <= policy.keep_per_cluster:
                continue
            ordered = sorted(members, key=_rank, reverse=True)
            for rank, row in enumerate(ordered):
                if rank < policy.keep_per_cluster:
                    continue
                flag, _, meta = verdicts[row["id"]]
                meta["cluster_size"] = len(members)
                meta["cluster_rank"] = rank
                verdicts[row["id"]] = (FLAG_EXCLUDED, REASON_DUPLICATE, meta)

        updates = []
        for row_id, (flag, reason, meta) in verdicts.items():
            if flag == FLAG_KEEP:
                report.kept += 1
            else:
                report.excluded += 1
                by_reason[reason or "unknown"] += 1
            updates.append(
                (
                    flag,
                    reason,
                    json.dumps(meta, sort_keys=True) if meta else None,
                    row_id,
                )
            )
        conn.executemany(
            "UPDATE training_turns SET quality_flag = ?, exclusion_reason = ?, "
            "curation_meta = ? WHERE id = ?",
            updates,
        )
        conn.commit()

    report.by_reason = dict(sorted(by_reason.items(), key=lambda kv: -kv[1]))
    report.elapsed_seconds = round(time.monotonic() - started, 3)
    return report


def reset_verdicts(db_path: str | Path) -> int:
    """Return every row to ``pending``. Used when the policy changes."""
    ensure_curation_schema(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE training_turns SET quality_flag = ?, exclusion_reason = NULL, "
            "curation_meta = NULL",
            (FLAG_PENDING,),
        )
        conn.commit()
        return cursor.rowcount


def corpus_stats(db_path: str | Path) -> dict:
    """Counts for the console panel. Never returns row content."""
    ensure_curation_schema(db_path)
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) c FROM training_turns").fetchone()["c"]
        by_flag = {
            row["quality_flag"]: row["c"]
            for row in conn.execute(
                "SELECT quality_flag, COUNT(*) c FROM training_turns GROUP BY 1"
            )
        }
        by_reason = {
            row["exclusion_reason"]: row["c"]
            for row in conn.execute(
                "SELECT exclusion_reason, COUNT(*) c FROM training_turns "
                "WHERE exclusion_reason IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            )
        }
        by_harness = {
            f"{row['harness']}/reasoning={row['has_reasoning']}": row["c"]
            for row in conn.execute(
                "SELECT harness, has_reasoning, COUNT(*) c FROM training_turns "
                "GROUP BY 1, 2"
            )
        }
        kept_reasoning = conn.execute(
            "SELECT COUNT(*) c FROM training_turns WHERE quality_flag = ? "
            "AND has_reasoning = 1",
            (FLAG_KEEP,),
        ).fetchone()["c"]
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) c FROM training_turns"
        ).fetchone()["c"]
        span = conn.execute(
            "SELECT MIN(captured_at) lo, MAX(captured_at) hi FROM training_turns"
        ).fetchone()
    return {
        "total": total,
        "sessions": sessions,
        "by_flag": by_flag,
        "by_exclusion_reason": by_reason,
        "by_harness": by_harness,
        "kept_with_reasoning": kept_reasoning,
        "captured_from": span["lo"],
        "captured_to": span["hi"],
    }
