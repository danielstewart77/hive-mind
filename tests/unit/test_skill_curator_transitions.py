"""Step 3 — config loader + apply_automatic_transitions (deterministic core).

All tests drive seeded last_used_at/created_at + an injected ``now`` so the
clock is deterministic. Transitions assert observable sidecar state plus the
returned counter dict.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_curator/skill_curator.py")
TELEMETRY_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _curator():
    return _load(SCRIPT_PATH, "skill_curator_under_test_t")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_under_test_t")


def _iso(dt):
    return dt.isoformat()


def _seed_skill(config_dir: Path, name: str, *, created_by="agent",
                state="active", pinned=False, last_used_at=None,
                created_at=None, symlink_target=None):
    """Create a skill dir (real or symlink) and write a precise sidecar record."""
    tel = _telemetry()
    skills = config_dir / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    if symlink_target is not None:
        symlink_target.mkdir(parents=True, exist_ok=True)
        (symlink_target / "SKILL.md").write_text("# real\n", encoding="utf-8")
        (skills / name).symlink_to(symlink_target, target_is_directory=True)
    else:
        d = skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")

    data = tel.load_usage(config_dir)
    rec = tel._empty_record()
    rec["created_by"] = created_by
    rec["state"] = state
    rec["pinned"] = pinned
    rec["last_used_at"] = _iso(last_used_at) if last_used_at else None
    rec["created_at"] = _iso(created_at) if created_at else _iso(NOW)
    data[name] = rec
    tel.save_usage(config_dir, data)


def test_active_to_stale_at_31_days(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=31))
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert tel.get_record(cfg, "rusty")["state"] == "stale"
    assert counts["marked_stale"] == 1


def test_active_to_archived_at_91_days(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "ancient", last_used_at=NOW - timedelta(days=91))
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert tel.get_record(cfg, "ancient")["state"] == "archived"
    assert (cfg / "skills" / ".archive" / "ancient" / "SKILL.md").is_file()
    assert counts["archived"] == 1


def test_pinned_at_200_days_untouched(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "loved", pinned=True, last_used_at=NOW - timedelta(days=200))
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert tel.get_record(cfg, "loved")["state"] == "active"
    assert (cfg / "skills" / "loved").is_dir()
    assert counts["marked_stale"] == 0
    assert counts["archived"] == 0
    assert counts["reactivated"] == 0


def test_reactivation_stale_to_active(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "revived", state="stale", last_used_at=NOW - timedelta(days=1))
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert tel.get_record(cfg, "revived")["state"] == "active"
    assert counts["reactivated"] == 1


def test_never_used_anchors_on_created_at(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    # No last_*_at; created 10 days ago — should stay active, not archive.
    _seed_skill(cfg, "fresh", last_used_at=None, created_at=NOW - timedelta(days=10))
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert tel.get_record(cfg, "fresh")["state"] == "active"
    assert counts["archived"] == 0
    assert counts["marked_stale"] == 0


def test_symlinked_plugin_never_considered(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    target = tmp_path / "plugin_src" / "plug"
    _seed_skill(cfg, "plug", last_used_at=NOW - timedelta(days=200),
                symlink_target=target)
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert counts["checked"] == 0
    assert (cfg / "skills" / "plug").is_symlink()


def test_protected_router_never_archived(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "operations", last_used_at=NOW - timedelta(days=200))
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert tel.get_record(cfg, "operations")["state"] == "active"
    assert (cfg / "skills" / "operations").is_dir()
    assert counts["checked"] == 0


def test_config_overrides_defaults(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    (cfg / "skills").mkdir(parents=True)
    (cfg / "skills" / "curator.yaml").write_text(
        "stale_after_days: 10\n", encoding="utf-8"
    )
    _seed_skill(cfg, "quickrot", last_used_at=NOW - timedelta(days=15))
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert tel.get_record(cfg, "quickrot")["state"] == "stale"
    assert counts["marked_stale"] == 1


def test_load_curator_config_defaults_when_absent(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    conf = cur.load_curator_config(cfg)
    assert conf["stale_after_days"] == 30
    assert conf["archive_after_days"] == 90
    assert conf["min_idle_hours"] == 2
    assert conf["consolidate"] is False
