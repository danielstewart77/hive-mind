#!/usr/bin/env python3
"""``/learn`` authoring-standards validator + prompt builder (stateless).

Two public surfaces, both deterministic and pure stdlib + PyYAML (no model,
no import-time side effects):

  - ``build_learn_prompt(user_request)`` — a port of Hermes'
    ``agent/learn_prompt.py::build_learn_prompt``, re-framed for hive_mind. It
    instructs the live agent to gather the described sources with the tools it
    already has (Read / Grep / web) and author ONE SKILL.md, saving it via the
    ``skill-manage`` skill (``skill_manage`` ``action="create"``). The embedded
    standards are the testable core of Hermes' ``_AUTHORING_STANDARDS``: kebab
    name, one-sentence description, the ordered body sections, exact-commands,
    and the ~100-200 line bound. Hermes-only frontmatter rules that conflict
    with hive_mind's ``skill_manage`` schema (``author: Hermes``, ``version``)
    are dropped — ``skill_manage`` renders the dialect itself.

  - ``validate_skill_md(content, *, harness)`` — a deterministic check of an
    authored SKILL.md against those standards. Returns
    ``{"valid": bool, "errors": [...], "warnings": [...]}``. It reuses
    ``skill_manage.split_frontmatter`` and ``skill_manage.strip_to_codex_frontmatter``
    (loaded by path) so the name regex and the Codex keep-set never drift from
    the Phase 2 writer.

The ``learn-skill`` SKILL.md invokes ``--action validate`` before saving, so a
failing skill is caught before it ever lands on disk. We do NOT validate prose
quality — only the deterministic, machine-checkable rules.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Reuse Phase 2's skill_manage frontmatter helpers — load by path.
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


# ---------------------------------------------------------------------------
# Standards constants (the testable core of Hermes' _AUTHORING_STANDARDS).
# ---------------------------------------------------------------------------

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Body sections, in the canonical order. The validator requires a top-level
# title, "## When to Use", and at least one of "## Procedure" / "## How to Run".
REQUIRED_SECTIONS_ANY = ("## Procedure", "## How to Run")
REQUIRED_SECTION_WHEN = "## When to Use"

# Line-count guidance: ~100-200 lines, with slack. Warn outside [30, 250];
# error only on the absurd (< 5 non-empty body lines or empty body).
LINE_WARN_LOW = 30
LINE_WARN_HIGH = 250
LINE_ERROR_LOW = 5


_AUTHORING_STANDARDS = """\
Follow the hive_mind skill-authoring standards exactly. These are the same
rules the deterministic validator enforces before the skill is saved:

Frontmatter:
- name: lowercase-hyphenated, <=64 chars, no spaces (^[a-z0-9][a-z0-9._-]*$).
- description: ONE sentence, ends with a period. State the capability, not the
  implementation. No marketing words (powerful, comprehensive, seamless,
  advanced, robust). Do NOT repeat the skill name. If the description contains
  a colon, wrap the whole value in double quotes.
    Good: `Search arXiv papers by keyword, author, or ID.`

Body section order (omit a section only if it genuinely has no content):
1. "# <Human Title>" then a 2-3 sentence intro: what it does, what it does NOT
   do, and the key dependency stance (e.g. "stdlib only").
2. "## When to Use" — bullet list of concrete trigger phrases.
3. "## Prerequisites" — exact env vars, install steps, credentials.
4. "## How to Run" — the canonical invocation.
5. "## Procedure" — numbered steps with copy-paste-exact commands.
6. "## Verification" — a single command/check that proves the skill worked.

Quality bar:
- Prefer exact commands, endpoint URLs, function signatures, and config keys
  that appear VERBATIM in the source. NEVER invent flags, paths, or APIs — if
  you didn't see it in the source, don't write it.
- Keep it tight and scannable: ~100 lines for a simple skill, ~200 for a
  complex one. Don't re-paste the source docs.
