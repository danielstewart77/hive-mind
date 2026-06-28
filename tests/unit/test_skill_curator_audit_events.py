"""Named transition events + per-run curator audit entry.

``apply_automatic_transitions`` now returns an ``events`` list alongside its
counters; a live ``run`` records exactly one ``kind="curator"`` audit entry
carrying those events plus the consolidation verdict. Tests drive a deterministic
clock and assert the observable event shape and ledger contents.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
CURATOR_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_curator/skill_curator.py")
TELEMETRY_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _curator():
    return _load(CURATOR_PATH, "skill_curator_events_under_test")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_events_under_test")


def _seed_skill(config_dir, name, *, state="active", last_used_at=None,
                created_at=None):
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


def _event_for(events, name):
    matches = [e for e in events if e["name"] == name]
    assert matches, f"no event for {name}: {events}"
    return matches[0]


def test_transitions_emit_named_events(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=31))      # stale
    _seed_skill(cfg, "ancient", last_used_at=NOW - timedelta(days=91))    # archive
    _seed_skill(cfg, "revived", state="stale",
                last_used_at=NOW - timedelta(days=1))                     # reactivate

    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    events = counts["events"]
    assert len(events) == 3

    stale_ev = _event_for(events, "rusty")
    assert (stale_ev["from_state"], stale_ev["to_state"], stale_ev["action"]) == \
        ("active", "stale", "stale")

    arch_ev = _event_for(events, "ancient")
    assert (arch_ev["from_state"], arch_ev["to_state"], arch_ev["action"]) == \
        ("active", "archived", "archive")

    react_ev = _event_for(events, "revived")
    assert (react_ev["from_state"], react_ev["to_state"], react_ev["action"]) == \
        ("stale", "active", "reactivate")

    # Counts keys preserved.
    assert counts["marked_stale"] == 1
    assert counts["archived"] == 1
    assert counts["reactivated"] == 1
    assert counts["checked"] == 3


def test_no_transitions_yields_empty_events(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "fresh", last_used_at=NOW - timedelta(days=1))
    counts = cur.apply_automatic_transitions(cfg, now=NOW)
    assert counts["events"] == []


def test_run_report_carries_events(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=31))
    cur.run(cfg, "claude_cli", now=NOW)
    import json
    state = json.loads(
        (cfg / "skills" / ".curator_state").read_text(encoding="utf-8")
    )
    assert "events" in state
    assert state["events"][0]["name"] == "rusty"
    assert state["events"][0]["action"] == "stale"


def test_live_run_appends_exactly_one_curator_audit_entry(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=31))
    _seed_skill(cfg, "ancient", last_used_at=NOW - timedelta(days=91))
    cur.run(cfg, "claude_cli", now=NOW)

    entries = tel.read_audit(cfg)
    curator_entries = [e for e in entries if e.get("kind") == "curator"]
    assert len(curator_entries) == 1
    entry = curator_entries[0]
    names = {ev["name"] for ev in entry["events"]}
    assert names == {"rusty", "ancient"}
    assert entry["counts"]["marked_stale"] == 1
    assert entry["counts"]["archived"] == 1
    # Consolidation defaults off → recorded as not-run.
    assert entry["consolidation"]["ran"] is False


def test_dry_run_writes_no_audit_entry(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "ancient", last_used_at=NOW - timedelta(days=91))
    cur.run(cfg, "claude_cli", now=NOW, dry_run=True)
    assert tel.read_audit(cfg) == []
