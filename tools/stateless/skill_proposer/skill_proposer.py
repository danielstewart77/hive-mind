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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
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

# Placeholder emitted for the varying argument slot in a templated procedure step.
_PLACEHOLDER = "{{arg}}"


@dataclass
class SequenceCluster:
    signature: Tuple[str, ...]
    count: int
    example_session_id: str
    proposed_name: str
    # Optional templated ``## Procedure`` lines (one per signature token),
    # filled by ``run`` from the cluster's member turns. Empty → generic steps.
    procedure_lines: Tuple[str, ...] = ()


@dataclass
class TurnRecord:
    """One captured turn reduced to its semantic action signal.

    ``tokens`` is the per-turn ordered tuple of semantic action tokens (see
    ``_extract_action``); ``details`` is the parallel tuple of the concrete
    invocation string each token was derived from (used for parameter
    extraction); ``succeeded`` is False only when the turn's final tool_result
    carried ``is_error`` (success gating — see ``_turn_succeeded``).
    """

    tokens: Tuple[str, ...]
    details: Tuple[str, ...]
    succeeded: bool


# ---------------------------------------------------------------------------
# Step 3a — semantic action-token extraction (name + input → action)
#
# The bare ``tool_use`` name is signal-free for harness-native minds whose work
# runs through generic ``Bash``/``Read``/``Edit``. We derive a semantic token
# from each block's name AND input so a crypto check and a docker restart no
# longer both read as "Bash". Deterministic, no model.
# ---------------------------------------------------------------------------

_CD_PREFIX_RE = re.compile(r"^\s*cd\s+\S+\s*&&\s*")
_LEADING_ASSIGN_RE = re.compile(r"^\s*(?:[A-Za-z_]\w*=[^\s;|&]+\s+)+")
_SOURCE_PREFIX_RE = re.compile(r"^\s*(?:\.|source)\s+\S+\s*(?:;|&&)?\s*")
_ENV_ASSIGN_RE = re.compile(r"(?:^|[;&|]|\s)([A-Za-z_]\w*)=([^\s;|&]+)")
_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
_TOOLSCRIPT_RE = re.compile(r"tools/stateless/(\w+)/\1\.py\b|(?:^|/|\s)(\w+)\.py\b")
_SKILL_MD_RE = re.compile(r"skills/([^/]+)/SKILL\.md$")


def _resolve_command(command: str) -> str:
    """Strip ``cd … &&`` prefixes, leading env assignments and ``.``/``source``
    lines, then substitute reused inline ``$VAR`` assignments. Returns the
    salient command with indirection removed (a ``$VAR`` with no in-command
    assignment is left intact so the caller can drop it)."""
    cmd = command.strip()
    env = {k: v for k, v in _ENV_ASSIGN_RE.findall(cmd)}
    while True:
        m = _CD_PREFIX_RE.match(cmd)
        if not m:
            break
        cmd = cmd[m.end():]
    cmd = _LEADING_ASSIGN_RE.sub("", cmd)
    cmd = _SOURCE_PREFIX_RE.sub("", cmd)

    def _sub(m: "re.Match") -> str:
        var = m.group(1) or m.group(2)
        return env.get(var, m.group(0))

    return _VAR_RE.sub(_sub, cmd).strip()


def _curl_parse(cmd: str) -> Tuple[str, str]:
    """``curl`` → ``http:<METHOD>:<first-two-path-segments>`` so distinct lucent
    ops (``memory/store`` vs ``memory/retrieve``) stay apart."""
    method = "GET"
    if re.search(r"-X\s*PUT", cmd):
        method = "PUT"
    elif re.search(r"-X\s*DELETE", cmd):
        method = "DELETE"
    elif re.search(r"-X\s*POST", cmd) or re.search(r"(?:^|\s)(?:-d|--data\b|--data-\w+)", cmd):
        method = "POST"
    m = re.search(r"https?://[^\s\"']+", cmd)
    if not m:
        return "sh:curl", cmd
    pm = re.search(r"https?://[^/\s]+/([^\s\"'?]*)", m.group(0))
    path = pm.group(1) if pm else ""
    segs = [s for s in path.split("/") if s][:2]
    return "http:" + method + ":" + "/".join(segs), cmd


def _parse_bash(command: str) -> Tuple[Optional[str], str]:
    """Return ``(token, detail)`` for a Bash command, or ``(None, "")`` to drop
    a block whose token would carry an unresolved ``$VAR`` (no signal)."""
    if not command or not command.strip():
        return "Bash", ""
    cmd = _resolve_command(command)
    if not cmd:
        return "Bash", ""
    if re.match(r"^(?:sudo\s+)?curl\b", cmd):
        return _curl_parse(cmd)
    m = _TOOLSCRIPT_RE.search(cmd)
    if m:
        tool = m.group(1) or m.group(2)
        if tool and "$" not in tool:
            return f"tool:{tool}", cmd
    parts = cmd.split()
    first = parts[0]
    if first == "sudo" and len(parts) > 1:
        first = parts[1]
    exe = os.path.basename(first)
    if not exe or "$" in exe:
        return None, ""
    return f"sh:{exe}", cmd


