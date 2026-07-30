"""Render curated turns into a training-ready JSONL dataset.

The raw store keeps the assistant's blocks as one ordered array so that no
positional information is ever lost. A trainer wants something different: an
alternating message sequence, where each batch of tool calls is an assistant
message and each result is a tool message replying to it. This module
performs exactly that transformation, per the recipes in
``docs/training-capture/data-contract.md``.

Three decisions are expressed here.

**Reasoning is an export-time choice, not a storage one.** ``reasoning``
mode emits ``thinking`` blocks in their true position; ``stripped`` mode
drops them entirely and leaves no placeholder behind, which is the correct
input for a non-reasoning base model. There is no mode that emits an empty
thought.

**Credential handling is a three-way choice and defaults to randomizing.**
``SECRETS_RANDOMIZE`` replaces each credential with a different string of
the same length and character class, deterministically, so one secret maps
to one surrogate everywhere it appears. The model still learns the only
transferable fact — that a forty-character opaque token follows ``ghp_`` —
and never sees a real one. This is the default because it is the one policy
that survives both failure modes at once. It is also the mainstream practice
outside this repo: clinical de-identification calls it *hiding in plain
sight*, and it exists precisely because sentinel-token redaction taught
models to emit the sentinel.

``SECRETS_REDACT`` replaces credentials with placeholders. It teaches the
model that a redaction slug belongs in the credential slot, so it emits
``<REDACTED_SECRET>`` at the moment it needs a live token; reach for it only
when a dataset must provably contain no credential-shaped string.

``SECRETS_KEEP`` writes the real values. The corpus trains a model that runs
on this hardware, so this is not absurd — but extraction risk scales with
duplication and with epochs, and a small LoRA over a few thousand turns is
exactly the shape that memorizes verbatim. Choose it deliberately, never by
default.

**The split is by session, never by turn.** Turns from one session share a
system prompt, a working directory and often a literal file being edited.
Splitting by turn would put turn three in train and turn four in eval, and
the resulting eval score would measure memorization.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.training_capture import connect
from core.training_curation import FLAG_KEEP, ensure_curation_schema
from core.training_redaction import (
    randomize_blocks,
    randomize_text,
    redact_blocks,
    redact_text,
)

MODE_REASONING = "reasoning"
MODE_STRIPPED = "stripped"
VALID_MODES = frozenset({MODE_REASONING, MODE_STRIPPED})

SECRETS_KEEP = "keep"
SECRETS_RANDOMIZE = "randomize"
SECRETS_REDACT = "redact"
VALID_SECRET_POLICIES = frozenset({SECRETS_KEEP, SECRETS_RANDOMIZE, SECRETS_REDACT})

_TEXT_TRANSFORMS = {
    SECRETS_KEEP: lambda text: text,
    SECRETS_RANDOMIZE: randomize_text,
    SECRETS_REDACT: redact_text,
}
_BLOCK_TRANSFORMS = {
    SECRETS_KEEP: lambda blocks: blocks,
    SECRETS_RANDOMIZE: randomize_blocks,
    SECRETS_REDACT: redact_blocks,
}


@dataclass
class ExportOptions:
    """Shape of one export. Serialized into the run record."""

    mode: str = MODE_STRIPPED
    harnesses: tuple[str, ...] = ()
    require_reasoning: bool = False
    eval_fraction: float = 0.05
    include_system_prompt: bool = True
    max_tool_result_chars: int = 8_000
    secrets: str = SECRETS_RANDOMIZE

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        if self.secrets not in VALID_SECRET_POLICIES:
            raise ValueError(
                f"secrets must be one of {sorted(VALID_SECRET_POLICIES)}"
            )
        if not 0.0 <= self.eval_fraction < 1.0:
            raise ValueError("eval_fraction must be in [0.0, 1.0)")


@dataclass
class ExportReport:
    train_examples: int = 0
    eval_examples: int = 0
    skipped: int = 0
    train_path: str = ""
    eval_path: str = ""
    mode: str = ""
    approx_tokens: int = 0
    options: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _truncate(text: str, limit: int) -> str:
    """Cap a tool result, marking the cut so the model sees it was cut.

    A 150,000-character ``cat`` of a log file is one training example that
    costs as much as three hundred useful ones. Truncating with an explicit
    marker keeps the example while teaching that long output gets elided —
    which is what actually happens in the harness.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [{len(text) - limit} characters truncated]"


