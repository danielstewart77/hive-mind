"""Proposer --notify: on-create summary with undo hint, test-mode safe.

A run that created skills composes a notify message naming them and (under
HERMES_NOTIFY_TEST) invokes notify with ``--test-mode``; a run that created
nothing sends no notification. Fixture training_turns.db is built in tmp via
core.training_capture — never the live DB.
"""

import importlib.util
import sys
from pathlib import Path

from core import training_capture as tc

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_proposer/skill_proposer.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _proposer():
    return _load(SCRIPT_PATH, "skill_proposer_notify_under_test")


def _seed_seq(db, *, session_id, turn_index, mind_id, tools, captured_at):
    turn = tc.TrainingTurn.from_blocks(
        session_id=session_id, turn_index=turn_index,
        harness=tc.HARNESS_CLAUDE_CODE, mind_id=mind_id, user_content="x",
        assistant_blocks=[{"type": "tool_use", "name": n, "input": {}} for n in tools],
        captured_at=captured_at,
    )
    tc.upsert_turns(str(db), [turn])


def _enable(config_dir: Path, **overrides):
    skills = config_dir / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    lines = ["enabled: true"] + [f"{k}: {v}" for k, v in overrides.items()]
    (skills / "proposer.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_created_run_composes_message_and_passes_test_mode(tmp_path, monkeypatch):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg)
    db = tmp_path / "training_turns.db"
    for i in range(3):
        _seed_seq(db, session_id="s", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], captured_at=100 + i)
    summary = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    created = [p["name"] for p in summary["proposed"] if p["created"]]
    assert created

    monkeypatch.setenv("HERMES_NOTIFY_TEST", "1")
    cmd = prop.maybe_notify(cfg, summary)
    assert cmd is not None
    assert "--test-mode" in cmd
    msg = cmd[cmd.index("--message") + 1]
    assert created[0] in msg
    assert "--action restore" in msg
    assert str(cfg) in msg


def test_nothing_created_sends_no_notification(tmp_path, monkeypatch):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    # Disabled by default → proposes nothing.
    db = tmp_path / "training_turns.db"
    for i in range(3):
        _seed_seq(db, session_id="s", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], captured_at=100 + i)
    summary = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    assert summary == {"enabled": False, "proposed": []}

    monkeypatch.setenv("HERMES_NOTIFY_TEST", "1")
    assert prop.maybe_notify(cfg, summary) is None


def test_enabled_but_below_threshold_sends_nothing(tmp_path, monkeypatch):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg)
    db = tmp_path / "training_turns.db"
    # Only one occurrence — below the default min_frequency=3, nothing created.
    _seed_seq(db, session_id="s", turn_index=0, mind_id="ada",
              tools=["Read", "Grep"], captured_at=100)
    summary = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    assert [p for p in summary["proposed"] if p["created"]] == []

    monkeypatch.setenv("HERMES_NOTIFY_TEST", "1")
    assert prop.maybe_notify(cfg, summary) is None


def test_cli_notify_flag_accepted(tmp_path, monkeypatch, capsys):
    import json
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg)
    db = tmp_path / "training_turns.db"
    for i in range(3):
        _seed_seq(db, session_id="s", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], captured_at=100 + i)
    monkeypatch.setenv("HERMES_NOTIFY_TEST", "1")
    rc = prop.main(["--config-dir", str(cfg), "--db-path", str(db),
                    "--mind-id", "ada", "--notify"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is True
