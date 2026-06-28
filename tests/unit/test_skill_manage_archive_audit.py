"""Archive logging in skill_manage delete + round-trip with restore_skill.

A ``delete`` archives the skill dir and logs a ``kind="archive"`` audit entry
capturing name + absorbed_into + archive_path. A subsequent
``telemetry.restore_skill`` brings the dir back and the ledger shows both events
in order — the chain that makes a consolidation merge reversible.
"""

import importlib.util
import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_manage/skill_manage.py")
TELEMETRY_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manage():
    return _load(SCRIPT_PATH, "skill_manage_archive_audit_under_test")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_archive_audit_under_test")


def _content(name="demo", body="Do the thing."):
    return f"---\nname: {name}\ndescription: A demo skill\n---\n{body}\n"


def _create(mod, cfg, name):
    return json.loads(mod.skill_manage("create", str(cfg), "claude_cli",
                                       name=name, content=_content(name)))


def test_delete_writes_archive_audit_entry(tmp_path):
    mod, tel = _manage(), _telemetry()
    _create(mod, tmp_path, "umbrella")
    _create(mod, tmp_path, "sibling")
    r = json.loads(mod.skill_manage("delete", str(tmp_path), "claude_cli",
                                    name="sibling", absorbed_into="umbrella"))
    assert r["success"] is True

    entries = tel.read_audit(tmp_path)
    archive_entries = [e for e in entries if e.get("kind") == "archive"]
    assert len(archive_entries) == 1
    entry = archive_entries[0]
    assert entry["name"] == "sibling"
    assert entry["absorbed_into"] == "umbrella"
    assert entry["archive_path"] == r["archived_to"]


def test_plain_prune_logs_absorbed_into_none(tmp_path):
    mod, tel = _manage(), _telemetry()
    _create(mod, tmp_path, "lonely")
    mod.skill_manage("delete", str(tmp_path), "claude_cli", name="lonely")
    entry = [e for e in tel.read_audit(tmp_path) if e.get("kind") == "archive"][-1]
    assert entry["name"] == "lonely"
    assert entry["absorbed_into"] is None


def test_archive_then_restore_round_trip_ledger_in_order(tmp_path):
    mod, tel = _manage(), _telemetry()
    _create(mod, tmp_path, "umbrella")
    _create(mod, tmp_path, "absorbed")

    mod.skill_manage("delete", str(tmp_path), "claude_cli",
                     name="absorbed", absorbed_into="umbrella")
    assert not (tmp_path / "skills" / "absorbed").exists()

    ok, _ = tel.restore_skill(tmp_path, "absorbed")
    assert ok is True
    assert (tmp_path / "skills" / "absorbed" / "SKILL.md").is_file()

    kinds = [e.get("kind") for e in tel.read_audit(tmp_path)]
    # archive precedes restore in the durable ledger.
    assert "archive" in kinds and "restore" in kinds
    assert kinds.index("archive") < kinds.index("restore")
