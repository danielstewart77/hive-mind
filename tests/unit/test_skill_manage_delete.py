"""Step 5 — delete = archive-not-remove + guards + CLI.

``delete`` never ``rmtree``s a live skill dir: it moves it under
``skills/.archive/`` (timestamp suffix on collision) and forgets the sidecar
record. Guards: refuse a symlinked skill dir, refuse the skills root, refuse a
pinned skill, validate ``absorbed_into`` targets. Plus a subprocess CLI roundtrip.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_manage/skill_manage.py")


def _load():
    spec = importlib.util.spec_from_file_location("skill_manage_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _content(name="demo", body="Do the thing."):
    return f"---\nname: {name}\ndescription: A demo skill\n---\n{body}\n"


def _create(mod, cfg, name):
    return json.loads(mod.skill_manage("create", str(cfg), "claude_cli",
                                       name=name, content=_content(name)))


def test_delete_archives_not_removes(tmp_path):
    mod = _load()
    _create(mod, tmp_path, "demo")
    r = json.loads(mod.skill_manage("delete", str(tmp_path), "claude_cli", name="demo"))
    assert r["success"] is True
    assert not (tmp_path / "skills" / "demo").exists()
    assert (tmp_path / "skills" / ".archive" / "demo" / "SKILL.md").exists()
    # sidecar forgotten
    assert "demo" not in mod.telemetry.load_usage(tmp_path)


def test_delete_symlink_refused(tmp_path):
    mod = _load()
    external = tmp_path / "plugin_src"
    external.mkdir()
    (external / "SKILL.md").write_text(_content("plugin"), encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    link = skills / "plugin"
    os.symlink(str(external), str(link))

    r = json.loads(mod.skill_manage("delete", str(tmp_path), "claude_cli", name="plugin"))
    assert r["success"] is False
    assert link.exists()  # left intact
    assert (external / "SKILL.md").exists()


def test_delete_pinned_refused(tmp_path):
    mod = _load()
    _create(mod, tmp_path, "demo")
    mod.telemetry.set_pinned(tmp_path, "demo", True)
    r = json.loads(mod.skill_manage("delete", str(tmp_path), "claude_cli", name="demo"))
    assert r["success"] is False
    assert (tmp_path / "skills" / "demo").exists()  # not archived


def test_delete_archive_collision_suffixes(tmp_path):
    mod = _load()
    _create(mod, tmp_path, "demo")
    mod.skill_manage("delete", str(tmp_path), "claude_cli", name="demo")
    _create(mod, tmp_path, "demo")  # re-create same name
    mod.skill_manage("delete", str(tmp_path), "claude_cli", name="demo")
    archive = tmp_path / "skills" / ".archive"
    dirs = [p for p in archive.iterdir() if p.is_dir() and p.name.startswith("demo")]
    assert len(dirs) == 2  # two distinct archived dirs


def test_delete_absorbed_into_missing_target_rejected(tmp_path):
    mod = _load()
    _create(mod, tmp_path, "demo")
    r = json.loads(mod.skill_manage("delete", str(tmp_path), "claude_cli",
                                    name="demo", absorbed_into="ghost-umbrella"))
    assert r["success"] is False
    assert (tmp_path / "skills" / "demo").exists()


def test_cli_create_roundtrip(tmp_path):
    content = _content("cli-demo")
    r = subprocess.run(
        [sys.executable, SCRIPT_PATH, "--action", "create", "--config-dir", str(tmp_path),
         "--harness", "claude_cli", "--name", "cli-demo", "--content", content],
        capture_output=True, text=True, timeout=20,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["success"] is True
    assert (tmp_path / "skills" / "cli-demo" / "SKILL.md").exists()
