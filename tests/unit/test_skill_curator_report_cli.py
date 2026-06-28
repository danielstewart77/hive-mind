"""Step 4 — run report + CLI entry point."""

import importlib.util
import json
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
    return _load(SCRIPT_PATH, "skill_curator_under_test_r")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_under_test_r")


def _seed_skill(config_dir: Path, name: str, *, state="active",
                last_used_at=None, created_at=None):
    tel = _telemetry()
    skills = config_dir / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    d = skills / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    data = tel.load_usage(config_dir)
    rec = tel._empty_record()
    rec["created_by"] = "agent"
    rec["state"] = state
    rec["last_used_at"] = last_used_at.isoformat() if last_used_at else None
    rec["created_at"] = (created_at or NOW).isoformat()
    data[name] = rec
    tel.save_usage(config_dir, data)


def test_run_writes_curator_state(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=31))
    _seed_skill(cfg, "ancient", last_used_at=NOW - timedelta(days=91))
    cur.run(cfg, "claude_cli", now=NOW)

    state_path = cfg / "skills" / ".curator_state"
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_run_at"]
    assert state["marked_stale"] == 1
    assert state["archived"] == 1
    assert state["checked"] == 2
    assert state["reactivated"] == 0


def test_cli_runs_and_emits_json(tmp_path, capsys):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=31))
    rc = cur.main(["--config-dir", str(cfg), "--harness", "codex_cli"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    # CLI summary carries the counts.
    counts = payload.get("counts", payload)
    assert counts.get("marked_stale") == 1
    # And the .curator_state file was written.
    state = json.loads((cfg / "skills" / ".curator_state").read_text(encoding="utf-8"))
    assert state["marked_stale"] == 1


def test_dry_run_does_not_mutate(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "ancient", last_used_at=NOW - timedelta(days=91))
    cur.run(cfg, "claude_cli", now=NOW, dry_run=True)

    # State unchanged, dir live, no archive, no report written.
    assert tel.get_record(cfg, "ancient")["state"] == "active"
    assert (cfg / "skills" / "ancient").is_dir()
    assert not (cfg / "skills" / ".archive").exists()
    assert not (cfg / "skills" / ".curator_state").exists()


def test_consolidate_defaults_off(tmp_path, monkeypatch):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=1))

    called = {"n": 0}
    real = cur.maybe_consolidate

    def _spy(config_dir, harness, *, enabled):
        called["n"] += 1
        assert enabled is False
        return real(config_dir, harness, enabled=enabled)

    monkeypatch.setattr(cur, "maybe_consolidate", _spy)
    summary = cur.run(cfg, "claude_cli", now=NOW)
    # Hook is invoked but with enabled=False (no model spawn).
    assert called["n"] == 1
    assert summary["consolidation"]["ran"] is False
