"""Step 2 — the Curator's own archive_skill helper (move dir + set_state).

Distinct from skill_manage delete: it KEEPS the sidecar record (set_state
archived, never forget) so a 90-day-stale skill stays counted and recoverable.
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
    return _load(SCRIPT_PATH, "skill_curator_under_test_a")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_under_test_a")


def _make_skill(config_dir: Path, name: str, *, symlink_target=None):
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
    tel.seed_record_if_missing(config_dir, name, created_by="agent")


def test_archive_moves_dir_and_sets_state(tmp_path):
    cur = _curator()
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    _make_skill(cfg, "doomed")

    ok, dest = cur.archive_skill(cfg, "doomed")
    assert ok is True
    assert dest is not None

    assert not (cfg / "skills" / "doomed").exists()
    assert (cfg / "skills" / ".archive" / "doomed" / "SKILL.md").is_file()

    rec = tel.get_record(cfg, "doomed")
    # Record still present (not forgotten) and archived.
    data = tel.load_usage(cfg)
    assert "doomed" in data
    assert rec["state"] == "archived"
    assert rec["archived_at"] is not None


def test_archive_collision_timestamp_suffix(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    # Pre-existing archive entry of the same name.
    pre = cfg / "skills" / ".archive" / "dup"
    pre.mkdir(parents=True, exist_ok=True)
    (pre / "SKILL.md").write_text("# old archived\n", encoding="utf-8")

    _make_skill(cfg, "dup")
    ok, dest = cur.archive_skill(cfg, "dup")
    assert ok is True
    # Original archive untouched.
    assert (cfg / "skills" / ".archive" / "dup" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# old archived\n"
    # New one landed under a timestamp-suffixed dir.
    assert Path(dest).name != "dup"
    assert Path(dest).name.startswith("dup-")
    assert (Path(dest) / "SKILL.md").is_file()


def test_archive_symlink_refused(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    target = tmp_path / "plugin_src" / "linked"
    _make_skill(cfg, "linked", symlink_target=target)

    ok, dest = cur.archive_skill(cfg, "linked")
    assert ok is False
    assert dest is None
    # Link untouched.
    assert (cfg / "skills" / "linked").is_symlink()


def test_archive_missing_skill(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    (cfg / "skills").mkdir(parents=True)
    ok, dest = cur.archive_skill(cfg, "ghost")
    assert ok is False
    assert dest is None