- Larger scripts/parsers belong in a `scripts/` file (add via `skill_manage`
  write_file), referenced from SKILL.md by relative path — not inlined."""


def build_learn_prompt(user_request: str) -> str:
    """Build the agent prompt for an open-ended ``/learn`` request.

    Port of Hermes ``build_learn_prompt``, re-framed for hive_mind: the agent
    gathers material with Read / Grep / web tools and saves via the
    ``skill-manage`` skill (``skill_manage`` ``action="create"``). An empty
    request falls back to the "workflow we just went through" default.
    """
    req = (user_request or "").strip()
    if not req:
        req = (
            "the workflow we just went through in this conversation — review "
            "the steps taken and distill them into a reusable skill"
        )

    return (
        "[/learn] The user wants you to learn a reusable skill from the "
        "source(s) they described below, and save it.\n\n"
        f"WHAT TO LEARN FROM:\n{req}\n\n"
        "Do this:\n"
        "1. Gather the material. Resolve whatever the user named using the "
        "tools you already have — Read / Grep for local files or directories, "
        "the web tools for URLs, the current conversation history if they "
        "referred to something you just did, and the text they pasted as-is. "
        "If the request is ambiguous about scope, make a reasonable choice and "
        "note it; do not stall.\n"
        "2. Author ONE SKILL.md following the standards below.\n"
        "3. Validate it: run `learn_validator.py --action validate` on your "
        "drafted content. Fix any errors it reports before saving.\n"
        "4. On a clean validation, save it with the `skill-manage` skill "
        "(`skill_manage` action=\"create\"). If the procedure needs a "
        "non-trivial script, add it under the skill's `scripts/` with "
        "`skill_manage` write_file and reference it by relative path.\n\n"
        f"{_AUTHORING_STANDARDS}\n\n"
        "When done, tell the user the skill name and a one-line summary of "
        "what it captured."
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _count_sentences(text: str) -> int:
    """Count terminal-punctuation sentence runs in *text*.

    A run of one or more of ``. ! ?`` counts as a single terminator (so
    ellipses and "?!" don't inflate the count). Trailing whitespace ignored.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    # Collapse runs of terminal punctuation to a single marker, then count.
    runs = re.findall(r"[.!?]+", stripped)
    return len(runs)


def validate_skill_md(content: str, *, harness: str) -> Dict[str, Any]:
    """Validate an authored SKILL.md string against the authoring standards.

    Deterministic checks (see module docstring). Returns
    ``{"valid": bool, "errors": [...], "warnings": [...]}``. ``valid`` is True
    iff ``errors`` is empty; warnings never block.
    """
    errors: List[str] = []
    warnings: List[str] = []

    sm = _load_skill_manage()

    if not content or not content.strip():
        return {"valid": False, "errors": ["Content cannot be empty."], "warnings": []}

    fields, body = sm.split_frontmatter(content)
    if fields is None:
        return {
            "valid": False,
            "errors": ["SKILL.md must start with a closed YAML frontmatter block (---)."],
            "warnings": [],
        }

    # --- name ---
    name = fields.get("name")
    if not name or not isinstance(name, str):
        errors.append("Frontmatter must include a 'name' field.")
    else:
        if len(name) > MAX_NAME_LENGTH:
            errors.append(f"name exceeds {MAX_NAME_LENGTH} characters.")
        if not VALID_NAME_RE.match(name):
            errors.append(
                f"Invalid name '{name}'. Use lowercase letters, numbers, hyphens, "
                "dots, and underscores; must start with a letter or digit."
            )

    # --- description ---
    desc = fields.get("description")
    if not desc or not isinstance(desc, str) or not desc.strip():
        errors.append("Frontmatter must include a non-empty 'description' field.")
    else:
        if len(desc) > MAX_DESCRIPTION_LENGTH:
            errors.append(f"description exceeds {MAX_DESCRIPTION_LENGTH} characters.")
        n_sentences = _count_sentences(desc)
        if n_sentences == 0:
            errors.append("description must be a single sentence ending in a period.")
        elif n_sentences > 1:
            errors.append(
                f"description must be ONE sentence (found {n_sentences}); "
                "state the capability in a single sentence ending with a period."
            )

    # --- body sections ---
    body_stripped = body.strip()
    if not body_stripped:
        errors.append("SKILL.md must have a body after the frontmatter.")
    else:
        # Top-level title.
        if not re.search(r"^#\s+\S", body, flags=re.MULTILINE):
            errors.append("Body must include a top-level '# <Title>' heading.")
        if REQUIRED_SECTION_WHEN not in body:
            errors.append(f"Body must include a '{REQUIRED_SECTION_WHEN}' section.")
        if not any(sec in body for sec in REQUIRED_SECTIONS_ANY):
            errors.append(
                "Body must include at least one of "
                f"{' / '.join(REQUIRED_SECTIONS_ANY)}."
            )

    # --- line-count bound ---
    body_lines = [ln for ln in body.splitlines() if ln.strip()]
    n_body_lines = len(body_lines)
    if body_stripped:
        if n_body_lines < LINE_ERROR_LOW:
            errors.append(
                f"Body has {n_body_lines} non-empty lines — too short to be a "
                "usable skill (minimum 5)."
            )
        elif n_body_lines < LINE_WARN_LOW:
            warnings.append(
                f"Body has only {n_body_lines} lines; the guidance is ~100-200 "
                "lines for a complete skill."
            )
        elif n_body_lines > LINE_WARN_HIGH:
            warnings.append(
                f"Body has {n_body_lines} lines; the guidance is ~100-200 lines. "
                "Consider moving detail into references/ or scripts/."
            )

    # --- codex dialect: flag extra frontmatter keys that would be stripped ---
    if harness == "codex_cli":
        kept = sm.strip_to_codex_frontmatter(fields)
        extra = sorted(k for k in fields if k not in kept)
        if extra:
            warnings.append(
                "Codex frontmatter keeps only name/description/argument-hint; "
                f"these keys will be stripped on save: {', '.join(extra)}."
            )

    return {"valid": not errors, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="/learn authoring-standards validator + prompt builder"
    )
    parser.add_argument("--action", required=True, choices=["validate", "prompt"])
    parser.add_argument(
        "--harness", default="claude_cli", choices=["claude_cli", "codex_cli"]
    )
    parser.add_argument("--content", help="SKILL.md content (action=validate)")
    parser.add_argument("--request", help="User request text (action=prompt)")

    args = parser.parse_args(argv)

    if args.action == "prompt":
        print(json.dumps({"prompt": build_learn_prompt(args.request or "")}, ensure_ascii=False))
        return 0

    if not args.content:
        print(json.dumps({"valid": False, "errors": ["--content is required for validate."],
                          "warnings": []}, ensure_ascii=False))
        return 1
    result = validate_skill_md(args.content, harness=args.harness)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["valid"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
