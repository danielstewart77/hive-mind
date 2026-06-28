"""Step 5 — proposer scheduling fork.

Asserts the committed Nagatha (codex) proposer task is registered in
bots/scheduled_tasks/tasks.yaml with a command pointing at skill_proposer.py.
Ada's schedule rides in its SKILL.md frontmatter (gitignored harness-native, no
test) — Codex strips `schedule:`, so Nagatha's cadence is the command task here.
"""

from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASKS_YAML = _PROJECT_ROOT / "bots/scheduled_tasks/tasks.yaml"


def _load_tasks():
    data = yaml.safe_load(TASKS_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data.get("tasks", [])


def test_nagatha_proposer_task_registered():
    tasks = _load_tasks()
    by_name = {t.get("name"): t for t in tasks if isinstance(t, dict)}
    assert "nagatha-skill-proposer" in by_name, by_name.keys()
    task = by_name["nagatha-skill-proposer"]

    # Command points at the proposer backend for the codex harness.
    command = task.get("command")
    assert command, task
    joined = " ".join(command) if isinstance(command, list) else str(command)
    assert "skill_proposer.py" in joined
    assert "--harness" in joined
    assert "codex_cli" in joined
    assert "--mind-id" in joined

    # A cron string is present.
    assert isinstance(task.get("cron"), str) and task["cron"].strip()


def test_nagatha_curator_and_proposer_tasks_notify_on_change():
    """Both scheduled Nagatha command tasks pass --notify so a changed run
    surfaces a Telegram summary (with the restore undo hint) on cron cadence."""
    tasks = _load_tasks()
    by_name = {t.get("name"): t for t in tasks if isinstance(t, dict)}
    for name in ("nagatha-skill-curator", "nagatha-skill-proposer"):
        assert name in by_name, by_name.keys()
        command = by_name[name].get("command")
        assert isinstance(command, list), command
        assert "--notify" in command, (name, command)
