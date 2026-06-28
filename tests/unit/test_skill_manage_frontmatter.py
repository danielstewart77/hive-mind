"""Step 1 — frontmatter rendering + shared Codex strip.

Tests the harness-aware frontmatter layer of skill_manage: the single shared
``strip_to_codex_frontmatter`` keep-set and the two render paths (full Claude
dialect with provenance metadata vs minimal Codex dialect). Assert observable
behavior only — the rendered YAML block parses back to the expected keys.
"""

import importlib.util
from datetime import datetime
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_manage/skill_manage.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("skill_manage_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_frontmatter(rendered: str) -> dict:
    """Parse a rendered ``---\\n...\\n---`` block back to a dict."""
    assert rendered.startswith("---"), rendered
    end = rendered.index("\n---", 3)
    return yaml.safe_load(rendered[3:end])


def test_codex_strip_keeps_only_portable():
    mod = _load_module()
    fm = {
        "name": "demo",
        "description": "A demo skill",
        "argument-hint": "[arg]",
        "user-invocable": False,
        "model": "opus",
        "schedule": "0 9 * * *",
        "schedule_timezone": "America/Chicago",
        "voice": "alloy",
        "tools": ["Bash"],
        "allowed-tools": ["Read"],
        "hooks": {"Stop": "x"},
        "metadata": {"provenance": "agent"},
    }
    stripped = mod.strip_to_codex_frontmatter(fm)
    assert stripped == {
        "name": "demo",
        "description": "A demo skill",
        "argument-hint": "[arg]",
    }


def test_render_claude_full():
    mod = _load_module()
    fields = {
        "name": "demo",
        "description": "A demo skill",
        "argument-hint": "[arg]",
    }
    rendered = mod.render_frontmatter(fields, "claude_cli")
    parsed = _parse_frontmatter(rendered)
    assert parsed["name"] == "demo"
    assert parsed["description"] == "A demo skill"
    assert parsed["argument-hint"] == "[arg]"
    assert parsed["user-invocable"] is False
    assert parsed["metadata"]["provenance"] == "agent"
    # authored_at is a parseable ISO-8601 timestamp
    datetime.fromisoformat(parsed["metadata"]["authored_at"])


def test_render_codex_minimal():
    mod = _load_module()
    fields = {
        "name": "demo",
        "description": "A demo skill",
        "argument-hint": "[arg]",
        "model": "opus",
        "schedule": "0 9 * * *",
    }
    rendered = mod.render_frontmatter(fields, "codex_cli")
    parsed = _parse_frontmatter(rendered)
    assert set(parsed.keys()) == {"name", "description", "argument-hint"}
    assert "metadata" not in parsed
    assert "user-invocable" not in parsed
    assert "model" not in parsed
    assert "schedule" not in parsed


def test_render_codex_no_provenance_in_frontmatter():
    mod = _load_module()
    fields = {"name": "demo", "description": "A demo skill"}
    rendered = mod.render_frontmatter(fields, "codex_cli")
    assert "provenance" not in rendered
    assert "metadata" not in rendered
    parsed = _parse_frontmatter(rendered)
    assert set(parsed.keys()) == {"name", "description"}


def test_unknown_harness_rejected():
    mod = _load_module()
    fields = {"name": "demo", "description": "A demo skill"}
    try:
        mod.render_frontmatter(fields, "bogus_cli")
    except ValueError as e:
        assert "harness" in str(e).lower()
    else:
        raise AssertionError("expected ValueError for unknown harness")
