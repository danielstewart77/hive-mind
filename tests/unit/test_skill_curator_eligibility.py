"""Step 1 — eligibility gate + skill rows for the deterministic curator.

Drives the importable helpers directly. Tmp_path config dirs, seeded sidecar
records, real and symlinked skill dirs. Asserts which on-disk skills are
curation-eligible per design-decision D2 (real-dir-not-symlink AND
created_by=="agent" AND not a protected router skill).
"""

import importlib.util
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_curator/skill_curator.py")
TELEMETRY_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _curator():
    return _load(SCRIPT_PATH, "skill_curator_under_test")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_under_test_c")


def _make_skill(config_dir: Path, name: str, *, created_by="agent",
                symlink_target=None):
    """Create a skill under <config_dir>/skills/<name>/ and seed its record.

    When *symlink_target* is given, the skill dir is a symlink to that real
    directory instead of a real dir.
    """
    skills = config_dir / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    tel = _telemetry()
    if symlink_target is not None:
        symlink_target.mkdir(parents=True, exist_ok=True)
        (symlink_target / "SKILL.md").write_text("# real\n", encoding="utf-8")
        (skills / name).symlink_to(symlink_target, target_is_directory=True)
    else:
        d = skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    tel.seed_record_if_missing(config_dir, name, created_by=created_by)


def _names(rows):
    return {r["name"] for r in rows}


def test_symlinked_skill_excluded(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    target = tmp_path / "plugin_src" / "linked"
    _make_skill(cfg, "linked", created_by="agent", symlink_target=target)
    rows = cur.eligible_skill_rows(cfg)
    assert "linked" not in _names(rows)


def test_human_created_excluded(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _make_skill(cfg, "humanish", created_by="human")
    rows = cur.eligible_skill_rows(cfg)
    assert "humanish" not in _names(rows)


def test_agent_created_included(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _make_skill(cfg, "agentish", created_by="agent")
    rows = cur.eligible_skill_rows(cfg)
    assert "agentish" in _names(rows)
    row = next(r for r in rows if r["name"] == "agentish")
    assert "last_activity_at" in row


def test_protected_router_excluded(tmp_path):
    cur = _curator()
    for name in ("software", "operations", "planning", "information", "communication"):
        cfg = tmp_path / name
        _make_skill(cfg, name, created_by="agent")
        rows = cur.eligible_skill_rows(cfg)
        assert name not in _names(rows), f"{name} must be protected"


def test_no_skills_dir_returns_empty(tmp_path):
    cur = _curator()
    cfg = tmp_path / "empty"
    cfg.mkdir()
    assert cur.eligible_skill_rows(cfg) == []
