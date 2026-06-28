#!/usr/bin/env python3
"""Per-mind skill usage telemetry sidecar.

Tracks per-skill usage metadata in a sidecar JSON file at
``<config_dir>/skills/.usage.json``, keyed by skill name. Ported from
Hermes' ``tools/skill_usage.py`` and re-parametrized for hive_mind's
multi-mind, copy-don't-share model: there is **no** global home and **no**
shared file. Every public function takes an explicit ``config_dir`` (a mind's
``.claude`` / ``.codex`` directory) so each mind owns its own sidecar.

This module is **both** a CLI (argparse + JSON stdout) and an importable
library. The Stop-hook adapters call ``bump_skills`` / ``seed_existing_skills``
in-process; operators and tests drive the same logic via the CLI.

Design notes (ported verbatim from the Hermes original):
  - Sidecar, not frontmatter. Keeps operational telemetry out of
    user-authored SKILL.md content.
  - Atomic writes via tempfile + ``os.replace`` (+ ``fsync``).
  - ``fcntl``-locked read-modify-write so concurrent bumps never lose
    updates.
  - All counter bumps are best-effort: failures log at DEBUG and return
    silently. A broken sidecar never breaks the caller.

Lifecycle states:
    active    -> default
    stale     -> unused beyond the stale window (set by the curator, Phase 3)
    archived  -> unused beyond the archive window (Phase 3)
    pinned    -> opt-out from auto transitions (boolean flag, orthogonal)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

logger = logging.getLogger(__name__)

# fcntl is Unix-only; on Windows use msvcrt for file locking.
msvcrt: Any = None
fcntl: Any
try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific fallback
    fcntl = None
    try:
        import msvcrt  # type: ignore[no-redef]
    except ImportError:
        pass


STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
_VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}


# ---------------------------------------------------------------------------
# Path resolution — sidecar lives under the mind's own config dir
# ---------------------------------------------------------------------------

def _skills_dir(config_dir: Path) -> Path:
    return Path(config_dir) / "skills"


def usage_file(config_dir: Path) -> Path:
    """Resolve the sidecar path for *config_dir*.

    The sidecar is ``<config_dir>/skills/.usage.json`` for both harnesses —
    Claude resolves ``config_dir`` to a mind's ``.claude`` dir, Codex to its
    ``.codex`` dir.
    """
    return _skills_dir(config_dir) / ".usage.json"


def audit_log_file(config_dir: Path) -> Path:
    """Resolve the append-only audit-ledger path for *config_dir*.

    The ledger is ``<config_dir>/skills/.skill_audit.log`` — one JSON object
    per line, append-only history. This is the single durable record both the
    curator and skill_manage write to when they transition or archive a skill.
    """
    return _skills_dir(config_dir) / ".skill_audit.log"


@contextmanager
def _usage_file_lock(config_dir: Path) -> Iterator[None]:
    """Serialize .usage.json read-modify-write cycles across processes."""
    lock_path = usage_file(config_dir).with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        yield
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        elif msvcrt:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        fd.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp defensively for activity comparisons."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def latest_activity_at(record: Dict[str, Any]) -> Optional[str]:
    """Return the newest actual activity timestamp for a usage record.

    "Activity" means a skill was used, viewed, or patched. Creation time is
    intentionally **excluded** so callers can still distinguish never-active
    skills; lifecycle code can fall back to ``created_at`` as its own anchor.
    """
    latest_dt: Optional[datetime] = None
    latest_raw: Optional[str] = None
    for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
        raw = record.get(key)
        dt = _parse_iso_timestamp(raw)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_raw = str(raw)
    return latest_raw


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------

def _empty_record() -> Dict[str, Any]:
    return {
        "created_by": None,
        "use_count": 0,
        "view_count": 0,
        "last_used_at": None,
        "last_viewed_at": None,
        "patch_count": 0,
        "last_patched_at": None,
        "created_at": _now_iso(),
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
    }


def load_usage(config_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Read the entire .usage.json map. Returns empty dict on missing/corrupt."""
    path = usage_file(config_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    clean: Dict[str, Dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            clean[str(k)] = v
    return clean


def save_usage(config_dir: Path, data: Dict[str, Dict[str, Any]]) -> None:
    """Write the usage map atomically. Best-effort — errors are logged, not raised."""
    path = usage_file(config_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".usage_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to write %s: %s", path, e, exc_info=True)


def get_record(config_dir: Path, skill_name: str) -> Dict[str, Any]:
    """Return the record for *skill_name*, creating a fresh one if missing.

    Missing keys are backfilled from ``_empty_record`` so callers can always
    index every field, even against an older sidecar.
    """
    data = load_usage(config_dir)
    rec = data.get(skill_name)
    if not isinstance(rec, dict):
        return _empty_record()
    base = _empty_record()
    for k, v in base.items():
        rec.setdefault(k, v)
    return rec


def seed_record_if_missing(
    config_dir: Path,
    skill_name: str,
    *,
    created_by: Optional[str] = None,
    state: str = STATE_ACTIVE,
) -> bool:
    """Persist a baseline usage record for *skill_name* if none exists.

    Fixes ``created_at`` to the moment the skill is first seen so any future
    inactivity clock measures non-use FROM THEN — not from epoch. No-op (and
    returns ``False``) when a record already exists, so re-runs never clobber
    counters or provenance.
    """
    if not skill_name:
        return False
    try:
        with _usage_file_lock(config_dir):
            data = load_usage(config_dir)
            if isinstance(data.get(skill_name), dict):
                return False
            rec = _empty_record()
            rec["created_by"] = created_by
            if state in _VALID_STATES:
                rec["state"] = state
            data[skill_name] = rec
            save_usage(config_dir, data)
            return True
    except Exception as e:
        logger.debug(
            "skill_telemetry.seed_record_if_missing(%s) failed: %s",
            skill_name, e, exc_info=True,
        )
        return False


def _mutate(config_dir: Path, skill_name: str, mutator: Callable[[Dict[str, Any]], None]) -> None:
    """Load, apply *mutator(record)* in place, save. Best-effort.

    Records telemetry for ANY skill — usage tracking is pure observability.
    A missing record is created on first touch.
    """
    if not skill_name:
        return
    try:
        with _usage_file_lock(config_dir):
            data = load_usage(config_dir)
            rec = data.get(skill_name)
            if not isinstance(rec, dict):
                rec = _empty_record()
            mutator(rec)
            data[skill_name] = rec
            save_usage(config_dir, data)
    except Exception as e:
        logger.debug(
            "skill_telemetry._mutate(%s) failed: %s", skill_name, e, exc_info=True
        )


# ---------------------------------------------------------------------------
# Public counter-bump helpers — telemetry for ALL skills (observability only)
# ---------------------------------------------------------------------------

def bump_view(config_dir: Path, skill_name: str) -> None:
    """Bump view_count and last_viewed_at."""
    def _apply(rec: Dict[str, Any]) -> None:
        rec["view_count"] = int(rec.get("view_count") or 0) + 1
        rec["last_viewed_at"] = _now_iso()
    _mutate(config_dir, skill_name, _apply)


def bump_use(config_dir: Path, skill_name: str) -> None:
    """Bump use_count and last_used_at (a skill was actively invoked)."""
    def _apply(rec: Dict[str, Any]) -> None:
        rec["use_count"] = int(rec.get("use_count") or 0) + 1
        rec["last_used_at"] = _now_iso()
    _mutate(config_dir, skill_name, _apply)


def bump_patch(config_dir: Path, skill_name: str) -> None:
    """Bump patch_count and last_patched_at (a skill was edited)."""
    def _apply(rec: Dict[str, Any]) -> None:
        rec["patch_count"] = int(rec.get("patch_count") or 0) + 1
        rec["last_patched_at"] = _now_iso()
    _mutate(config_dir, skill_name, _apply)


def mark_agent_created(config_dir: Path, skill_name: str) -> None:
    """Mark a skill as agent-authored (curator-management opt-in, Phase 3)."""
    def _apply(rec: Dict[str, Any]) -> None:
        rec["created_by"] = "agent"
    _mutate(config_dir, skill_name, _apply)


def set_state(config_dir: Path, skill_name: str, state: str) -> bool:
    """Set lifecycle state. No-op (returns ``False``) if *state* is invalid."""
    if state not in _VALID_STATES:
        logger.debug("set_state: invalid state %r for %s", state, skill_name)
        return False

    def _apply(rec: Dict[str, Any]) -> None:
        rec["state"] = state
        if state == STATE_ARCHIVED:
            rec["archived_at"] = _now_iso()
        elif state == STATE_ACTIVE:
            rec["archived_at"] = None
    _mutate(config_dir, skill_name, _apply)
    return True


def set_pinned(config_dir: Path, skill_name: str, pinned: bool) -> None:
    """Set the pinned flag (orthogonal to state)."""
    def _apply(rec: Dict[str, Any]) -> None:
        rec["pinned"] = bool(pinned)
    _mutate(config_dir, skill_name, _apply)


def forget(config_dir: Path, skill_name: str) -> None:
    """Drop a skill's usage entry entirely (called when the skill is deleted)."""
    if not skill_name:
        return
    try:
        with _usage_file_lock(config_dir):
            data = load_usage(config_dir)
            if skill_name in data:
                del data[skill_name]
                save_usage(config_dir, data)
    except Exception as e:
        logger.debug(
            "skill_telemetry.forget(%s) failed: %s", skill_name, e, exc_info=True
        )


# ---------------------------------------------------------------------------
# Shared audit ledger — append-only history both other tools write to
# ---------------------------------------------------------------------------

def append_audit(
    config_dir: Path, entry: Dict[str, Any], *, now: Optional[datetime] = None
) -> None:
    """Append one JSON line to ``<config_dir>/skills/.skill_audit.log``.

    The ledger is append-only history: every entry is merged with an ``at``
    ISO-8601 timestamp and written as a single line. Parents are created on
    demand and the write is serialized through the module's lock so concurrent
    appends never interleave a partial line. Best-effort — failures log at
    DEBUG and return silently; a broken ledger never breaks the caller.
    """
    path = audit_log_file(config_dir)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    record = {"at": stamp, **dict(entry)}
    try:
        with _usage_file_lock(config_dir):
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, sort_keys=True, ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception as e:  # pragma: no cover - best-effort
        logger.debug("skill_telemetry.append_audit failed: %s", e, exc_info=True)


def read_audit(config_dir: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read the audit ledger, returning the most-recent *limit* entries.

    Entries come back oldest-first (newest last); when *limit* is given only
    the trailing ``limit`` entries are returned. Corrupt or empty lines are
    skipped. A missing ledger yields ``[]``.
    """
    path = audit_log_file(config_dir)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError as e:  # pragma: no cover - defensive
        logger.debug("skill_telemetry.read_audit failed: %s", e, exc_info=True)
        return []
    if limit is not None and limit >= 0:
        return entries[-limit:] if limit else []
    return entries


# ---------------------------------------------------------------------------
# restore_skill — the exact inverse of the curator/skill_manage archive move
# ---------------------------------------------------------------------------

def _archived_copies(config_dir: Path, name: str) -> List[Path]:
    """Return every archived copy of *name* under ``skills/.archive/``.

    Matches both the plain ``<name>`` directory and any timestamp-suffixed
    ``<name>-<stamp>`` collision copy (the suffix scheme used by both
    ``skill_curator.archive_skill`` and ``skill_manage`` delete). Sorted so the
    most recent copy is last.
    """
    archive_root = _skills_dir(config_dir) / ".archive"
    if not archive_root.is_dir():
        return []
    copies: List[Path] = []
    for entry in archive_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name == name or entry.name.startswith(f"{name}-"):
            copies.append(entry)
    # Plain "<name>" sorts before any "<name>-<stamp>"; the lexicographically
    # largest timestamp suffix is the newest, so a plain sort puts newest last.
    copies.sort(key=lambda p: p.name)
    return copies


def restore_skill(config_dir: Path, name: str) -> "tuple[bool, Optional[str]]":
    """Move an archived skill back to live and re-activate it — inverse of
    ``archive_skill``.

    Locates ``<config_dir>/skills/.archive/<name>`` (picking the most-recent
    timestamp-suffixed copy on collision), moves it back to
    ``<config_dir>/skills/<name>``, sets its sidecar state ``active``, and
    appends a ``{kind: "restore", name}`` audit entry. Refuses (returns
    ``(False, reason)``) when a live skill of that name already exists or no
    archived copy is found — and mutates nothing on refusal.
    """
    if not name:
        return False, "no skill name given"

    live = _skills_dir(config_dir) / name
    if live.exists() or os.path.islink(str(live)):
        return False, f"skill '{name}' already exists live; refusing to overwrite"

    copies = _archived_copies(config_dir, name)
    if not copies:
        return False, f"no archived copy of '{name}' found"

    source = copies[-1]
    try:
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(live))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("restore_skill move failed: %s", e, exc_info=True)
        return False, f"could not move archive copy ({type(e).__name__})"

    set_state(config_dir, name, STATE_ACTIVE)
    append_audit(config_dir, {"kind": "restore", "name": name})
    return True, str(live)


# ---------------------------------------------------------------------------
# Shared bump helper — the thin entry point the Stop-hooks call
# ---------------------------------------------------------------------------

def bump_skills(config_dir: Path, names: Iterable[str]) -> List[str]:
    """Bump ``use_count`` once for each distinct skill name in *names*.

    This is the shared logic both per-harness Stop-hook adapters call: the
    detector returns the set of skills that fired this turn, and this records
    one use per distinct name. Returns the sorted list of names bumped.
    """
    distinct = sorted({n for n in names if n})
    for name in distinct:
        bump_use(config_dir, name)
    return distinct


# ---------------------------------------------------------------------------
# First-run backfill — seed a record for every real (non-plugin) skill
# ---------------------------------------------------------------------------

def seed_existing_skills(config_dir: Path) -> Dict[str, List[str]]:
    """Seed a record for every existing on-disk skill not yet recorded.

    Walks ``<config_dir>/skills/*/SKILL.md`` one level deep, skips dotdirs
    (e.g. ``.archive``) and **symlinked** skill dirs (plugin skills, per D2 —
    a symlink means an externally-owned plugin skill, never curated). For each
    real skill without a record, writes ``created_at=now``,
    ``created_by="human"``, ``state="active"``. Idempotent: an existing record
    is never clobbered.

    Returns ``{"seeded": [...], "skipped": [...]}`` (both sorted).
    """
    skills_dir = _skills_dir(config_dir)
    seeded: List[str] = []
    skipped: List[str] = []
    if not skills_dir.is_dir():
        return {"seeded": seeded, "skipped": skipped}

    for entry in sorted(skills_dir.iterdir()):
        name = entry.name
        if name.startswith("."):
            continue
        if not entry.is_dir():
            continue
        # D2: a symlinked skill dir is a plugin skill — never curate it.
        if os.path.islink(str(entry)):
            skipped.append(name)
            continue
        if not (entry / "SKILL.md").is_file():
            continue
        if seed_record_if_missing(
            config_dir, name, created_by="human", state=STATE_ACTIVE
        ):
            seeded.append(name)
        else:
            skipped.append(name)

    return {"seeded": sorted(seeded), "skipped": sorted(skipped)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Per-mind skill usage telemetry sidecar")
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "bump-use", "bump-view", "bump-patch", "mark-agent-created",
            "set-state", "set-pinned", "forget", "list", "seed",
            "restore", "audit",
        ],
        help="Telemetry action to perform",
    )
    parser.add_argument("--config-dir", required=True, help="Mind config dir (.claude/.codex)")
    parser.add_argument("--skill", help="Skill name (required for all per-skill actions)")
    parser.add_argument("--state", help="Lifecycle state for --action set-state")
    parser.add_argument("--pinned", help="Bool for --action set-pinned (true/false)")
    parser.add_argument("--limit", type=int, default=None, help="Tail length for --action audit")

    args = parser.parse_args(argv)
    config_dir = Path(args.config_dir)
    action = args.action

    def _needs_skill() -> Optional[int]:
        if not args.skill:
            print(json.dumps({"error": f"--skill is required for --action {action}"}))
            return 1
        return None

    if action == "list":
        print(json.dumps(load_usage(config_dir), indent=2, sort_keys=True))
        return 0

    if action == "seed":
        print(json.dumps(seed_existing_skills(config_dir), sort_keys=True))
        return 0

    if action == "audit":
        print(json.dumps(read_audit(config_dir, args.limit), indent=2, sort_keys=True))
        return 0

    if action == "restore":
        rc = _needs_skill()
        if rc is not None:
            return rc
        ok, info = restore_skill(config_dir, args.skill)
        if not ok:
            print(json.dumps({"ok": False, "skill": args.skill, "error": info}, sort_keys=True))
            return 1
        print(json.dumps({"ok": True, "skill": args.skill, "restored_to": info},
                         sort_keys=True))
        return 0

    rc = _needs_skill()
    if rc is not None:
        return rc
    skill: str = args.skill

    if action == "bump-use":
        bump_use(config_dir, skill)
    elif action == "bump-view":
        bump_view(config_dir, skill)
    elif action == "bump-patch":
        bump_patch(config_dir, skill)
    elif action == "mark-agent-created":
        mark_agent_created(config_dir, skill)
    elif action == "set-state":
        if not args.state or not set_state(config_dir, skill, args.state):
            print(json.dumps({"error": f"invalid state: {args.state!r}"}))
            return 1
    elif action == "set-pinned":
        pinned = str(args.pinned).strip().lower() in ("1", "true", "yes", "on")
        set_pinned(config_dir, skill, pinned)
    elif action == "forget":
        forget(config_dir, skill)

    print(json.dumps({"ok": True, "skill": skill, "record": get_record(config_dir, skill)
                      if action != "forget" else None}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