def render_turn(
    user_content: str,
    blocks: list[dict],
    options: ExportOptions,
) -> list[dict]:
    """Turn one row into an alternating message list.

    A run of consecutive ``tool_use`` blocks becomes a single assistant
    message carrying several ``tool_calls``, which is how the harness
    actually issues parallel calls — flattening them into separate messages
    would teach the model to serialize work it is allowed to batch.
    """
    blocks = _BLOCK_TRANSFORMS[options.secrets](blocks)
    scrub = _TEXT_TRANSFORMS[options.secrets]
    messages: list[dict] = [{"role": "user", "content": scrub(user_content or "")}]

    pending_text: list[str] = []
    pending_reasoning: list[str] = []
    pending_calls: list[dict] = []

    def flush_assistant() -> None:
        if not pending_text and not pending_calls and not pending_reasoning:
            return
        message: dict = {
            "role": "assistant",
            "content": "\n".join(t for t in pending_text if t.strip()),
        }
        if options.mode == MODE_REASONING and pending_reasoning:
            message["reasoning"] = "\n".join(pending_reasoning)
        if pending_calls:
            message["tool_calls"] = list(pending_calls)
        messages.append(message)
        pending_text.clear()
        pending_reasoning.clear()
        pending_calls.clear()

    for block in blocks:
        kind = block.get("type")
        if kind == "thinking":
            # A thought opens a new action group: whatever was pending
            # belongs to the previous rationale.
            if pending_calls or pending_text:
                flush_assistant()
            if options.mode == MODE_REASONING:
                pending_reasoning.append(block.get("text") or "")
        elif kind == "text":
            pending_text.append(block.get("text") or "")
        elif kind == "tool_use":
            pending_calls.append(
                {
                    "id": block.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "unknown",
                        "arguments": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
        elif kind == "tool_result":
            flush_assistant()
            content = block.get("content")
            text = content if isinstance(content, str) else json.dumps(content or "")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_call_id") or "",
                    "content": _truncate(text, options.max_tool_result_chars),
                }
            )
    flush_assistant()
    return messages


def _is_eval(session_id: str, fraction: float) -> bool:
    """Deterministic per-session split — stable across re-exports."""
    if fraction <= 0:
        return False
    digest = hashlib.sha256(session_id.encode()).hexdigest()
    return (int(digest[:8], 16) % 10_000) < int(fraction * 10_000)


def export_dataset(
    db_path: str | Path,
    out_dir: str | Path,
    options: ExportOptions | None = None,
) -> ExportReport:
    """Write ``train.jsonl`` and ``eval.jsonl`` from rows flagged ``keep``."""
    options = options or ExportOptions()
    ensure_curation_schema(db_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    train_path = out_dir / "train.jsonl"
    eval_path = out_dir / "eval.jsonl"

    report = ExportReport(
        mode=options.mode,
        train_path=str(train_path),
        eval_path=str(eval_path),
        options=asdict(options),
    )

    query = (
        "SELECT session_id, turn_index, harness, source_model, system_prompt, "
        "user_content, assistant_blocks, has_reasoning "
        "FROM training_turns WHERE quality_flag = ?"
    )
    params: list = [FLAG_KEEP]
    if options.harnesses:
        query += f" AND harness IN ({','.join('?' for _ in options.harnesses)})"
        params.extend(options.harnesses)
    if options.require_reasoning:
        query += " AND has_reasoning = 1"
    query += " ORDER BY session_id, turn_index"

    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    with train_path.open("w", encoding="utf-8") as train_f, eval_path.open(
        "w", encoding="utf-8"
    ) as eval_f:
        for row in rows:
            try:
                blocks = json.loads(row["assistant_blocks"] or "[]")
            except json.JSONDecodeError:
                report.skipped += 1
                continue
            messages = render_turn(row["user_content"] or "", blocks, options)
            if not any(m["role"] == "assistant" for m in messages):
                report.skipped += 1
                continue
            if options.include_system_prompt and row["system_prompt"]:
                messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": _TEXT_TRANSFORMS[options.secrets](
                            row["system_prompt"]
                        ),
                    },
                )
            example = {
                "messages": messages,
                "meta": {
                    "session_id": row["session_id"],
                    "turn_index": row["turn_index"],
                    "harness": row["harness"],
                    "source_model": row["source_model"],
                    "has_reasoning": bool(row["has_reasoning"]),
                },
            }
            line = json.dumps(example, ensure_ascii=False)
            report.approx_tokens += len(line) // 4
            if _is_eval(row["session_id"], options.eval_fraction):
                eval_f.write(line + "\n")
                report.eval_examples += 1
            else:
                train_f.write(line + "\n")
                report.train_examples += 1
    return report
