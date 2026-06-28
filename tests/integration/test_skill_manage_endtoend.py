"""Step 6 — skill_manage acceptance integration test.

The committed acceptance proof for Phase 2. Exercises the same code path the
``skill-manage`` SKILL.md invokes, using ``tmp_path`` config dirs for both
harnesses (never the live gitignored mind dirs). Proves: per-harness create
writes a real dir with the right frontmatter dialect; two minds stay independent;
delete archives rather than removes.
"""

import importlib.util
import json
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_manage/skill_manage.py")


def _load():
    spec = importlib.util.spec_from_file_location("skill_manage_e2e", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _content(name="demo", body="Do the thing well."):
    return f"---\nname: {name}\ndescription: A demo skill\n---\n{body}\n"


def _read_fm(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    end = text.index("\n---", 3)
    return yaml.safe_load(text[3:end])


def test_ada_create_real_dir_full_frontmatter(tmp_path):
    mod = _load()
    cfg = tmp_path / "ada" / ".claude"
    r = json.loads(mod.skill_manage("create", str(cfg), "claude_cli",
                                    name="demo", content=_content()))
    assert r["success"] is True
    skill_md = cfg / "skills" / "demo" / "SKILL.md"
    assert skill_md.exists()
    assert not (cfg / "skills" / "demo").is_symlink()
    fm = _read_fm(skill_md)
    assert fm["name"] == "demo"
    assert fm["metadata"]["provenance"] == "agent"
    assert mod.telemetry.get_record(cfg, "demo")["created_by"] == "agent"


def test_nagatha_create_minimal_frontmatter(tmp_path):
    mod = _load()
    cfg = tmp_path / "nagatha" / ".codex"
    r = json.loads(mod.skill_manage("create", str(cfg), "codex_cli",
                                    name="demo", content=_content()))
    assert r["success"] is True
    skill_md = cfg / "skills" / "demo" / "SKILL.md"
    assert skill_md.exists()
    fm = _read_fm(skill_md)
    assert set(fm.keys()) == {"name", "description"}
    assert "metadata" not in fm
    assert mod.telemetry.get_record(cfg, "demo")["created_by"] == "agent"


def test_two_minds_independent(tmp_path):
    mod = _load()
    ada = tmp_path / "ada" / ".claude"
    nag = tmp_path / "nagatha" / ".codex"
    mod.skill_manage("create", str(ada), "claude_cli", name="shared-name", content=_content("shared-name"))
    mod.skill_manage("create", str(nag), "codex_cli", name="shared-name", content=_content("shared-name"))

    assert (ada / "skills" / "shared-name" / "SKILL.md").exists()
    assert (nag / "skills" / "shared-name" / "SKILL.md").exists()
    # full vs minimal frontmatter — no cross-mind bleed
    assert "metadata" in _read_fm(ada / "skills" / "shared-name" / "SKILL.md")
    assert "metadata" not in _read_fm(nag / "skills" / "shared-name" / "SKILL.md")
    # separate sidecars
    assert "shared-name" in mod.telemetry.load_usage(ada)
    assert "shared-name" in mod.telemetry.load_usage(nag)


def test_delete_then_archive_present(tmp_path):
    mod = _load()
    cfg = tmp_path / "ada" / ".claude"
    mod.skill_manage("create", str(cfg), "claude_cli", name="demo", content=_content())
    mod.skill_manage("delete", str(cfg), "claude_cli", name="demo")
    assert not (cfg / "skills" / "demo").exists()
    assert (cfg / "skills" / ".archive" / "demo" / "SKILL.md").exists()
    assert "demo" not in mod.telemetry.load_usage(cfg)
