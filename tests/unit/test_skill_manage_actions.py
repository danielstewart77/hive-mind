"""Step 4 — action dispatch: create / edit / patch / write_file / remove_file.

Drives the in-process ``skill_manage(action, config_dir, harness, **kw)``
dispatcher against ``tmp_path`` config dirs. Asserts on-disk state, returned JSON,
and the Phase 1 telemetry sidecar (created_by / patch_count).
"""

import importlib.util
import json
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_manage/skill_manage.py")


def _load():
    spec = importlib.util.spec_from_file_location("skill_manage_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _content(name="demo", desc="A demo skill", body="Do the thing."):
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n"


def _read_fm(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    end = text.index("\n---", 3)
    return yaml.safe_load(text[3:end])


def test_create_claude_dialect(tmp_path):
    mod = _load()
    r = json.loads(mod.skill_manage("create", str(tmp_path), "claude_cli",
                                    name="demo", content=_content()))
    assert r["success"] is True
    skill_md = tmp_path / "skills" / "demo" / "SKILL.md"
    assert skill_md.exists()
    fm = _read_fm(skill_md)
    assert fm["user-invocable"] is False
    assert fm["metadata"]["provenance"] == "agent"
    rec = mod.telemetry.get_record(tmp_path, "demo")
    assert rec["created_by"] == "agent"


def test_create_codex_dialect(tmp_path):
    mod = _load()
    r = json.loads(mod.skill_manage("create", str(tmp_path), "codex_cli",
                                    name="demo", content=_content()))
    assert r["success"] is True
    fm = _read_fm(tmp_path / "skills" / "demo" / "SKILL.md")
    assert "metadata" not in fm
    assert set(fm.keys()) == {"name", "description"}
    rec = mod.telemetry.get_record(tmp_path, "demo")
    assert rec["created_by"] == "agent"


def test_create_collision_rejected(tmp_path):
    mod = _load()
    mod.skill_manage("create", str(tmp_path), "claude_cli", name="demo", content=_content())
    r = json.loads(mod.skill_manage("create", str(tmp_path), "claude_cli",
                                    name="demo", content=_content()))
    assert r["success"] is False


def test_edit_full_rewrite_bumps_patch(tmp_path):
    mod = _load()
    mod.skill_manage("create", str(tmp_path), "claude_cli", name="demo", content=_content())
    before = mod.telemetry.get_record(tmp_path, "demo")["patch_count"]
    r = json.loads(mod.skill_manage("edit", str(tmp_path), "claude_cli",
                                    name="demo", content=_content(body="New body here.")))
    assert r["success"] is True
    after = mod.telemetry.get_record(tmp_path, "demo")["patch_count"]
    assert after == before + 1
    assert "New body here." in (tmp_path / "skills" / "demo" / "SKILL.md").read_text()


def test_patch_unique_match(tmp_path):
    mod = _load()
    body = "alpha unique-token beta gamma"
    mod.skill_manage("create", str(tmp_path), "claude_cli", name="demo",
                     content=_content(body=body))
    # unique match
    r = json.loads(mod.skill_manage("patch", str(tmp_path), "claude_cli", name="demo",
                                    old_string="unique-token", new_string="REPLACED"))
    assert r["success"] is True
    assert "REPLACED" in (tmp_path / "skills" / "demo" / "SKILL.md").read_text()
    p1 = mod.telemetry.get_record(tmp_path, "demo")["patch_count"]

    # multi-match without replace_all -> error, no bump
    mod.skill_manage("edit", str(tmp_path), "claude_cli", name="demo",
                     content=_content(body="dup dup dup"))
    p_before = mod.telemetry.get_record(tmp_path, "demo")["patch_count"]
    r = json.loads(mod.skill_manage("patch", str(tmp_path), "claude_cli", name="demo",
                                    old_string="dup", new_string="X"))
    assert r["success"] is False
    assert mod.telemetry.get_record(tmp_path, "demo")["patch_count"] == p_before

    # replace_all -> success
    r = json.loads(mod.skill_manage("patch", str(tmp_path), "claude_cli", name="demo",
                                    old_string="dup", new_string="X", replace_all=True))
    assert r["success"] is True
    assert (tmp_path / "skills" / "demo" / "SKILL.md").read_text().count("X") >= 3


def test_patch_breaking_frontmatter_rejected(tmp_path):
    mod = _load()
    mod.skill_manage("create", str(tmp_path), "claude_cli", name="demo", content=_content())
    skill_md = tmp_path / "skills" / "demo" / "SKILL.md"
    before = skill_md.read_text()
    r = json.loads(mod.skill_manage("patch", str(tmp_path), "claude_cli", name="demo",
                                    old_string="name: demo", new_string="renamed: demo"))
    assert r["success"] is False
    assert skill_md.read_text() == before  # unchanged


def test_write_file_and_remove_file(tmp_path):
    mod = _load()
    mod.skill_manage("create", str(tmp_path), "claude_cli", name="demo", content=_content())
    p0 = mod.telemetry.get_record(tmp_path, "demo")["patch_count"]

    r = json.loads(mod.skill_manage("write_file", str(tmp_path), "claude_cli", name="demo",
                                    file_path="references/a.md", file_content="hello"))
    assert r["success"] is True
    ref = tmp_path / "skills" / "demo" / "references" / "a.md"
    assert ref.read_text() == "hello"
    assert mod.telemetry.get_record(tmp_path, "demo")["patch_count"] == p0 + 1

    r = json.loads(mod.skill_manage("remove_file", str(tmp_path), "claude_cli", name="demo",
                                    file_path="references/a.md"))
    assert r["success"] is True
    assert not ref.exists()
    assert mod.telemetry.get_record(tmp_path, "demo")["patch_count"] == p0 + 2

    # bad subdir and missing skill rejected
    r = json.loads(mod.skill_manage("write_file", str(tmp_path), "claude_cli", name="demo",
                                    file_path="secret/x.md", file_content="x"))
    assert r["success"] is False
    r = json.loads(mod.skill_manage("write_file", str(tmp_path), "claude_cli", name="ghost",
                                    file_path="references/x.md", file_content="x"))
    assert r["success"] is False
