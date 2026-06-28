#!/usr/bin/env python3
"""Agent-authoring tool: create / edit / patch / delete skills (stateless).

Ported faithfully from Hermes ``tools/skill_manager_tool.py`` and re-shaped for
hive_mind's stateless, multi-mind, copy-don't-share model. Where Hermes resolves
a single ``~/.hermes/skills`` root from global config, this tool takes the mind's
config dir as an explicit ``--config-dir`` argument (its ``.claude`` / ``.codex``
directory) plus a ``--harness {claude_cli,codex_cli}`` flag. Skills root is
``<config-dir>/skills/``. The tool is pure: it never reads ``runtime.yaml`` and
never touches the live (gitignored) mind dirs except when the caller points it at
them.

Six actions, each emitting JSON to stdout:
  create      -- new skill (SKILL.md + dir); renders harness-aware frontmatter
  edit        -- full SKILL.md rewrite of an existing skill
  patch       -- exact-match find/replace within SKILL.md or a supporting file
  delete      -- ARCHIVE (never rmtree) the skill dir to skills/.archive/
  write_file  -- add/overwrite a supporting file under references|templates|scripts|assets
  remove_file -- remove a supporting file

Harness-aware frontmatter (the one real fork from Hermes): the tool renders the
SKILL.md frontmatter itself rather than trusting the agent to hand-write a
dialect. ``claude_cli`` emits the full block (``user-invocable: false`` plus a
``metadata.provenance: agent`` + ``authored_at`` stamp). ``codex_cli`` runs the
declared fields through the single shared ``strip_to_codex_frontmatter`` keep-set
(``name`` / ``description`` / ``argument-hint``) and emits the minimal block. Both
paths derive from one keep-set so the two dialects cannot drift; for Codex,
provenance rides the telemetry sidecar only.

Telemetry: this tool reuses the Phase 1 sidecar module
(``tools/stateless/skill_telemetry/skill_telemetry.py``), loaded by absolute path
via ``importlib`` since it is a standalone script, not an installed package. The
action -> telemetry map: ``create -> mark_agent_created``,
``edit``/``patch``/``write_file``/``remove_file`` -> ``bump_patch``,
``delete -> forget``.

Delete = archive, never remove. The repo's HITL approval gate is a Telegram
inline-keyboard callback driven by the bot surface — it is not reachable
synchronously from a stateless CLI process. Per the backlog instruction, ``delete``
therefore archives **unconditionally**: it moves ``skills/<name>/`` to
``skills/.archive/<name>/`` (timestamp suffix on collision) and calls
``forget(name)`` — it never ``rmtree``s the live dir. The HITL approval gate is a
thin wrapper deferred to the skill layer (the ``skill-manage`` SKILL.md surfaces
the action to Daniel before invoking delete).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
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

# Local threat scanner (Step 3) — sibling stateless module.
_THREAT_PATH = _THIS_DIR / "threat_scan.py"


def _load_threat_scan():
    spec = importlib.util.spec_from_file_location("threat_scan", str(_THREAT_PATH))
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise ImportError(f"cannot load threat_scan module from {_THREAT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Constants & limits (ported verbatim from skill_manager_tool.py)
# ---------------------------------------------------------------------------

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file

VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}

SUPPORTED_HARNESSES = {"claude_cli", "codex_cli"}

# The exact Codex keep-set from convert-claude-skill-to-codex Step 3.
CODEX_KEEP_FIELDS = ("name", "description", "argument-hint")


# ===========================================================================
# Step 1: frontmatter rendering + shared Codex strip
# ===========================================================================

def strip_to_codex_frontmatter(fm: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the portable Codex fields, drop everything else.

    Single source of truth for the keep-set (``name`` / ``description`` /
    ``argument-hint``) documented in ``convert-claude-skill-to-codex`` Step 3.
    Both render paths derive from this so the two dialects cannot drift.
    """
    return {k: fm[k] for k in CODEX_KEEP_FIELDS if k in fm and fm[k] is not None}


