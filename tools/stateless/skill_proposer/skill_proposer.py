#!/usr/bin/env python3
"""Autonomous skill proposer (stateless, per-mind, default-OFF).

Closes the Phase 4 loop: the agent's recurring tool sequences become drafted
skills. The proposer is fully deterministic — it reads recent ``training_turns``
rows for ONE mind, clusters identical ordered tool-call sequences above a
conservative frequency threshold, skips clusters already covered by an existing
skill, and drafts + creates ONE SKILL.md per over-threshold cluster (capped) via
Phase 2's ``skill_manage`` ``action="create"``. There is **no model footprint**:
the draft body is a deterministic template.

Reuse, don't reinvent:
  - The DB read goes through ``core.training_capture.connect`` so the reader and
    the test fixture share the authoritative schema and cannot drift. The live
    ``data/training_turns.db`` is never required — tests build a fresh SQLite file
    in tmp via ``core.training_capture``.
  - The write goes through ``skill_manage.skill_manage(action="create", ...)``,
    which renders the harness dialect, runs the guards/threat scan, and bumps the
    telemetry sidecar (``mark_agent_created`` → ``created_by="agent"``). So every
    auto-created skill is agent-stamped and immediately Curator-eligible (Phase 3
    ``is_curation_eligible``) for free. The proposer never touches the sidecar or
    renders frontmatter itself.

Default-OFF: the proposer reads ``enabled: false`` from an optional
``<config-dir>/skills/proposer.yaml`` (mirrors ``load_curator_config``). When
disabled it returns ``{"enabled": False, "proposed": []}`` and writes nothing —
no model, no ``skill_manage`` call.

Scheduling (same as the Phase 3 curator): both Ada and Nagatha carry
``schedule:`` in their ``skill-proposer`` SKILL.md frontmatter. ``schedule:`` is
a custom field read only by our scheduler
(``core.scheduled_skills.discover_scheduled_skills``), which globs both
``.claude/skills`` and ``.codex/skills`` — neither harness reads or strips it.
The scheduler dispatches the skill as an agent turn through the mind's gateway
regardless of harness, and that agent runs this backend.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Lazy-load helpers (standalone scripts / repo package), loaded by path.
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_SKILL_MANAGE_PATH = _THIS_DIR.parent / "skill_manage" / "skill_manage.py"


def _load_skill_manage():
    spec = importlib.util.spec_from_file_location("skill_manage", str(_SKILL_MANAGE_PATH))
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise ImportError(f"cannot load skill_manage module from {_SKILL_MANAGE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_training_capture():
    """Import ``core.training_capture`` — the authoritative DB layer.

    Tries a normal package import first (works under pytest with the repo root
    on the path); falls back to loading by file path so the standalone CLI runs
    without the package installed.
    """
    try:
        from core import training_capture as tc  # type: ignore
        return tc
    except Exception:  # pragma: no cover - CLI fallback
        repo_root = _THIS_DIR.parents[2]
        path = repo_root / "core" / "training_capture.py"
        spec = importlib.util.spec_from_file_location("core.training_capture", str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load training_capture from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


# ---------------------------------------------------------------------------
# Cluster record
# ---------------------------------------------------------------------------

# Common harness-control / noise tools that should not seed a procedural name.
_NOISE_TOOL_WORDS = {"todowrite", "todoread"}

MAX_NAME_LENGTH = 64
VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass
class SequenceCluster:
    signature: Tuple[str, ...]
    count: int
    example_session_id: str
    proposed_name: str


# ---------------------------------------------------------------------------
# Step 3 — DB reader + clustering
# ---------------------------------------------------------------------------

def read_recent_sequences(
    db_path: Any,
    mind_id: str,
    *,
    harness: Optional[str] = None,
    lookback_turns: int = 500,
) -> List[Tuple[str, ...]]:
    """Read recent ``training_turns`` for *mind_id* and return per-turn ordered
    tuples of ``tool_use`` block names.

    Rows are ordered ``captured_at DESC, id DESC`` and limited to
    ``lookback_turns`` (most-recent first). Optionally filtered by ``harness``.
    Turns with no tool_use blocks are skipped. Uses
    ``core.training_capture.connect`` for schema fidelity — never assumes column
    positions.
    """
    tc = _load_training_capture()
    sql = (
        "SELECT assistant_blocks FROM training_turns "
        "WHERE mind_id = ?"
    )
    params: List[Any] = [mind_id]
    if harness is not None:
        sql += " AND harness = ?"
        params.append(harness)
    sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
    params.append(int(lookback_turns))

    sequences: List[Tuple[str, ...]] = []
    with tc.connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    for row in rows:
        raw = row["assistant_blocks"]
        if not raw:
            continue
        try:
            blocks = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(blocks, list):
            continue
        names = tuple(
            b.get("name", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")
        )
        if names:
            sequences.append(names)
    return sequences


def _derive_proposed_name(signature: Tuple[str, ...]) -> str:
    """Build a deterministic, kebab, <=64-char name from a tool signature.

    Lowercases each tool name, splits camelCase, joins distinct meaningful
    words with hyphens, and prefixes ``auto-``. Guaranteed to match
    ``VALID_NAME_RE`` and be non-empty.
    """
    words: List[str] = []
    seen = set()
    for tool in signature:
        # Split camelCase / snake / separators into words.
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+", tool)
        for p in parts:
            w = re.sub(r"[^a-z0-9]", "", p.lower())
            if not w or w in _NOISE_TOOL_WORDS or w in seen:
                continue
            seen.add(w)
            words.append(w)
    slug = "-".join(words) if words else "workflow"
    name = f"auto-{slug}"
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if not VALID_NAME_RE.match(name):
        name = "auto-workflow"
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH].rstrip("-._")
    return name


def cluster_sequences(
    sequences: List[Tuple[str, ...]],
    *,
    min_frequency: int = 3,
    min_sequence_length: int = 2,
) -> List[SequenceCluster]:
    """Count identical ordered tool tuples and return over-threshold clusters.

    Only sequences of length >= ``min_sequence_length`` are counted; only those
    recurring >= ``min_frequency`` times become clusters. Sorted by count desc
    then signature for deterministic ordering. Each cluster gets a deterministic
    kebab ``proposed_name``.
    """
    counts: Counter = Counter(
        seq for seq in sequences if len(seq) >= int(min_sequence_length)
    )
    clusters: List[SequenceCluster] = []
    for signature, count in counts.items():
        if count < int(min_frequency):
            continue
        clusters.append(
            SequenceCluster(
                signature=signature,
                count=count,
                example_session_id="",
                proposed_name=_derive_proposed_name(signature),
            )
        )
    clusters.sort(key=lambda c: (-c.count, c.signature))
    return clusters


# ---------------------------------------------------------------------------
# Step 4 — config, coverage, draft, run, CLI
# ---------------------------------------------------------------------------

DEFAULT_ENABLED = False
DEFAULT_MIN_FREQUENCY = 3
DEFAULT_MIN_SEQUENCE_LENGTH = 2
DEFAULT_LOOKBACK_TURNS = 500
DEFAULT_MAX_PROPOSALS_PER_RUN = 1


def _skills_root(config_dir: Path) -> Path:
    return Path(config_dir) / "skills"


def _proposer_config_path(config_dir: Path) -> Path:
    return _skills_root(config_dir) / "proposer.yaml"


def load_proposer_config(config_dir: Path) -> Dict[str, Any]:
    """Read ``<config-dir>/skills/proposer.yaml`` tolerantly (mirrors the
    curator's ``load_curator_config``).

    A missing or corrupt file means all defaults — crucially ``enabled=False``.
    Each field is coerced independently so one bad key falls back to its default
    rather than discarding the whole file.
    """
    cfg: Dict[str, Any] = {}
    path = _proposer_config_path(config_dir)
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, yaml.YAMLError):
            cfg = {}

    def _as_int(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(cfg.get("enabled", DEFAULT_ENABLED)),
        "min_frequency": _as_int("min_frequency", DEFAULT_MIN_FREQUENCY),
        "min_sequence_length": _as_int("min_sequence_length", DEFAULT_MIN_SEQUENCE_LENGTH),
        "lookback_turns": _as_int("lookback_turns", DEFAULT_LOOKBACK_TURNS),
        "max_proposals_per_run": _as_int(
            "max_proposals_per_run", DEFAULT_MAX_PROPOSALS_PER_RUN
        ),
    }


def existing_skill_names(config_dir: Path) -> set:
    """Names of every skill dir under ``<config-dir>/skills/`` (real dirs AND
    symlinks; skip dotdirs). A plugin skill (symlink) counts as covering work,
    so symlinks are included — mirrors the curator's one-level dir scan but
    without the symlink exclusion (coverage is broader than curation).
    """
    skills_root = _skills_root(config_dir)
    names: set = set()
    if not skills_root.is_dir():
        return names
    for entry in skills_root.iterdir():
        if entry.name.startswith("."):
            continue
        # Real dir or symlink-to-dir, with a SKILL.md.
        if not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            names.add(entry.name)
    return names


def is_covered(cluster: SequenceCluster, existing_names: set) -> bool:
    """True if an existing skill already covers this cluster.

    Covered when the cluster's deterministic ``proposed_name`` already exists —
    the signature→name map is deterministic, so a skill previously created (by
    the proposer or a human using the same derived name) matching this signature
    is detected by name equality.
    """
    return cluster.proposed_name in existing_names


def _title_from_name(name: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_.]", name) if w)


def draft_skill_md(cluster: SequenceCluster, *, harness: str) -> str:
    """Render a deterministic SKILL.md body for *cluster*.

    Frontmatter rendering is left to ``skill_manage create`` (per harness); this
    returns a complete SKILL.md (minimal frontmatter + body) that
    ``skill_manage`` re-renders. The body has ``# Title``, ``## When to Use``,
    a ``## Procedure`` with one numbered step per tool in the signature, and a
    ``## Verification``. Description is one in-bound sentence that passes the
    Step-1 validator.
    """
    name = cluster.proposed_name
    title = _title_from_name(name)
    tools = cluster.signature
    tool_list = ", ".join(tools)
    description = (
        f"Run the recurring {len(tools)}-step tool sequence "
        f"observed across {cluster.count} turns."
    )

    steps = "\n".join(
        f"{i}. Invoke `{tool}` as the next step of the sequence."
        for i, tool in enumerate(tools, start=1)
    )

    body = (
        f"# {title}\n\n"
        f"Auto-proposed skill capturing a recurring tool sequence "
        f"(`{tool_list}`) seen {cluster.count} times in recent turns. It is a "
        f"deterministic draft scaffold; refine the steps with the real commands "
        f"before relying on it. No external dependencies of its own.\n\n"
        f"## When to Use\n\n"
        f"- when you are about to run the sequence: {tool_list}\n"
        f"- when a task repeats the workflow this skill names\n\n"
        f"## Procedure\n\n"
        f"{steps}\n\n"
        f"## Verification\n\n"
        f"Confirm the sequence completed and produced the expected result.\n"
    )

    fields = {"name": name, "description": description}
    sm = _load_skill_manage()
    return sm.compose_skill_md(fields, body, harness)


def run(
    config_dir: Any,
    harness: str,
    *,
    db_path: Any,
    mind_id: str,
    now: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run one proposer pass for *mind_id*.

    When disabled (the default), returns ``{"enabled": False, "proposed": []}``
    and does nothing. When enabled: read → cluster → drop covered → cap at
    ``max_proposals_per_run`` → draft + ``skill_manage create`` each. Returns
    ``{"enabled": True, "proposed": [{name, count, created}]}``.
    """
    config_dir = Path(config_dir)
    conf = load_proposer_config(config_dir)
    if not conf["enabled"]:
        return {"enabled": False, "proposed": []}

    sequences = read_recent_sequences(
        db_path, mind_id, harness=None, lookback_turns=conf["lookback_turns"]
    )
    clusters = cluster_sequences(
        sequences,
        min_frequency=conf["min_frequency"],
        min_sequence_length=conf["min_sequence_length"],
    )

    sm = _load_skill_manage()
    proposed: List[Dict[str, Any]] = []
    created_count = 0
    for cluster in clusters:
        if created_count >= conf["max_proposals_per_run"]:
            break
        # Re-read existing names each iteration so a just-created skill counts.
        if is_covered(cluster, existing_skill_names(config_dir)):
            continue
        content = draft_skill_md(cluster, harness=harness)
        out = sm.skill_manage(
            "create", config_dir, harness,
            name=cluster.proposed_name, content=content,
        )
        result = json.loads(out)
        created = bool(result.get("success"))
        proposed.append({
            "name": cluster.proposed_name,
            "count": cluster.count,
            "created": created,
        })
        if created:
            created_count += 1

    return {"enabled": True, "proposed": proposed}


# ---------------------------------------------------------------------------
# Notify — concise on-change summary with an undo hint (best-effort)
# ---------------------------------------------------------------------------

_NOTIFY_PATH = _THIS_DIR.parent / "notify" / "notify.py"


def _created_names(summary: Dict[str, Any]) -> List[str]:
    """Names of skills the proposer actually created this run."""
    return [p["name"] for p in summary.get("proposed", []) if p.get("created")]


def compose_notify_message(config_dir: Path, summary: Dict[str, Any]) -> str:
    """Build a concise one-message proposer summary that NAMES the created
    skills and carries the undo hint.

    Each auto-created skill is agent-provenance and Curator-eligible; the message
    names them and appends the ``skill_telemetry.py --action restore`` recovery
    command (a created skill can be deleted via skill_manage and brought back the
    same way an archive is), so the recipient has the reversal syntax inline.
    """
    created = _created_names(summary)
    summary_line = "created " + ", ".join(created) if created else "created nothing"
    msg = f"Skill proposer: {summary_line}."
    if created:
        example = created[0]
        msg += (
            f" Undo with: skill_telemetry.py --action restore "
            f"--skill {example} --config-dir {config_dir}"
        )
    return msg


def maybe_notify(config_dir: Path, summary: Dict[str, Any]) -> Optional[List[str]]:
    """Shell out to the notify tool with a one-message change summary.

    No-op (returns ``None``) when nothing was created. Best-effort: a notify
    failure is swallowed so it never fails the run. When ``HERMES_NOTIFY_TEST``
    is set, ``--test-mode`` is passed so no real Telegram is sent — tests assert
    the composed message and the invoked argv. Returns the argv invoked (or that
    would have been), or ``None`` when no notification was warranted.
    """
    if not _created_names(summary):
        return None
    message = compose_notify_message(config_dir, summary)
    cmd = [sys.executable, str(_NOTIFY_PATH), "send", "--message", message]
    if os.getenv("HERMES_NOTIFY_TEST"):
        cmd.append("--test-mode")
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:  # pragma: no cover - notify is best-effort
        pass
    return cmd


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous per-mind skill proposer (default-OFF)"
    )
    parser.add_argument("--config-dir", required=True, help="Mind config dir (.claude/.codex)")
    parser.add_argument(
        "--harness", default="claude_cli", choices=["claude_cli", "codex_cli"]
    )
    parser.add_argument(
        "--db-path", default="data/training_turns.db",
        help="Path to training_turns.db (read-only)",
    )
    parser.add_argument("--mind-id", required=True, help="This mind's MIND_ID")
    parser.add_argument(
        "--notify", action="store_true",
        help="On a run that created skills, send a concise notify summary (best-effort)",
    )
    args = parser.parse_args(argv)

    summary = run(
        Path(args.config_dir), args.harness,
        db_path=args.db_path, mind_id=args.mind_id,
    )
    if args.notify:
        maybe_notify(Path(args.config_dir), summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
