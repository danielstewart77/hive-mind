"""Phase 3.3 acceptance proof — end-to-end deterministic curator run.

Tmp_path config dirs for both harnesses, seeded timestamps, exercising the same
``run`` / ``apply_automatic_transitions`` code path the scheduled invocation
uses. Proves the harness-identical core, per-mind transitions, reactivation,
plugin-untouched, and two-mind independence.
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
    return _load(SCRIPT_PATH, "skill_curator_e2e")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_e2e")


def _seed(config_dir: Path, name: str, *, created_by="agent", state="active",
          pinned=False, last_used_at=None, created_at=None, symlink_target=None):
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
    rec["last_used_at"] = last_used_at.isoformat() if last_used_at else None
    rec["created_at"] = (created_at or NOW).isoformat()
    data[name] = rec
    tel.save_usage(config_dir, data)


def _full_matrix(config_dir: Path, plugin_src: Path):
    """Seed the standard transition matrix into *config_dir*."""
    _seed(config_dir, "stale_one", last_used_at=NOW - timedelta(days=31))
    _seed(config_dir, "old_one", last_used_at=NOW - timedelta(days=91))
    _seed(config_dir, "pinned_one", pinned=True,
          last_used_at=NOW - timedelta(days=200))
    _seed(config_dir, "plugin_one", last_used_at=NOW - timedelta(days=200),
          symlink_target=plugin_src)
    _seed(config_dir, "software", last_used_at=NOW - timedelta(days=200))


def _assert_matrix_transitions(cur, tel, config_dir: Path, plugin_src: Path):
    summary = cur.run(config_dir, "claude_cli", now=NOW)

    assert tel.get_record(config_dir, "stale_one")["state"] == "stale"
    assert tel.get_record(config_dir, "old_one")["state"] == "archived"
    assert (config_dir / "skills" / ".archive" / "old_one" / "SKILL.md").is_file()
    assert tel.get_record(config_dir, "pinned_one")["state"] == "active"
    assert (config_dir / "skills" / "pinned_one").is_dir()
    # Plugin untouched.
    assert (config_dir / "skills" / "plugin_one").is_symlink()
    # Protected router untouched.
    assert tel.get_record(config_dir, "software")["state"] == "active"
    assert (config_dir / "skills" / "software").is_dir()

    counts = summary["counts"]
    # checked = agent-created, real-dir, non-router → stale_one, old_one,
    # pinned_one (plugin + software excluded by eligibility).
    assert counts["checked"] == 3
    assert counts["marked_stale"] == 1
    assert counts["archived"] == 1
    assert counts["reactivated"] == 0

    state = cur._curator_state_path(config_dir)
    import json
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["marked_stale"] == 1
    assert saved["archived"] == 1
    return summary


def test_ada_seeded_run_produces_transitions(tmp_path):
    cur, tel = _curator(), _telemetry()
    ada = tmp_path / "ada" / ".claude"
    plugin_src = tmp_path / "ada_plugin_src" / "plugin_one"
    _full_matrix(ada, plugin_src)
    _assert_matrix_transitions(cur, tel, ada, plugin_src)


def test_nagatha_seeded_run_produces_transitions(tmp_path):
    cur, tel = _curator(), _telemetry()
    nag = tmp_path / "nagatha" / ".codex"
    plugin_src = tmp_path / "nag_plugin_src" / "plugin_one"
    _full_matrix(nag, plugin_src)
    # Same matrix, codex harness — proves harness-identical core.
    summary = cur.run(nag, "codex_cli", now=NOW)
    assert summary["harness"] == "codex_cli"
    assert tel.get_record(nag, "stale_one")["state"] == "stale"
    assert tel.get_record(nag, "old_one")["state"] == "archived"
    assert (nag / "skills" / ".archive" / "old_one" / "SKILL.md").is_file()
    assert tel.get_record(nag, "pinned_one")["state"] == "active"
    assert (nag / "skills" / "plugin_one").is_symlink()
    assert tel.get_record(nag, "software")["state"] == "active"
    assert summary["counts"]["marked_stale"] == 1
    assert summary["counts"]["archived"] == 1


def test_reactivation_endtoend(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "ada" / ".claude"
    _seed(cfg, "revived", state="stale", last_used_at=NOW - timedelta(days=1))
    summary = cur.run(cfg, "claude_cli", now=NOW)
    assert tel.get_record(cfg, "revived")["state"] == "active"
    assert summary["counts"]["reactivated"] == 1


def test_two_minds_independent(tmp_path):
    cur, tel = _curator(), _telemetry()
    ada = tmp_path / "ada" / ".claude"
    nag = tmp_path / "nagatha" / ".codex"
    _seed(ada, "ada_skill", last_used_at=NOW - timedelta(days=91))
    _seed(nag, "nag_skill", last_used_at=NOW - timedelta(days=31))

    cur.run(ada, "claude_cli", now=NOW)
    cur.run(nag, "codex_cli", now=NOW)

    # Ada's archive happened in Ada's tree only.
    assert tel.get_record(ada, "ada_skill")["state"] == "archived"
    assert (ada / "skills" / ".archive" / "ada_skill").is_dir()
    assert not (nag / "skills" / ".archive" / "ada_skill").exists()
    # Nagatha's stale happened in Nagatha's tree only; no ada_skill record there.
    assert tel.get_record(nag, "nag_skill")["state"] == "stale"
    assert "ada_skill" not in tel.load_usage(nag)
    assert "nag_skill" not in tel.load_usage(ada)