def _extract_action(block: Any) -> Optional[Tuple[str, str]]:
    """Map one ``tool_use`` block to ``(token, detail)``, or ``None`` to drop.

    ``Bash`` → ``tool:``/``http:``/``sh:`` (see ``_parse_bash``); ``Read``/
    ``Edit``/``Write`` → ``skill:<name>`` for a SKILL.md else ``<verb>:<ext>``;
    ``Skill`` → ``skill:<skill>``; ``Agent`` → ``agent:<subagent_type>``;
    ``mcp__*`` verbatim; anything else → the bare name (unchanged fallback)."""
    if not isinstance(block, dict):
        return None
    name = block.get("name", "")
    inp = block.get("input")
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        tok, det = _parse_bash(inp.get("command", "") or "")
        return None if tok is None else (tok, det)
    if name in ("Read", "Edit", "Write"):
        fp = inp.get("file_path", "") or ""
        if fp:
            mm = _SKILL_MD_RE.search(fp)
            if mm:
                return f"skill:{mm.group(1)}", fp
            ext = Path(fp).suffix.lstrip(".").lower()
            if ext:
                return f"{name.lower()}:{ext}", fp
        return name, fp
    if name == "Skill":
        s = inp.get("skill") or ""
        return (f"skill:{s}", s) if s else (name, "")
    if name == "Agent":
        t = inp.get("subagent_type") or ""
        return (f"agent:{t}", t) if t else (name, "")
    if name.startswith("mcp__"):
        return name, ""
    if name:
        return name, ""
    return None


def _extract_action_token(block: Any) -> Optional[str]:
    """The semantic token for one ``tool_use`` block (``None`` to drop)."""
    a = _extract_action(block)
    return a[0] if a else None


def _collapse_runs(seq: Tuple[str, ...]) -> Tuple[str, ...]:
    """Collapse consecutive identical tokens — a workflow's signal is its shape,
    not how many greps it took. Kills the ``Bash×N`` noise at the source."""
    out: List[str] = []
    for t in seq:
        if not out or out[-1] != t:
            out.append(t)
    return tuple(out)


def _collapse_pairs(
    tokens: Tuple[str, ...], details: Tuple[str, ...]
) -> List[Tuple[str, str]]:
    """Run-collapse ``(token, detail)`` pairs, keeping the first detail of a run."""
    out: List[Tuple[str, str]] = []
    for tok, det in zip(tokens, details):
        if out and out[-1][0] == tok:
            continue
        out.append((tok, det))
    return out


def _turn_succeeded(blocks: List[Any]) -> bool:
    """Success gating: a turn counts only if its final ``tool_result`` did not
    error. Rows captured before ``is_error`` existed have no flag → treated as
    successful, so gating only tightens as new turns are captured."""
    last: Optional[bool] = None
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            last = bool(b.get("is_error"))
    return last is not True


# ---------------------------------------------------------------------------
# Step 3 — DB reader + clustering
# ---------------------------------------------------------------------------

def read_recent_turn_records(
    db_path: Any,
    mind_id: str,
    *,
    harness: Optional[str] = None,
    lookback_turns: int = 500,
) -> List[TurnRecord]:
    """Read recent ``training_turns`` for *mind_id* into ``TurnRecord``s.

    Each record carries the per-turn semantic action tokens (name + input),
    the parallel concrete-invocation details, and the success verdict. Rows are
    ordered ``captured_at DESC, id DESC`` and limited to ``lookback_turns``;
    optionally filtered by ``harness``. Turns with no tool_use blocks are
    skipped. Uses ``core.training_capture.connect`` for schema fidelity."""
    tc = _load_training_capture()
    sql = "SELECT assistant_blocks FROM training_turns WHERE mind_id = ?"
    params: List[Any] = [mind_id]
    if harness is not None:
        sql += " AND harness = ?"
        params.append(harness)
    sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
    params.append(int(lookback_turns))

    records: List[TurnRecord] = []
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
        toks: List[str] = []
        dets: List[str] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                a = _extract_action(b)
                if a is None:
                    continue
                toks.append(a[0])
                dets.append(a[1])
        if toks:
            records.append(
                TurnRecord(
                    tokens=tuple(toks),
                    details=tuple(dets),
                    succeeded=_turn_succeeded(blocks),
                )
            )
    return records


