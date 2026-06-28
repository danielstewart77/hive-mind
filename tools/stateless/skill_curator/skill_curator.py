#!/usr/bin/env python3
"""Deterministic skill-lifecycle Curator (stateless, per-mind).

Ported from Hermes' ``agent/curator.py::apply_automatic_transitions`` and
re-shaped for hive_mind's stateless, multi-mind, copy-don't-share model. Where
Hermes resolves a single ``~/.hermes/skills`` root from global config and splits
provenance via bundled/hub manifest files, this tool takes the mind's config dir
as an explicit ``--config-dir`` (its ``.claude`` / ``.codex`` directory) and uses
the **local, two-part** eligibility signal of design-decision D2:

    A skill is curation-eligible iff its directory is a *real dir (not a
    symlink)* AND its sidecar record carries ``created_by == "agent"`` AND its
    name is not one of the protected router skills.

Plugin skills arrive as symlinks (``plugin_skills_sync.sh``) and are seeded
``created_by="human"`` by Phase 1's backfill — both gates exclude them. We
therefore do **not** port Hermes' bundled/hub manifest machinery, suppression
lists, or ``prune_builtins`` flag.

The Curator does two things:

  1. **Deterministic transitions (3.1, primary)** —
     ``apply_automatic_transitions`` walks every eligible skill and moves it
     ``active → stale @stale_after_days → archived @archive_after_days`` based on
     the latest real activity timestamp, reactivates ``stale → active`` when used
     again, and **never deletes**. Pinned skills are exempt. The archive
     transition *keeps* the sidecar record (``set_state(archived)``) so a stale
     skill stays counted and recoverable — distinct from ``skill_manage delete``
     which forgets the record.

  2. **LLM consolidation (3.2, dormant)** — a ported ``CURATOR_REVIEW_PROMPT``
     umbrella-building pass gated behind ``consolidate: false`` (default). Ships
     structurally complete but spawns no model under the default.

Config (per-mind, optional) lives at ``<config-dir>/skills/curator.yaml``; an
absent file means all defaults. The ``min_idle_hours`` gate is parsed for parity
but is a **no-op** in the hive_mind scheduled-subprocess model — there is no
"agent actively running" signal reachable from a stateless CLI, so the cadence
gate is the cron schedule itself.

A run report is written to ``<config-dir>/skills/.curator_state`` (JSON):
``last_run_at`` plus the four counters ``{checked, marked_stale, archived,
reactivated}``.

Telemetry I/O is reused from the Phase 1 sidecar
(``tools/stateless/skill_telemetry/skill_telemetry.py``), loaded by absolute path
via ``importlib`` since this is a standalone script.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Reuse the Phase 1 telemetry sidecar — load the standalone script by path.
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_TELEMETRY_PATH = _THIS_DIR.parent / "skill_telemetry" / "skill_telemetry.py"


def _load_telemetry():
    spec = importlib.util.spec_from_file_location("skill_telemetry", str(_TELEMETRY_PATH))
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise ImportError(f"cannot load telemetry module from {_TELEMETRY_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


telemetry = _load_telemetry()

# Re-use the canonical state constants so curator + telemetry never drift.
STATE_ACTIVE = telemetry.STATE_ACTIVE
STATE_STALE = telemetry.STATE_STALE
STATE_ARCHIVED = telemetry.STATE_ARCHIVED

# hive_mind's analogue of Hermes' PROTECTED_BUILTIN_SKILLS = {"plan"}: the five
# verified router skills (each SKILL.md frontmatter reads "Route all …"). These
# are hard-exempt regardless of any flag or sidecar state — verified present on
# disk under minds/ada/.claude/skills/. No names are invented; this is the
# literal on-disk router family.
PROTECTED_ROUTER_SKILLS = frozenset(
    {"software", "operations", "planning", "information", "communication"}
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _skills_root(config_dir: Path) -> Path:
    return Path(config_dir) / "skills"


# ---------------------------------------------------------------------------
# Eligibility — D2: real-dir-not-symlink AND created_by=="agent" AND not router
# ---------------------------------------------------------------------------

def is_curation_eligible(config_dir: Path, name: str, record: Dict[str, Any]) -> bool:
    """Return True iff *name* is curation-eligible per D2.

    Three gates, all local (no manifest files):
      - the on-disk skill dir is a real dir, not a symlink (plugin skills are
        symlinks and are excluded);
      - the sidecar record carries ``created_by == "agent"`` (human/plugin
        skills are excluded);
      - the name is not a protected router skill.
    """
    if name in PROTECTED_ROUTER_SKILLS:
        return False
    if record.get("created_by") != "agent":
        return False
    skill_dir = _skills_root(config_dir) / name
    if os.path.islink(str(skill_dir)):
        return False
    if not skill_dir.is_dir():
        return False
    return True


def eligible_skill_rows(config_dir: Path) -> List[Dict[str, Any]]:
    """Walk ``<config-dir>/skills/*/SKILL.md`` one level deep and return the
    eligible rows, each joined with its sidecar record.

    Skips dotdirs (e.g. ``.archive``) and symlinked skill dirs. Each returned
    row is ``{name, **record, _persisted, last_activity_at}`` with missing
    record keys backfilled from ``telemetry._empty_record``. Only rows passing
    ``is_curation_eligible`` are returned.
    """
    skills_root = _skills_root(config_dir)
    if not skills_root.is_dir():
        return []

    data = telemetry.load_usage(config_dir)
    rows: List[Dict[str, Any]] = []
    for entry in sorted(skills_root.iterdir()):
        name = entry.name
        if name.startswith("."):
            continue
        # A symlinked skill dir is an externally-owned plugin skill (D2).
        if os.path.islink(str(entry)):
            continue
        if not entry.is_dir():
            continue
        if not (entry / "SKILL.md").is_file():
            continue

        raw = data.get(name)
        persisted = isinstance(raw, dict)
        rec: Dict[str, Any] = raw if isinstance(raw, dict) else telemetry._empty_record()
        base = telemetry._empty_record()
        for k, v in base.items():
            rec.setdefault(k, v)

        if not is_curation_eligible(config_dir, name, rec):
            continue

        row = {"name": name, **rec, "_persisted": persisted}
        row["last_activity_at"] = telemetry.latest_activity_at(row)
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Config — defaults straight from Hermes; optional <config-dir>/skills/curator.yaml
# ---------------------------------------------------------------------------

DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_ARCHIVE_AFTER_DAYS = 90
DEFAULT_MIN_IDLE_HOURS = 2
DEFAULT_CONSOLIDATE = False


def _curator_config_path(config_dir: Path) -> Path:
    return _skills_root(config_dir) / "curator.yaml"


def load_curator_config(config_dir: Path) -> Dict[str, Any]:
    """Read ``<config-dir>/skills/curator.yaml`` tolerantly.

    A missing or corrupt file means all defaults. Each field is coerced
    independently so a single bad key falls back to its default rather than
    discarding the whole file.

    ``min_idle_hours`` is parsed for parity with Hermes but is a **no-op** in the
    hive_mind scheduled-subprocess model — there is no "agent actively running"
    signal reachable from a stateless CLI, so the cadence gate is the cron
    schedule itself, not this value.
    """
    cfg: Dict[str, Any] = {}
    path = _curator_config_path(config_dir)
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

    def _as_float(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "stale_after_days": _as_int("stale_after_days", DEFAULT_STALE_AFTER_DAYS),
        "archive_after_days": _as_int("archive_after_days", DEFAULT_ARCHIVE_AFTER_DAYS),
        "min_idle_hours": _as_float("min_idle_hours", DEFAULT_MIN_IDLE_HOURS),
        "consolidate": bool(cfg.get("consolidate", DEFAULT_CONSOLIDATE)),
    }


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# archive_skill — move dir + set_state(archived). KEEPS the record (not forget).
# ---------------------------------------------------------------------------

def _find_live_skill_dir(config_dir: Path, name: str) -> Optional[Path]:
    """Locate the real (non-archive) skill dir one level under skills/<name>/.

    Returns None when the dir is absent or is a symlink (plugin skill — never
    archive it). The Curator only walks one level deep (no category nesting),
    so this is a direct child lookup, not an rglob.
    """
    skill_dir = _skills_root(config_dir) / name
    if os.path.islink(str(skill_dir)):
        return None
    if not skill_dir.is_dir():
        return None
    if not (skill_dir / "SKILL.md").is_file():
        return None
    return skill_dir


def _validate_archive_target(config_dir: Path, skill_dir: Path) -> Optional[str]:
    """Guard before moving: refuse the skills root itself or out-of-root paths.

    Mirrors ``skill_manage._validate_delete_target`` (minus the symlink branch,
    already handled by ``_find_live_skill_dir``). Returns an error string to
    refuse on, or None when safe.
    """
    try:
        resolved = skill_dir.resolve()
        root = _skills_root(config_dir).resolve()
    except OSError as exc:  # pragma: no cover - defensive
        return f"could not resolve path ({exc})"
    if resolved == root:
        return "path resolves to the skills root itself"
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return "path is outside the skills root"
    if not rel.parts:
        return "resolves to the skills root"
    return None


def archive_skill(config_dir: Path, name: str) -> Tuple[bool, Optional[str]]:
    """Move ``skills/<name>/`` to ``skills/.archive/<name>/`` and stamp the
    record archived — **keeping** the sidecar entry (Hermes' ``archive_skill``
    ends in ``set_state(STATE_ARCHIVED)``, not ``forget``).

    On collision the destination gets a ``%Y%m%dT%H%M%S%f`` timestamp suffix so
    an existing archive entry is never clobbered. Symlinked or missing skills are
    refused. Returns ``(ok, dest)`` — ``dest`` is the archive path on success,
    ``None`` on refusal. Never ``rmtree``, never ``forget``.
    """
    skill_dir = _find_live_skill_dir(config_dir, name)
    if skill_dir is None:
        return False, None

    unsafe = _validate_archive_target(config_dir, skill_dir)
    if unsafe:
        return False, None

    archive_root = _skills_root(config_dir) / ".archive"
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - defensive
        return False, None

    dest = archive_root / name
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        dest = archive_root / f"{name}-{stamp}"

    try:
        shutil.move(str(skill_dir), str(dest))
    except Exception:  # pragma: no cover - defensive
        return False, None

    telemetry.set_state(config_dir, name, STATE_ARCHIVED)
    return True, str(dest)


# ---------------------------------------------------------------------------
# Deterministic transitions (pure, no LLM) — the 3.1 core
# ---------------------------------------------------------------------------

def apply_automatic_transitions(
    config_dir: Path,
    *,
    now: Optional[datetime] = None,
    stale_after_days: Optional[int] = None,
    archive_after_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Walk every eligible skill and move active/stale/archived based on the
    latest real activity timestamp. Pinned skills are never touched.

    Ported from Hermes' ``curator.py::apply_automatic_transitions`` with the
    transition arithmetic verbatim. The ``seeded`` branch is dropped (D2 has no
    unpersisted built-ins — every eligible row is agent-created and already has a
    record). When ``stale_after_days`` / ``archive_after_days`` are not passed
    they are read from ``<config-dir>/skills/curator.yaml``. ``now`` is injectable
    so tests seed a deterministic clock.

    Returns the counter dict ``{checked, marked_stale, archived, reactivated}``
    plus an ``events`` list — one ``{name, from_state, to_state, action}`` per
    transition actually made (``action`` ∈ ``stale`` / ``archive`` /
    ``reactivate``). The named events make each run observable and let the audit
    ledger record exactly what moved.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if stale_after_days is None or archive_after_days is None:
        conf = load_curator_config(config_dir)
        if stale_after_days is None:
            stale_after_days = conf["stale_after_days"]
        if archive_after_days is None:
            archive_after_days = conf["archive_after_days"]

    stale_cutoff = now - timedelta(days=stale_after_days)
    archive_cutoff = now - timedelta(days=archive_after_days)

    counts: Dict[str, Any] = {
        "marked_stale": 0, "archived": 0, "reactivated": 0, "checked": 0,
    }
    events: List[Dict[str, Any]] = []

    for row in eligible_skill_rows(config_dir):
        counts["checked"] += 1
        name = row["name"]
        if row.get("pinned"):
            continue

        last_activity = _parse_iso(row.get("last_activity_at"))
        # Never active ⇒ anchor on created_at so new skills don't immediately
        # archive themselves from epoch.
        anchor = last_activity or _parse_iso(row.get("created_at")) or now
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)

        current = row.get("state", STATE_ACTIVE)

        if anchor <= archive_cutoff and current != STATE_ARCHIVED:
            ok, _dest = archive_skill(config_dir, name)
            if ok:
                counts["archived"] += 1
                events.append({
                    "name": name, "from_state": current,
                    "to_state": STATE_ARCHIVED, "action": "archive",
                })
        elif anchor <= stale_cutoff and current == STATE_ACTIVE:
            telemetry.set_state(config_dir, name, STATE_STALE)
            counts["marked_stale"] += 1
            events.append({
                "name": name, "from_state": STATE_ACTIVE,
                "to_state": STATE_STALE, "action": "stale",
            })
        elif anchor > stale_cutoff and current == STATE_STALE:
            telemetry.set_state(config_dir, name, STATE_ACTIVE)
            counts["reactivated"] += 1
            events.append({
                "name": name, "from_state": STATE_STALE,
                "to_state": STATE_ACTIVE, "action": "reactivate",
            })

    counts["events"] = events
    return counts


# ---------------------------------------------------------------------------
# 3.2 consolidation pass — wired but default-off (consolidate: false)
# ---------------------------------------------------------------------------

# The absorbed-skill archiver is Phase 2's skill_manage delete --absorbed-into
# — NOT a duplicate of the Curator's own archive_skill. When the consolidation
# worker merges a sibling into an umbrella it drops the sidecar record (the
# content lives on in the umbrella), which is exactly what skill_manage delete
# does. The deterministic 3.1 pass keeps records (archive_skill); the 3.2 pass
# forgets them (skill_manage delete). These are intentionally different.
ABSORBED_ARCHIVE_COMMAND = "skill_manage delete --absorbed-into <umbrella>"


# Ported verbatim from Hermes' agent/curator.py CURATOR_REVIEW_PROMPT, with two
# adaptations: rule 3b names hive_mind's five router skills (not Hermes' "plan"),
# and every ``~/.hermes/skills/`` path is rewritten to ``<config-dir>/skills/``.
CURATOR_REVIEW_PROMPT = (
    "You are running as the background skill CURATOR. This is an "
    "UMBRELLA-BUILDING consolidation pass, not a passive audit and not a "
    "duplicate-finder.\n\n"
    "The goal of the skill collection is a LIBRARY OF CLASS-LEVEL "
    "INSTRUCTIONS AND EXPERIENTIAL KNOWLEDGE. A collection of hundreds of "
    "narrow skills where each one captures one session's specific bug is "
    "a FAILURE of the library — not a feature. An agent searching skills "
    "matches on descriptions, not on exact names; one broad umbrella "
    "skill with labeled subsections beats five narrow siblings for "
    "discoverability, not the other way around.\n\n"
    "The right target shape is CLASS-LEVEL skills with rich SKILL.md "
    "bodies + `references/`, `templates/`, and `scripts/` subfiles for "
    "session-specific detail — not one-session-one-skill micro-entries.\n\n"
    "Hard rules — do not violate:\n"
    "1. DO NOT touch plugin (symlinked) skills. The candidate list "
    "below is already filtered to agent-created skills only.\n"
    "2. DO NOT delete any skill. Archiving (moving the skill's directory "
    "into <config-dir>/skills/.archive/) is the maximum destructive action. "
    "Archives are recoverable; deletion is not.\n"
    "3. DO NOT touch skills shown as pinned=yes. Skip them entirely.\n"
    "3b. DO NOT archive, delete, consolidate, move, or otherwise modify any "
    "skill named in the protected router list (currently: software, "
    "operations, planning, information, communication). These back "
    "load-bearing routing UX and are filtered out of the candidate list "
    "below — never resurrect one as an archive or absorb target.\n"
    "4. DO NOT use usage counters as a reason to skip consolidation. The "
    "counters are new and often mostly zero. Judge overlap on CONTENT, "
    "not on use_count. 'use=0' is not evidence a skill is valuable; it's "
    "absence of evidence either way.\n"
    "5. DO NOT reject consolidation on the grounds that 'each skill has "
    "a distinct trigger'. Pairwise distinctness is the wrong bar. The "
    "right bar is: 'would a human maintainer write this as N separate "
    "skills, or as one skill with N labeled subsections?' When the "
    "answer is the latter, merge.\n\n"
    "How to work — not optional:\n"
    "1. Scan the full candidate list. Identify PREFIX CLUSTERS (skills "
    "sharing a first word or domain keyword). For each cluster with 2+ "
    "members, ask 'what is the UMBRELLA CLASS these skills all serve? "
    "Would a maintainer name that class and write one skill for it?' If "
    "yes, pick (or create) the umbrella and absorb the siblings into it.\n"
    "2. Three ways to consolidate — use the right one per cluster:\n"
    "   a. MERGE INTO EXISTING UMBRELLA — patch the broadest member to add "
    "a labeled section for each sibling's unique insight, then archive the "
    "siblings.\n"
    "   b. CREATE A NEW UMBRELLA SKILL.md — use skill_manage action=create "
    "to write a new class-level skill, then archive the absorbed siblings.\n"
    "   c. DEMOTE TO REFERENCES/TEMPLATES/SCRIPTS — move a sibling's "
    "narrow-but-valuable content into the umbrella's `references/`, "
    "`templates/`, or `scripts/` directory under <config-dir>/skills/"
    "<umbrella>/, then archive the old sibling.\n\n"
    "Package integrity — not optional:\n"
    "Before demoting or archiving a skill, inspect it as a COMPLETE "
    "directory package, not just SKILL.md. If the source skill has support "
    "files OR SKILL.md contains relative links such as `references/...`, "
    "`templates/...`, `scripts/...`, or `assets/...`, DO NOT flatten only "
    "SKILL.md. Either keep it standalone, fully re-home every support file "
    "into the umbrella and rewrite the destination paths, or archive the "
    "entire original package unchanged. Never leave archived/demoted "
    "instructions pointing at files left behind under the old skill "
    "directory.\n\n"
    "Your toolset:\n"
    "  - skill_manage action=patch      — add sections to the umbrella\n"
    "  - skill_manage action=create     — create a new umbrella SKILL.md\n"
    "  - skill_manage action=write_file — add a references/ templates/ or "
    "scripts/ file under an existing skill\n"
    "  - skill_manage action=delete     — archive a skill. MUST pass "
    "`absorbed_into=<umbrella>` when you've merged its content into another "
    "skill, or `absorbed_into=\"\"` when truly pruning with no forwarding "
    "target.\n\n"
    "'keep' is a legitimate decision ONLY when the skill is already a "
    "class-level umbrella and none of the proposed merges would improve "
    "discoverability.\n\n"
    "When done, write a human summary AND a structured machine-readable "
    "block. Format EXACTLY:\n\n"
    "## Structured summary (required)\n"
    "```yaml\n"
    "consolidations:\n"
    "  - from: <old-skill-name>\n"
    "    into: <umbrella-skill-name>\n"
    "    reason: <one short sentence>\n"
    "prunings:\n"
    "  - name: <skill-name>\n"
    "    reason: <one short sentence>\n"
    "```\n\n"
    "Every skill you moved to .archive/ MUST appear in exactly one of the "
    "two lists. Leave a list empty (`consolidations: []`) if none. Do not "
    "omit the block."
)


def build_consolidation_candidates(config_dir: Path) -> List[Dict[str, Any]]:
    """Return the agent-created eligible rows formatted as the prompt's
    candidate list. Reuses ``eligible_skill_rows`` so the same D2 filter
    (real-dir + created_by=agent + not-router) applies — symlinked, human, and
    protected-router skills are already excluded.
    """
    candidates: List[Dict[str, Any]] = []
    for row in eligible_skill_rows(config_dir):
        candidates.append({
            "name": row["name"],
            "state": row.get("state", STATE_ACTIVE),
            "pinned": bool(row.get("pinned")),
            "last_activity_at": row.get("last_activity_at"),
            "use_count": int(row.get("use_count") or 0),
        })
    return candidates


def _format_candidate_list(candidates: List[Dict[str, Any]]) -> str:
    lines = []
    for c in candidates:
        lines.append(
            f"- {c['name']} | state={c['state']} | pinned="
            f"{'yes' if c['pinned'] else 'no'} | "
            f"last_activity={c['last_activity_at'] or 'never'} | "
            f"use_count={c['use_count']}"
        )
    return "\n".join(lines) if lines else "(no agent-created candidates)"


def maybe_consolidate(
    config_dir: Path, harness: str, *, enabled: bool
) -> Dict[str, Any]:
    """The 3.2 consolidation hook — dormant by default.

    When ``enabled`` is False (the default, gated by ``consolidate: false``),
    return immediately and spawn **nothing**. When True, assemble the review
    prompt + candidate list and return a payload describing the subagent
    dispatch. The model spawn itself is the only inert piece under the default;
    the dispatch surface (prompt + candidate builder + the
    ``skill_manage delete --absorbed-into`` reuse path) ships structurally
    complete. The consolidation worker archives absorbed skills via Phase 2's
    ``skill_manage delete --absorbed-into <umbrella>`` — no duplication.
    """
    if not enabled:
        return {"ran": False, "reason": "consolidate disabled"}

    candidates = build_consolidation_candidates(config_dir)
    prompt = (
        CURATOR_REVIEW_PROMPT
        + "\n\n## Candidate skills (agent-created only)\n"
        + _format_candidate_list(candidates)
    )
    return {
        "ran": True,
        "harness": harness,
        "prompt": prompt,
        "candidates": candidates,
        "absorbed_archive_command": ABSORBED_ARCHIVE_COMMAND,
    }


# ---------------------------------------------------------------------------
# Run report + orchestration
# ---------------------------------------------------------------------------

def _curator_state_path(config_dir: Path) -> Path:
    return _skills_root(config_dir) / ".curator_state"


def write_run_report(
    config_dir: Path, counts: Dict[str, Any], *, now: Optional[datetime] = None
) -> Path:
    """Atomically write ``<config-dir>/skills/.curator_state`` (JSON).

    Payload is ``{last_run_at, **counts}`` — so the named ``events`` list rides
    in alongside the four counters, making the latest snapshot self-describing.
    Same tempfile + ``os.replace`` pattern the Phase 1/2 tools use.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    path = _curator_state_path(config_dir)
    payload = {"last_run_at": now.isoformat(), **counts}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".curator_state_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def _compute_dry_run_counts(
    config_dir: Path,
    *,
    now: datetime,
    stale_after_days: int,
    archive_after_days: int,
) -> Dict[str, Any]:
    """Compute would-be transition counts + events WITHOUT mutating anything."""
    stale_cutoff = now - timedelta(days=stale_after_days)
    archive_cutoff = now - timedelta(days=archive_after_days)
    counts: Dict[str, Any] = {
        "marked_stale": 0, "archived": 0, "reactivated": 0, "checked": 0,
    }
    events: List[Dict[str, Any]] = []
    for row in eligible_skill_rows(config_dir):
        counts["checked"] += 1
        if row.get("pinned"):
            continue
        anchor = (
            _parse_iso(row.get("last_activity_at"))
            or _parse_iso(row.get("created_at"))
            or now
        )
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        current = row.get("state", STATE_ACTIVE)
        if anchor <= archive_cutoff and current != STATE_ARCHIVED:
            counts["archived"] += 1
            events.append({"name": row["name"], "from_state": current,
                           "to_state": STATE_ARCHIVED, "action": "archive"})
        elif anchor <= stale_cutoff and current == STATE_ACTIVE:
            counts["marked_stale"] += 1
            events.append({"name": row["name"], "from_state": STATE_ACTIVE,
                           "to_state": STATE_STALE, "action": "stale"})
        elif anchor > stale_cutoff and current == STATE_STALE:
            counts["reactivated"] += 1
            events.append({"name": row["name"], "from_state": STATE_STALE,
                           "to_state": STATE_ACTIVE, "action": "reactivate"})
    counts["events"] = events
    return counts


def run(
    config_dir: Path,
    harness: str,
    *,
    now: Optional[datetime] = None,
    consolidate: Optional[bool] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run the curator: deterministic transitions, then (if enabled) the
    consolidation hook. Writes ``.curator_state`` on a live run; a ``dry_run``
    computes the would-be counts and writes no report and mutates nothing.

    Returns a JSON-serializable summary ``{harness, dry_run, counts,
    consolidation, report_path}``.
    """
    config_dir = Path(config_dir)
    if now is None:
        now = datetime.now(timezone.utc)
    conf = load_curator_config(config_dir)
    effective_consolidate = (
        consolidate if consolidate is not None else conf["consolidate"]
    )

    if dry_run:
        counts = _compute_dry_run_counts(
            config_dir,
            now=now,
            stale_after_days=conf["stale_after_days"],
            archive_after_days=conf["archive_after_days"],
        )
        return {
            "harness": harness,
            "dry_run": True,
            "counts": counts,
            "consolidation": {"ran": False, "reason": "dry-run"},
            "report_path": None,
        }

    counts = apply_automatic_transitions(
        config_dir,
        now=now,
        stale_after_days=conf["stale_after_days"],
        archive_after_days=conf["archive_after_days"],
    )
    report_path = write_run_report(config_dir, counts, now=now)
    consolidation = maybe_consolidate(
        config_dir, harness, enabled=bool(effective_consolidate)
    )

    # Durable audit trail: one curator entry per live run, capturing the named
    # transitions AND whether the consolidation pass ran. This is what makes a
    # run observable and (via restore_skill) reversible after the fact.
    events = counts.get("events", [])
    counts_only = {k: v for k, v in counts.items() if k != "events"}
    consolidation_summary = {
        "ran": bool(consolidation.get("ran")),
        "reason": consolidation.get("reason"),
        "harness": consolidation.get("harness"),
    }
    try:
        telemetry.append_audit(config_dir, {
            "kind": "curator",
            "counts": counts_only,
            "events": events,
            "consolidation": consolidation_summary,
        }, now=now)
    except Exception:  # pragma: no cover - audit is best-effort
        pass

    return {
        "harness": harness,
        "dry_run": False,
        "counts": counts,
        "consolidation": consolidation,
        "report_path": str(report_path),
    }


# ---------------------------------------------------------------------------
# Notify — concise on-change summary with an undo hint (best-effort)
# ---------------------------------------------------------------------------

_NOTIFY_PATH = _THIS_DIR.parent / "notify" / "notify.py"


def _run_changed(summary: Dict[str, Any]) -> bool:
    """True when a live curator run actually moved something or consolidated."""
    counts = summary.get("counts", {})
    if any(int(counts.get(k, 0) or 0) > 0
           for k in ("marked_stale", "archived", "reactivated")):
        return True
    return bool(summary.get("consolidation", {}).get("ran"))


def compose_notify_message(config_dir: Path, summary: Dict[str, Any]) -> str:
    """Build a concise one-message curator summary that NAMES what changed and
    carries the undo hint.

    Lists the skills that staled / archived / reactivated by name and appends the
    ``skill_telemetry.py --action restore`` recovery command so the recipient can
    reverse an archive without hunting for the syntax.
    """
    counts = summary.get("counts", {})
    events = counts.get("events", []) or []
    by_action: Dict[str, List[str]] = {"stale": [], "archive": [], "reactivate": []}
    for ev in events:
        by_action.setdefault(ev.get("action", ""), []).append(ev.get("name", ""))

    parts: List[str] = []
    if by_action.get("archive"):
        parts.append("archived " + ", ".join(by_action["archive"]))
    if by_action.get("stale"):
        parts.append("staled " + ", ".join(by_action["stale"]))
    if by_action.get("reactivate"):
        parts.append("reactivated " + ", ".join(by_action["reactivate"]))

    consolidation = summary.get("consolidation", {})
    if consolidation.get("ran"):
        parts.append("ran consolidation pass")

    summary_line = "; ".join(parts) if parts else "no transitions"
    msg = f"Skill curator: {summary_line}."

    archived = by_action.get("archive") or []
    if archived:
        example = archived[0]
        msg += (
            f" Undo an archive with: skill_telemetry.py --action restore "
            f"--skill {example} --config-dir {config_dir}"
        )
    return msg


def maybe_notify(config_dir: Path, summary: Dict[str, Any]) -> Optional[List[str]]:
    """Shell out to the notify tool with a one-message change summary.

    No-op (returns ``None``) when nothing changed. Best-effort: a notify failure
    is swallowed so it never fails the run. When ``HERMES_NOTIFY_TEST`` is set,
    ``--test-mode`` is passed so no real Telegram is sent — tests assert the
    composed message and the invoked argv. Returns the argv that was invoked (or
    would have been), or ``None`` when no notification was warranted.
    """
    if not _run_changed(summary):
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
        description="Deterministic per-mind skill-lifecycle curator"
    )
    parser.add_argument("--config-dir", required=True, help="Mind config dir (.claude/.codex)")
    parser.add_argument(
        "--harness", default="claude_cli", choices=["claude_cli", "codex_cli"]
    )
    parser.add_argument(
        "--consolidate", action="store_true",
        help="Override config and run the LLM consolidation pass for this run",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute would-be transitions without mutating; write no report",
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="On a changed live run, send a concise notify summary (best-effort)",
    )
    args = parser.parse_args(argv)

    consolidate_arg = True if args.consolidate else None
    summary = run(
        Path(args.config_dir), args.harness,
        consolidate=consolidate_arg, dry_run=args.dry_run,
    )
    if args.notify and not args.dry_run:
        maybe_notify(Path(args.config_dir), summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