def _dump_frontmatter(fields: Dict[str, Any]) -> str:
    """Render a YAML frontmatter block (with the ``---`` fences) from a dict."""
    body = yaml.safe_dump(fields, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return f"---\n{body}---\n"


def render_frontmatter(fields: Dict[str, Any], harness: str) -> str:
    """Render the SKILL.md frontmatter block for *harness*.

    ``claude_cli`` emits the full block: the declared fields, ``user-invocable:
    false``, and a ``metadata`` block with ``provenance: agent`` +
    ``authored_at`` (ISO-8601 UTC). ``codex_cli`` runs the declared fields
    through ``strip_to_codex_frontmatter`` and emits the minimal block (no
    provenance — that rides the sidecar). Raises ``ValueError`` on an unknown
    harness.
    """
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError(
            f"Unknown harness {harness!r}. Supported: {sorted(SUPPORTED_HARNESSES)}"
        )

    if harness == "codex_cli":
        return _dump_frontmatter(strip_to_codex_frontmatter(fields))

    # claude_cli — full dialect.
    out: Dict[str, Any] = {}
    if "name" in fields:
        out["name"] = fields["name"]
    if "description" in fields:
        out["description"] = fields["description"]
    out["user-invocable"] = False
    # Pass through any optional Claude-supported fields the agent declared.
    for k in ("argument-hint", "model", "schedule", "schedule_timezone", "voice"):
        if k in fields and fields[k] is not None:
            out[k] = fields[k]
    out["metadata"] = {
        "provenance": "agent",
        "authored_at": datetime.now(timezone.utc).isoformat(),
    }
    return _dump_frontmatter(out)


def split_frontmatter(content: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Split SKILL.md content into (frontmatter dict, body).

    Returns ``(None, content)`` when no closed frontmatter block is present.
    """
    if not content.startswith("---"):
        return None, content
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return None, content
    yaml_content = content[3:end_match.start() + 3]
    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return None, content
    body = content[end_match.end() + 3:]
    if not isinstance(parsed, dict):
        return None, body
    return parsed, body


def compose_skill_md(fields: Dict[str, Any], body: str, harness: str) -> str:
    """Render a complete SKILL.md (frontmatter + body) for *harness*."""
    fm = render_frontmatter(fields, harness)
    body = body.lstrip("\n")
    return f"{fm}\n{body}" if body else fm


# ===========================================================================
# Step 2: validators + guards (ported from skill_manager_tool.py)
# ===========================================================================

def _validate_name(name: str) -> Optional[str]:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_category(category: Optional[str]) -> Optional[str]:
    """Validate an optional category name used as a single directory segment."""
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."
    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Categories must be a single "
            "directory name (no path separators)."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """Validate SKILL.md content has proper frontmatter + required fields."""
    if not content.strip():
        return "Content cannot be empty."
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---)."
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Add a closing '---' line."
    yaml_content = content[3:end_match.start() + 3]
    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"
    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."
    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter."
    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    """Check content does not exceed the character limit for agent writes."""
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). Split into a smaller "
            f"SKILL.md with supporting files in references/ or templates/."
        )
    return None


def _validate_file_bytes(file_content: str) -> Optional[str]:
    """Check a supporting file's UTF-8 byte length against the 1 MiB limit."""
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return (
            f"File content is {content_bytes:,} bytes "
            f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
            f"Split into smaller files."
        )
    return None


def _has_traversal(file_path: str) -> bool:
    """True when any path component is ``..`` (defends against escaping the dir)."""
    return any(part == ".." for part in Path(file_path).parts)


def _validate_file_path(file_path: str) -> Optional[str]:
    """Validate a supporting-file path: allowed subdir, no traversal, has a file."""
    if not file_path:
        return "file_path is required."
    if _has_traversal(file_path):
        return "Path traversal ('..') is not allowed."
    normalized = Path(file_path)
    # SKILL.md lives at the skill root, not under a subdir — accept it directly.
    if normalized.parts and normalized.name == "SKILL.md":
        if len(normalized.parts) in (1, 2):
            return None
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"
    if len(normalized.parts) < 2:
        return (
            f"Provide a file path, not just a directory. "
            f"Example: '{normalized.parts[0]}/myfile.md'"
        )
    return None


def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """Atomically write text via a same-dir tempfile + ``os.replace``."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent), prefix=f".{file_path.name}.tmp.", suffix=""
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(temp_path, file_path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Path resolution — skills root is <config-dir>/skills/ (single per mind)
# ---------------------------------------------------------------------------

def _skills_root(config_dir: Path) -> Path:
    return Path(config_dir) / "skills"


def _resolve_skill_dir(config_dir: Path, name: str, category: Optional[str] = None) -> Path:
    """Build the directory path for a new skill, optionally under a category."""
    root = _skills_root(config_dir)
    if category:
        return root / category / name
    return root / name


def _find_skill(config_dir: Path, name: str) -> Optional[Path]:
    """Locate an existing skill dir by name under skills/[<category>/]<name>/.

    Walks ``skills/**/SKILL.md`` and returns the parent dir whose basename
    matches *name*, skipping the ``.archive`` tree. Returns None if not found.
    """
    root = _skills_root(config_dir)
    if not root.exists():
        return None
    for skill_md in root.rglob("SKILL.md"):
        if ".archive" in skill_md.parts:
            continue
        if skill_md.parent.name == name:
            return skill_md.parent
    return None


def _resolve_skill_target(skill_dir: Path, file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a supporting-file path and ensure it stays within the skill dir."""
    target = (skill_dir / file_path)
    try:
        target.resolve().relative_to(skill_dir.resolve())
    except ValueError:
        return None, f"Resolved path escapes the skill directory: {file_path}"
    return target, None


# ===========================================================================
# Step 3: security scan wiring (warn + annotate, never block)
# ===========================================================================

threat_scan = _load_threat_scan()


def set_flagged(config_dir: Path, name: str, flagged: bool = True) -> None:
    """Annotate the sidecar record with ``flagged``. Reuses telemetry's lock."""
    telemetry._mutate(config_dir, name, lambda rec: rec.__setitem__("flagged", bool(flagged)))


def _scan_and_annotate(config_dir: Path, name: str, content: str) -> Dict[str, Any]:
    """Scan *content*; on any hit, annotate the sidecar and return warn fields.

    Returns ``{"flagged": bool, "warnings": [...]}``. Never blocks — Skippy is
    an operator mind, so flagged writes still succeed.
    """
    findings = threat_scan.scan_for_threats(content)
    if findings:
        set_flagged(config_dir, name, True)
        return {"flagged": True, "warnings": findings}
    return {"flagged": False, "warnings": []}


# ===========================================================================
# Step 4: action handlers + dispatcher
# ===========================================================================

def _rerender_content(content: str, harness: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse incoming SKILL.md, re-render its frontmatter for *harness*.

    Keeps the agent-declared fields but renders the dialect ourselves so the
    agent can't hand-write the wrong harness's frontmatter. Returns
    ``(rendered_content, None)`` or ``(None, error)``.
    """
    err = _validate_frontmatter(content)
    if err:
        return None, err
    fields, body = split_frontmatter(content)
    if fields is None:
        return None, "Could not parse SKILL.md frontmatter."
    try:
        rendered = compose_skill_md(fields, body, harness)
    except ValueError as e:
        return None, str(e)
    return rendered, None


def _create_skill(config_dir: Path, harness: str, name: str,
                  content: str, category: Optional[str] = None) -> Dict[str, Any]:
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}
    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    rendered, err = _rerender_content(content, harness)
    if err:
        return {"success": False, "error": err}
    err = _validate_content_size(rendered)
    if err:
        return {"success": False, "error": err}

    if _find_skill(config_dir, name) is not None:
        return {"success": False, "error": f"A skill named '{name}' already exists."}

    skill_dir = _resolve_skill_dir(config_dir, name, category)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    _atomic_write_text(skill_md, rendered)

    result: Dict[str, Any] = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir),
        "skill_md": str(skill_md),
    }
    if category:
        result["category"] = category
    result.update(_scan_and_annotate(config_dir, name, rendered))
    return result


def _edit_skill(config_dir: Path, harness: str, name: str, content: str) -> Dict[str, Any]:
    rendered, err = _rerender_content(content, harness)
    if err:
        return {"success": False, "error": err}
    err = _validate_content_size(rendered)
    if err:
        return {"success": False, "error": err}

    skill_dir = _find_skill(config_dir, name)
    if skill_dir is None:
        return {"success": False, "error": f"Skill '{name}' not found."}

    skill_md = skill_dir / "SKILL.md"
    _atomic_write_text(skill_md, rendered)
    result: Dict[str, Any] = {
        "success": True,
        "message": f"Skill '{name}' updated (full rewrite).",
        "path": str(skill_dir),
    }
    result.update(_scan_and_annotate(config_dir, name, rendered))
    return result


def _patch_skill(config_dir: Path, name: str, old_string: str, new_string: str,
                 file_path: Optional[str] = None, replace_all: bool = False) -> Dict[str, Any]:
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'."}

    skill_dir = _find_skill(config_dir, name)
    if skill_dir is None:
        return {"success": False, "error": f"Skill '{name}' not found."}

    if file_path:
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
    else:
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {file_path or 'SKILL.md'}"}

    content = target.read_text(encoding="utf-8")

    # Exact-match find/replace (no fuzzy engine — literal semantics).
    count = content.count(old_string)
    if count == 0:
        return {"success": False, "error": f"old_string not found in {file_path or 'SKILL.md'}."}
    if count > 1 and not replace_all:
        return {
            "success": False,
            "error": (
                f"old_string matched {count} times in {file_path or 'SKILL.md'}; "
                f"it must be unique unless replace_all=True."
            ),
        }
    new_content = content.replace(old_string, new_string)

    target_label = file_path or "SKILL.md"
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {"success": False, "error": f"Patch would break SKILL.md structure: {err}"}

    _atomic_write_text(target, new_content)
    result: Dict[str, Any] = {
        "success": True,
        "message": f"Patched {target_label} in skill '{name}' ({count} replacement(s)).",
    }
    result.update(_scan_and_annotate(config_dir, name, new_content))
    return result


def _write_file(config_dir: Path, name: str, file_path: str, file_content: str) -> Dict[str, Any]:
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}
    if file_content is None:
        return {"success": False, "error": "file_content is required."}
    err = _validate_file_bytes(file_content)
    if err:
        return {"success": False, "error": err}
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    skill_dir = _find_skill(config_dir, name)
    if skill_dir is None:
        return {"success": False, "error": f"Skill '{name}' not found. Create it first."}

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    _atomic_write_text(target, file_content)
    result: Dict[str, Any] = {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }
    result.update(_scan_and_annotate(config_dir, name, file_content))
    return result


def _remove_file(config_dir: Path, name: str, file_path: str) -> Dict[str, Any]:
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    skill_dir = _find_skill(config_dir, name)
    if skill_dir is None:
        return {"success": False, "error": f"Skill '{name}' not found."}

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if not target.exists():
        return {"success": False, "error": f"File '{file_path}' not found in skill '{name}'."}

    target.unlink()
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    return {"success": True, "message": f"File '{file_path}' removed from skill '{name}'."}


# Action -> telemetry map (reuse Phase 1 module; create/delete handled inline).
_BUMP_PATCH_ACTIONS = {"edit", "patch", "write_file", "remove_file"}


def skill_manage(action: str, config_dir: Any, harness: str, **kwargs: Any) -> str:
    """Dispatch a skill-management action. Returns a JSON string.

    ``config_dir`` is the mind's ``.claude`` / ``.codex`` dir; ``harness`` is
    ``claude_cli`` or ``codex_cli``. Action-specific kwargs: ``name``,
    ``content``, ``category``, ``file_path``, ``file_content``, ``old_string``,
    ``new_string``, ``replace_all``, ``absorbed_into``.
    """
    cfg = Path(config_dir)
    name = kwargs.get("name", "") or ""

    if harness not in SUPPORTED_HARNESSES and action in {"create", "edit"}:
        return json.dumps(
            {"success": False, "error": f"Unknown harness {harness!r}."}, ensure_ascii=False
        )

    if action == "create":
        content = kwargs.get("content")
        if not content:
            return json.dumps({"success": False, "error": "content is required for 'create'."})
        result = _create_skill(cfg, harness, name, content, kwargs.get("category"))
    elif action == "edit":
        content = kwargs.get("content")
        if not content:
            return json.dumps({"success": False, "error": "content is required for 'edit'."})
        result = _edit_skill(cfg, harness, name, content)
    elif action == "patch":
        result = _patch_skill(
            cfg, name, kwargs.get("old_string"), kwargs.get("new_string"),
            kwargs.get("file_path"), bool(kwargs.get("replace_all", False)),
        )
    elif action == "delete":
        result = _delete_skill(cfg, name, kwargs.get("absorbed_into"))
    elif action == "write_file":
        result = _write_file(cfg, name, kwargs.get("file_path"), kwargs.get("file_content"))
    elif action == "remove_file":
        result = _remove_file(cfg, name, kwargs.get("file_path"))
    else:
        result = {"success": False, "error": f"Unknown action '{action}'."}

    # Telemetry — reuse the Phase 1 module. Bumps happen on success only.
    if result.get("success"):
        try:
            if action == "create":
                telemetry.mark_agent_created(cfg, name)
            elif action in _BUMP_PATCH_ACTIONS:
                telemetry.bump_patch(cfg, name)
            elif action == "delete":
                telemetry.forget(cfg, name)
        except Exception:  # pragma: no cover - telemetry is best-effort
            pass

    return json.dumps(result, ensure_ascii=False)


# ===========================================================================
# Step 5: delete = archive-not-remove + guards + CLI
# ===========================================================================

def _is_path_redirect(path: Path) -> bool:
    """True when *path* is a symlink (a plugin skill / poisoned redirect)."""
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _validate_delete_target(config_dir: Path, skill_dir: Path) -> Optional[str]:
    """Guard before archiving: refuse symlinks and the skills root itself.

    A symlinked skill dir is an externally-owned plugin skill — archiving it
    would clobber a link out of the mind's tree. The skills root itself must
    never be archived (would move every skill). Returns an error string to
    refuse on, or None when safe.
    """
    if _is_path_redirect(skill_dir):
        return (
            f"Refusing to delete '{skill_dir.name}': the skill directory is a "
            f"symlink (an externally-owned plugin skill). Remove the link target "
            f"manually if intended."
        )
    try:
        resolved = skill_dir.resolve()
        root = _skills_root(config_dir).resolve()
    except OSError as exc:
        return f"Refusing to delete '{skill_dir.name}': could not resolve path ({exc})."
    if resolved == root:
        return (
            f"Refusing to delete: path resolves to the skills root itself, "
            f"which would archive every skill."
        )
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return f"Refusing to delete '{skill_dir.name}': path is outside the skills root."
    if not rel.parts:
        return f"Refusing to delete '{skill_dir.name}': resolves to the skills root."
    return None


def _pinned_guard(config_dir: Path, name: str) -> Optional[str]:
    """Return a refusal message if *name* is pinned in the sidecar, else None.

    Pin protects a skill from deletion only. Best-effort: an unreadable sidecar
    lets the delete through rather than block on broken telemetry.
    """
    try:
        rec = telemetry.get_record(config_dir, name)
        if rec.get("pinned"):
            return (
                f"Skill '{name}' is pinned and cannot be deleted. Unpin it first "
                f"(telemetry set-pinned false) if you really want to delete it."
            )
    except Exception:  # pragma: no cover - best-effort guard
        pass
    return None


def _delete_skill(config_dir: Path, name: str,
                  absorbed_into: Optional[str] = None) -> Dict[str, Any]:
    """Archive (never rmtree) a skill dir to skills/.archive/, then forget it.

    HITL note: the repo's approval gate is a Telegram inline-keyboard callback
    driven by the bot surface — it is not reachable synchronously from this
    stateless CLI. Per the backlog, delete therefore archives unconditionally;
    the HITL approval gate is deferred to the skill layer (the skill-manage
    SKILL.md surfaces the action to Daniel before invoking delete).
    """
    skill_dir = _find_skill(config_dir, name)
    if skill_dir is None:
        return {"success": False, "error": f"Skill '{name}' not found."}

    pinned_err = _pinned_guard(config_dir, name)
    if pinned_err:
        return {"success": False, "error": pinned_err}

    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        target_name = absorbed_into.strip()
        if target_name == name:
            return {"success": False,
                    "error": f"absorbed_into='{target_name}' cannot equal the deleted skill."}
        if _find_skill(config_dir, target_name) is None:
            return {"success": False, "error": (
                f"absorbed_into='{target_name}' does not exist. Create or patch "
                f"the umbrella skill first, then retry the delete.")}

    unsafe = _validate_delete_target(config_dir, skill_dir)
    if unsafe:
        return {"success": False, "error": unsafe}

    archive_root = _skills_root(config_dir) / ".archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    dest = archive_root / name
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        dest = archive_root / f"{name}-{stamp}"
    shutil.move(str(skill_dir), str(dest))

    absorbed = (
        absorbed_into.strip()
        if isinstance(absorbed_into, str) and absorbed_into.strip()
        else None
    )

    # Durable, reversible record: log where the dir went and which umbrella (if
    # any) it was absorbed into. This is what makes a consolidation merge
    # undoable — every absorbed sibling appears in the ledger paired with both
    # its umbrella and its archive path, so restore_skill can bring it back.
    try:
        telemetry.append_audit(config_dir, {
            "kind": "archive",
            "name": name,
            "absorbed_into": absorbed,
            "archive_path": str(dest),
        })
    except Exception:  # pragma: no cover - audit is best-effort
        pass

    message = f"Skill '{name}' archived to {dest}."
    if absorbed is not None:
        message += f" Content absorbed into '{absorbed}'."

    return {"success": True, "message": message, "archived_to": str(dest)}


# ---------------------------------------------------------------------------
# CLI — argparse + JSON stdout, mirroring the Phase 1 telemetry CLI shape.
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Agent skill-authoring tool (stateless)")
    parser.add_argument(
        "--action", required=True,
        choices=["create", "edit", "patch", "delete", "write_file", "remove_file"],
    )
    parser.add_argument("--config-dir", required=True, help="Mind config dir (.claude/.codex)")
    parser.add_argument("--harness", default="claude_cli", choices=sorted(SUPPORTED_HARNESSES))
    parser.add_argument("--name", required=True, help="Skill name")
    parser.add_argument("--content", help="Full SKILL.md content (create/edit)")
    parser.add_argument("--category", help="Optional category (create)")
    parser.add_argument("--file-path", help="Supporting file path (write_file/remove_file/patch)")
    parser.add_argument("--file-content", help="Supporting file content (write_file)")
    parser.add_argument("--old-string", help="Text to find (patch)")
    parser.add_argument("--new-string", help="Replacement text (patch)")
    parser.add_argument("--replace-all", action="store_true", help="Replace all matches (patch)")
    parser.add_argument("--absorbed-into", help="Umbrella skill name (delete)")

    args = parser.parse_args(argv)
    out = skill_manage(
        args.action, args.config_dir, args.harness,
        name=args.name, content=args.content, category=args.category,
        file_path=args.file_path, file_content=args.file_content,
        old_string=args.old_string, new_string=args.new_string,
        replace_all=args.replace_all, absorbed_into=args.absorbed_into,
    )
    print(out)
    result = json.loads(out)
    return 0 if result.get("success") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
