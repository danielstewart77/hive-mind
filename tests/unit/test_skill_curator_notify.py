"""Curator --notify: on-change summary with undo hint, test-mode safe.

A changed live run composes a notify message that NAMES the changed skills and,
under HERMES_NOTIFY_TEST, invokes the notify subprocess with ``--test-mode`` (no
real Telegram). An unchanged run sends nothing. Tests drive a deterministic
clock and assert on the composed message + the invoked argv — they do not stand
up a real notification channel.
"""

import importlib.util
import json
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
    return _load(CURATOR_PATH, "skill_curator_notify_under_test")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_curnotify_under_test")


def _seed_skill(cfg, name, *, state="active", last_used_at=None, created_at=None):
    tel = _telemetry()
    skills = cfg / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    d = skills / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    data = tel.load_usage(cfg)
    rec = tel._empty_record()
    rec["created_by"] = "agent"
    rec["state"] = state
    rec["last_used_at"] = last_used_at.isoformat() if last_used_at else None
    rec["created_at"] = (created_at or NOW).isoformat()
    data[name] = rec
    tel.save_usage(cfg, data)


def test_changed_run_composes_message_and_passes_test_mode(tmp_path, monkeypatch):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=31))
    _seed_skill(cfg, "ancient", last_used_at=NOW - timedelta(days=91))
    summary = cur.run(cfg, "claude_cli", now=NOW)

    monkeypatch.setenv("HERMES_NOTIFY_TEST", "1")
    cmd = cur.maybe_notify(cfg, summary)
    assert cmd is not None
    assert "--test-mode" in cmd
    assert "send" in cmd
    # The message names what changed and carries the undo hint.
    msg = cmd[cmd.index("--message") + 1]
    assert "ancient" in msg          # archived
    assert "rusty" in msg            # staled
    assert "--action restore" in msg
    assert str(cfg) in msg


def test_unchanged_run_sends_no_notification(tmp_path, monkeypatch):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "fresh", last_used_at=NOW - timedelta(days=1))
    summary = cur.run(cfg, "claude_cli", now=NOW)

    monkeypatch.setenv("HERMES_NOTIFY_TEST", "1")
    assert cur.maybe_notify(cfg, summary) is None


def test_cli_notify_flag_is_accepted(tmp_path, monkeypatch, capsys):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "rusty", last_used_at=NOW - timedelta(days=31))
    monkeypatch.setenv("HERMES_NOTIFY_TEST", "1")
    rc = cur.main(["--config-dir", str(cfg), "--notify"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["marked_stale"] == 1


def test_dry_run_with_notify_sends_nothing(tmp_path, monkeypatch, capsys):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _seed_skill(cfg, "ancient", last_used_at=NOW - timedelta(days=91))
    monkeypatch.setenv("HERMES_NOTIFY_TEST", "1")
    # A dry-run mutates nothing and must not notify — exercised via main().
    rc = cur.main(["--config-dir", str(cfg), "--notify", "--dry-run"])
    assert rc == 0
    # No archive happened (dry run), so the skill is still live.
    assert (cfg / "skills" / "ancient").is_dir()
