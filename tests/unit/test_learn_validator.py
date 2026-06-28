"""Step 1 — /learn authoring-standards validator + build_learn_prompt port.

Tests the deterministic validator of an authored SKILL.md against the Hermes
authoring standards (ported, re-framed for hive_mind), plus the prompt builder.
Assert observable behavior only — return values / dict shapes.
"""

import importlib.util
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/learn_skill/learn_validator.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("learn_validator_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_VALID_SKILL = """\
---
name: arxiv-search
description: Search arXiv papers by keyword, author, or ID.
---

# arXiv Search

Search arXiv for academic papers. Does not download PDFs. Stdlib only.

## When to Use

- "find arxiv papers about X"
- "search arxiv by author"

## Procedure

1. Build the query string from the user's keywords.
2. Invoke the search endpoint via the terminal.
3. Return the titles and IDs.

## Verification

Run the example query and confirm at least one result returns.
"""


def test_build_learn_prompt_includes_request_and_standards():
    mod = _load_module()
    prompt = mod.build_learn_prompt("learn how to query the planka API")
    assert "learn how to query the planka API" in prompt
    # The kebab/description/section-order standards must be embedded.
    assert "lowercase" in prompt.lower()
    assert "description" in prompt.lower()
    assert "When to Use" in prompt
    assert "Procedure" in prompt
    # Re-framed to hive_mind: save via skill-manage / skill_manage create.
    assert "skill_manage" in prompt or "skill-manage" in prompt


def test_build_learn_prompt_default_when_empty():
    mod = _load_module()
    prompt = mod.build_learn_prompt("")
    assert "workflow we just went through" in prompt


def test_valid_skill_md_passes():
    mod = _load_module()
    result = mod.validate_skill_md(_VALID_SKILL, harness="claude_cli")
    assert result["valid"] is True, result
    assert result["errors"] == []


def test_bad_name_rejected():
    mod = _load_module()
    bad = _VALID_SKILL.replace("name: arxiv-search", "name: Arxiv Search")
    result = mod.validate_skill_md(bad, harness="claude_cli")
    assert result["valid"] is False
    assert any("name" in e.lower() for e in result["errors"])


def test_multi_sentence_description_rejected():
    mod = _load_module()
    multi = _VALID_SKILL.replace(
        "description: Search arXiv papers by keyword, author, or ID.",
        "description: Search arXiv. It is fast. It is great.",
    )
    result = mod.validate_skill_md(multi, harness="claude_cli")
    assert result["valid"] is False
    assert any("sentence" in e.lower() for e in result["errors"])


def test_missing_required_section_rejected():
    mod = _load_module()
    # Drop the Procedure section (and there is no How to Run).
    missing = _VALID_SKILL.replace(
        "## Procedure\n\n"
        "1. Build the query string from the user's keywords.\n"
        "2. Invoke the search endpoint via the terminal.\n"
        "3. Return the titles and IDs.\n\n",
        "",
    )
    result = mod.validate_skill_md(missing, harness="claude_cli")
    assert result["valid"] is False
    assert any(
        "procedure" in e.lower() or "how to run" in e.lower() for e in result["errors"]
    )


def test_line_count_bounds_warn_not_error():
    mod = _load_module()
    short = """\
---
name: tiny-skill
description: Do one small thing.
---

# Tiny Skill

A minimal skill.

## When to Use

- "do the small thing"

## Procedure

1. Do it.

## Verification

Confirm it happened.
"""
    result = mod.validate_skill_md(short, harness="claude_cli")
    assert result["valid"] is True, result
    assert any("line" in w.lower() for w in result["warnings"])

    empty_body = """\
---
name: empty-skill
description: Has no body.
---
"""
    result2 = mod.validate_skill_md(empty_body, harness="claude_cli")
    assert result2["valid"] is False


def test_codex_dialect_extra_frontmatter_warns():
    mod = _load_module()
    codex_extra = """\
---
name: codex-skill
description: A codex-targeted skill.
argument-hint: "[topic]"
user-invocable: true
schedule: "0 5 * * 1"
---

# Codex Skill

Body text here describing the skill.

## When to Use

- "run the codex skill"

## Procedure

1. Do the thing.

## Verification

Confirm it worked.
"""
    result = mod.validate_skill_md(codex_extra, harness="codex_cli")
    # Extra (stripped) keys are a warning, not an error.
    assert result["valid"] is True, result
    joined = " ".join(result["warnings"]).lower()
    assert "user-invocable" in joined or "schedule" in joined