def read_recent_sequences(
    db_path: Any,
    mind_id: str,
    *,
    harness: Optional[str] = None,
    lookback_turns: int = 500,
) -> List[Tuple[str, ...]]:
    """Per-turn ordered tuples of semantic action tokens (thin shim over
    ``read_recent_turn_records`` — run-collapse and success-gating are applied
    downstream in clustering / ``run``)."""
    return [
        r.tokens
        for r in read_recent_turn_records(
            db_path, mind_id, harness=harness, lookback_turns=lookback_turns
        )
    ]


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
    """Run-collapse, group by **sorted token multiset**, return over-threshold
    clusters.

    Each sequence is first run-collapsed (consecutive duplicates folded). Only
    collapsed sequences of length >= ``min_sequence_length`` are counted. They
    are grouped by their **sorted** token multiset so the same set of actions in
    a different order (``docker→cat`` vs ``cat→docker``) lands in one cluster
    instead of two under-threshold ones; the most frequent observed ordering is
    kept as the cluster ``signature`` for naming and the drafted body. Only keys
    recurring >= ``min_frequency`` times become clusters. Sorted by count desc
    then signature for deterministic ordering.
    """
    collapsed = [_collapse_runs(seq) for seq in sequences]
    collapsed = [seq for seq in collapsed if len(seq) >= int(min_sequence_length)]
    by_key: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = defaultdict(list)
    for seq in collapsed:
        by_key[tuple(sorted(seq))].append(seq)

    clusters: List[SequenceCluster] = []
    for members in by_key.values():
        count = len(members)
        if count < int(min_frequency):
            continue
        signature = Counter(members).most_common(1)[0][0]
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
# Parameter extraction — turn a confirmed cluster's member turns into a runnable
# templated body (the varying argument becomes a placeholder) instead of a stub.
# ---------------------------------------------------------------------------

def _template_from_details(details: List[str]) -> Optional[str]:
    """One template line for an action token across its member invocations.

    All-identical invocations → that literal. Varying invocations → the common
    prefix + ``{{arg}}`` + common suffix (e.g. ``reminders.py due|add|list`` →
    ``reminders.py {{arg}}``). No usable detail → ``None``."""
    vals = [d for d in details if d]
    distinct = list(dict.fromkeys(vals))
    if not distinct:
        return None
    if len(distinct) == 1:
        return distinct[0]
    prefix = os.path.commonprefix(distinct)
    suffix = os.path.commonprefix([s[::-1] for s in distinct])[::-1]
    if len(prefix) + len(suffix) >= min(len(s) for s in distinct):
        suffix = ""  # affixes overlap; keep prefix only
    return f"{prefix}{_PLACEHOLDER}{suffix}".strip()


def _extract_procedure(
    signature: Tuple[str, ...], members: List[TurnRecord]
) -> Tuple[str, ...]:
    """Render one ``## Procedure`` line per signature token from member turns,
    diffing concrete invocations into a templated (placeholdered) command."""
    pool: Dict[str, List[str]] = defaultdict(list)
    for rec in members:
        for tok, det in _collapse_pairs(rec.tokens, rec.details):
            pool[tok].append(det)
    lines: List[str] = []
    for tok in signature:
        tmpl = _template_from_details(pool.get(tok, []))
        if tmpl:
            tmpl = tmpl.replace("`", "'").replace("\n", " ").strip()
            lines.append(f"`{tmpl}`")
        else:
            lines.append(f"Invoke `{tok}`.")
    return tuple(lines)


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

    if cluster.procedure_lines:
        steps = "\n".join(
            f"{i}. {line}" for i, line in enumerate(cluster.procedure_lines, start=1)
        )
    else:
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

    records = read_recent_turn_records(
        db_path, mind_id, harness=None, lookback_turns=conf["lookback_turns"]
    )
    # Success gating: only turns that ended without a terminal error count.
    succeeded = [r for r in records if r.succeeded]
    clusters = cluster_sequences(
        [r.tokens for r in succeeded],
        min_frequency=conf["min_frequency"],
        min_sequence_length=conf["min_sequence_length"],
    )
    # Map each cluster's canonical key back to its member turns for parameter
    # extraction (the key is the sorted, run-collapsed token multiset).
    key_to_records: Dict[Tuple[str, ...], List[TurnRecord]] = defaultdict(list)
    for r in succeeded:
        key_to_records[tuple(sorted(_collapse_runs(r.tokens)))].append(r)

    sm = _load_skill_manage()
    proposed: List[Dict[str, Any]] = []
    created_count = 0
    for cluster in clusters:
        if created_count >= conf["max_proposals_per_run"]:
            break
        # Re-read existing names each iteration so a just-created skill counts.
        if is_covered(cluster, existing_skill_names(config_dir)):
            continue
        cluster.procedure_lines = _extract_procedure(
            cluster.signature,
            key_to_records.get(tuple(sorted(cluster.signature)), []),
        )
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
