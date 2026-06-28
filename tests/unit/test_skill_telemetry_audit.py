"""Audit ledger + restore — the shared undo layer in skill_telemetry.

The ledger (``<config_dir>/skills/.skill_audit.log``) is the single durable,
append-only record both the curator and skill_manage write to; ``restore_skill``
is the exact inverse of an archive move. All tests drive the public functions
and assert observable on-disk state (files, sidecar records, ledger contents).
"""

import importlib.util
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")
CURATOR_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_curator/skill_curator.py")

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_audit_under_test")


def _curator():
    return _load(CURATOR_PATH, "skill_curator_audit_under_test")


# ---------------------------------------------------------------------------
# append_audit / read_audit
# ---------------------------------------------------------------------------

def test_append_then_read_round_trip(tmp_path):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    tel.append_audit(cfg, {"kind": "archive", "name": "alpha"})
    tel.append_audit(cfg, {"kind": "restore", "name": "alpha"})
    entries = tel.read_audit(cfg)
    assert [e["kind"] for e in entries] == ["archive", "restore"]
    assert all("at" in e for e in entries)
    assert entries[0]["name"] == "alpha"


def test_append_audit_stamps_injected_now(tmp_path):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    tel.append_audit(cfg, {"kind": "archive", "name": "x"}, now=NOW)
    entries = tel.read_audit(cfg)
    assert entries[-1]["at"] == NOW.isoformat()


def test_read_audit_limit_returns_tail(tmp_path):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    for i in range(5):
        tel.append_audit(cfg, {"kind": "archive", "name": f"s{i}"})
    tail = tel.read_audit(cfg, limit=2)
    assert [e["name"] for e in tail] == ["s3", "s4"]


def test_read_audit_missing_ledger_is_empty(tmp_path):
    tel = _telemetry()
    assert tel.read_audit(tmp_path / "cfg") == []


def test_concurrent_appends_do_not_corrupt_lines(tmp_path):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    (cfg / "skills").mkdir(parents=True)

    def _worker(idx):
        for j in range(20):
            tel.append_audit(cfg, {"kind": "archive", "name": f"w{idx}-{j}"})

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    path = cfg / "skills" / ".skill_audit.log"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 100
    # Every line is a well-formed JSON object (no interleaving / partial writes).
    for ln in lines:
        obj = json.loads(ln)
        assert obj["kind"] == "archive"


# ---------------------------------------------------------------------------
# restore_skill
# ---------------------------------------------------------------------------

def _archive_via_curator(cfg, name):
    """Seed a live agent skill and archive it the real way (curator)."""
    cur, tel = _curator(), _telemetry()
    skills = cfg / "skills"
    d = skills / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    tel.seed_record_if_missing(cfg, name, created_by="agent")
    ok, dest = cur.archive_skill(cfg, name)
    assert ok
    return dest


def test_restore_round_trips_and_sets_active_and_audits(tmp_path):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    _archive_via_curator(cfg, "comeback")
    assert not (cfg / "skills" / "comeback").exists()

    ok, info = tel.restore_skill(cfg, "comeback")
    assert ok is True
    assert (cfg / "skills" / "comeback" / "SKILL.md").is_file()
    assert tel.get_record(cfg, "comeback")["state"] == "active"

    entries = tel.read_audit(cfg)
    restore_entries = [e for e in entries if e.get("kind") == "restore"]
    assert restore_entries and restore_entries[-1]["name"] == "comeback"


def test_restore_picks_newest_on_suffix_collision(tmp_path):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    archive = cfg / "skills" / ".archive"
    archive.mkdir(parents=True)
    # Plain copy (oldest) plus two timestamp-suffixed copies; newest = largest suffix.
    older = archive / "dup"
    older.mkdir()
    (older / "SKILL.md").write_text("OLD", encoding="utf-8")
    mid = archive / "dup-20260101T000000000000"
    mid.mkdir()
    (mid / "SKILL.md").write_text("MID", encoding="utf-8")
    newest = archive / "dup-20260601T000000000000"
    newest.mkdir()
    (newest / "SKILL.md").write_text("NEW", encoding="utf-8")

    ok, _ = tel.restore_skill(cfg, "dup")
    assert ok is True
    assert (cfg / "skills" / "dup" / "SKILL.md").read_text(encoding="utf-8") == "NEW"
    # The non-selected copies are left in place.
    assert older.exists() and mid.exists()


def test_restore_refused_when_live_exists_mutates_nothing(tmp_path):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    _archive_via_curator(cfg, "again")
    # Re-create a live skill of the same name.
    live = cfg / "skills" / "again"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("# live\n", encoding="utf-8")

    ok, reason = tel.restore_skill(cfg, "again")
    assert ok is False
    assert reason
    # Archive copy still present, nothing moved.
    assert (cfg / "skills" / ".archive" / "again").exists()
    assert "restore" not in [e.get("kind") for e in tel.read_audit(cfg)]


def test_restore_refused_when_no_archive_copy(tmp_path):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    ok, reason = tel.restore_skill(cfg, "ghost")
    assert ok is False
    assert reason
    assert tel.read_audit(cfg) == []


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def test_cli_audit_prints_ledger(tmp_path, capsys):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    tel.append_audit(cfg, {"kind": "archive", "name": "z"})
    rc = tel.main(["--action", "audit", "--config-dir", str(cfg)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out[-1]["name"] == "z"


def test_cli_restore_round_trips(tmp_path, capsys):
    tel = _telemetry()
    cfg = tmp_path / "cfg"
    _archive_via_curator(cfg, "clicomeback")
    rc = tel.main(["--action", "restore", "--config-dir", str(cfg),
                   "--skill", "clicomeback"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert (cfg / "skills" / "clicomeback" / "SKILL.md").is_file()
