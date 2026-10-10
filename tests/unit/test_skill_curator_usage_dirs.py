"""Usage a skill earns under one harness keeps it alive under the others.

A mind's skills are rendered into every harness it can switch to, and each
harness's Stop hook bumps the sidecar in its own config dir. The curator
ages one dir's skills, so without the other dirs' activity a skill a mind
uses every day under codex or dsh archives itself out of its Claude dir.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _PROJECT_ROOT / "tools/stateless/skill_curator/skill_curator.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(curator, config_dir: Path, name: str, **record):
    tel = curator.telemetry
    skill = config_dir / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\nbody\n")
    data = tel.load_usage(config_dir)
    rec = tel._empty_record()
    rec.update(record)
    data[name] = rec
    tel.save_usage(config_dir, data)


def _record_usage(curator, config_dir: Path, name: str, when: datetime):
    """What a harness's Stop hook leaves behind: a bumped sidecar row."""
    tel = curator.telemetry
    data = tel.load_usage(config_dir)
    rec = tel._empty_record()
    rec.update({"use_count": 3, "last_used_at": when.isoformat()})
    data[name] = rec
    tel.save_usage(config_dir, data)


# 38
def test_skill_usage_counted_under_codex_and_dsh_reaches_the_curator(tmp_path, capsys):
    curator = _load(SCRIPT_PATH, "skill_curator_usage_dirs")
    now = datetime.now(timezone.utc)
    claude, codex, dsh = tmp_path / "claude", tmp_path / "codex", tmp_path / "dsh"
    long_ago = (now - timedelta(days=200)).isoformat()
    _seed(curator, claude, "used-under-codex", created_by="agent",
          last_used_at=long_ago, created_at=long_ago)
    _seed(curator, claude, "used-under-dsh", created_by="agent", state="stale",
          last_used_at=(now - timedelta(days=40)).isoformat(), created_at=long_ago)
    _seed(curator, claude, "used-nowhere", created_by="agent",
          last_used_at=long_ago, created_at=long_ago)
    _record_usage(curator, codex, "used-under-codex", now - timedelta(days=1))
    _record_usage(curator, dsh, "used-under-dsh", now - timedelta(days=1))

    curator.main([
        "--config-dir", str(claude), "--harness", "claude_cli",
        "--usage-dir", str(codex), "--usage-dir", str(dsh),
    ])

    counts = json.loads(capsys.readouterr().out)["counts"]
    usage = curator.telemetry.load_usage(claude)
    assert usage["used-under-codex"]["state"] == curator.STATE_ACTIVE
    assert (claude / "skills" / "used-under-codex" / "SKILL.md").exists()
    assert usage["used-under-dsh"]["state"] == curator.STATE_ACTIVE
    assert usage["used-nowhere"]["state"] == curator.STATE_ARCHIVED
    assert counts["archived"] == 1 and counts["reactivated"] == 1


# 38
def test_a_dry_run_counts_usage_from_the_other_harness_homes(tmp_path, capsys):
    curator = _load(SCRIPT_PATH, "skill_curator_usage_dry")
    now = datetime.now(timezone.utc)
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    long_ago = (now - timedelta(days=200)).isoformat()
    _seed(curator, claude, "used-under-codex", created_by="agent",
          last_used_at=long_ago, created_at=long_ago)
    _record_usage(curator, codex, "used-under-codex", now - timedelta(days=1))

    curator.main(["--config-dir", str(claude), "--dry-run", "--usage-dir", str(codex)])

    assert json.loads(capsys.readouterr().out)["counts"]["archived"] == 0


# 38
def test_the_same_skill_used_under_three_harnesses_counts_all_three(tmp_path):
    curator = _load(SCRIPT_PATH, "skill_curator_usage_sum")
    now = datetime.now(timezone.utc)
    claude, codex, dsh = tmp_path / "claude", tmp_path / "codex", tmp_path / "dsh"
    _seed(curator, claude, "shared", created_by="agent", use_count=2,
          created_at=now.isoformat())
    _record_usage(curator, codex, "shared", now - timedelta(days=2))
    _record_usage(curator, dsh, "shared", now - timedelta(days=1))

    row = curator.eligible_skill_rows(claude, [codex, dsh])[0]

    assert row["use_count"] == 2 + 3 + 3
    assert row["last_used_at"] == (now - timedelta(days=1)).isoformat()


# 38
def test_the_minds_declared_homes_count_without_being_named(tmp_path, capsys, monkeypatch):
    curator = _load(SCRIPT_PATH, "skill_curator_usage_env")
    now = datetime.now(timezone.utc)
    claude, dsh = tmp_path / "claude", tmp_path / "dsh"
    long_ago = (now - timedelta(days=200)).isoformat()
    _seed(curator, claude, "used-under-dsh", created_by="agent",
          last_used_at=long_ago, created_at=long_ago)
    _record_usage(curator, dsh, "used-under-dsh", now - timedelta(days=1))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("DSH_HOME", str(dsh))

    curator.main(["--config-dir", str(claude)])

    assert json.loads(capsys.readouterr().out)["counts"]["archived"] == 0
    assert curator.telemetry.load_usage(claude)["used-under-dsh"]["state"] == curator.STATE_ACTIVE
