"""Tests for the gate that decides whether a scheduled task fires at all.

A gate is a command the scheduler runs on the cron before any session
exists. Exit zero means the task has nothing to say and no mind is woken;
the signal code means fire; anything else is a broken gate and is reported
to Daniel rather than swallowed.

Each test names the requirement it protects.
"""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bots import scheduler
from core.scheduled_skills import ScheduledSkill, discover_scheduled_skills


def _skill(**overrides) -> ScheduledSkill:
    base = dict(
        mind_id="ada-uuid",
        mind_name="ada",
        skill_name="btc-buy-alerter",
        skill_path="/minds/ada/.claude/skills/btc-buy-alerter/SKILL.md",
        cron="0 * * * *",
        timezone="America/Chicago",
        voice=False,
        notify=False,
    )
    base.update(overrides)
    return ScheduledSkill(**base)


def _script(tmp_path: Path, name: str, body: str) -> tuple[str, ...]:
    """A real executable on disk, so the gate runs a process rather than a mock.

    The gate's whole job is to run something that is not this test.
    """
    path = tmp_path / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return (sys.executable, str(path))


# ---------------------------------------------------------------------------
# R1 — a gate declaration becomes the command that will be run
# ---------------------------------------------------------------------------
def test_gate_in_frontmatter_becomes_the_command_that_runs(tmp_path):
    d = tmp_path / "ada" / ".claude" / "skills" / "btc-buy-alerter"
    d.mkdir(parents=True)
    (tmp_path / "ada" / "runtime.yaml").write_text("mind_id: ada-uuid\n")
    (d / "SKILL.md").write_text(
        "---\n"
        "name: btc-buy-alerter\n"
        'schedule: "0 * * * *"\n'
        'gate: "/opt/venv/bin/python /tools/btc_gate.py --state /data/btc.json"\n'
        "---\n\nbody\n"
    )

    found = discover_scheduled_skills(tmp_path)

    assert len(found) == 1
    assert found[0].gate == (
        "/opt/venv/bin/python", "/tools/btc_gate.py", "--state", "/data/btc.json",
    )


def test_malformed_gate_is_dropped_rather_than_half_parsed(tmp_path):
    d = tmp_path / "ada" / ".claude" / "skills" / "btc-buy-alerter"
    d.mkdir(parents=True)
    (tmp_path / "ada" / "runtime.yaml").write_text("mind_id: ada-uuid\n")
    (d / "SKILL.md").write_text(
        "---\n"
        "name: btc-buy-alerter\n"
        'schedule: "0 * * * *"\n'
        "gate: python /tools/btc_gate.py --label 'unbalanced\n"
        "---\n\nbody\n"
    )

    found = discover_scheduled_skills(tmp_path)

    assert len(found) == 1
    assert found[0].gate is None


# ---------------------------------------------------------------------------
# R2/R3 — exit code decides whether anything is woken
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_exiting_zero_wakes_nothing(tmp_path):
    skill = _skill(
        discord_channel="1551239934051090462",
        gate=_script(tmp_path, "quiet.py", "raise SystemExit(0)\n"),
    )

    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as fired, \
         patch.object(scheduler, "_report_gate_failure", new=AsyncMock()) as reported:
        await scheduler.fire_skill(skill)

    fired.assert_not_awaited()
    reported.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_exiting_the_signal_code_fires_the_task(tmp_path):
    skill = _skill(
        discord_channel="1551239934051090462",
        gate=_script(tmp_path, "signal.py", "raise SystemExit(10)\n"),
    )

    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as fired, \
         patch.object(scheduler, "_report_gate_failure", new=AsyncMock()) as reported:
        await scheduler.fire_skill(skill)

    fired.assert_awaited_once()
    assert fired.await_args.args[1] == "1551239934051090462"
    reported.assert_not_awaited()


# ---------------------------------------------------------------------------
# R4 — a broken gate is loud, and does not fire
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_exiting_an_unrecognised_code_is_reported_and_does_not_fire(tmp_path):
    skill = _skill(
        discord_channel="1551239934051090462",
        gate=_script(tmp_path, "broken.py", "raise SystemExit(3)\n"),
    )

    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as fired, \
         patch.object(scheduler, "_report_gate_failure", new=AsyncMock()) as reported:
        await scheduler.fire_skill(skill)

    fired.assert_not_awaited()
    reported.assert_awaited_once()
    assert "3" in reported.await_args.args[1]


@pytest.mark.asyncio
async def test_gate_command_that_does_not_exist_is_reported_and_does_not_fire(tmp_path):
    missing = str(tmp_path / "not-installed")
    assert not os.path.exists(missing)
    skill = _skill(discord_channel="1551239934051090462", gate=(missing,))

    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as fired, \
         patch.object(scheduler, "_report_gate_failure", new=AsyncMock()) as reported:
        await scheduler.fire_skill(skill)

    fired.assert_not_awaited()
    reported.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_failure_reaches_daniel_on_telegram(tmp_path):
    sent: list[tuple[int, str]] = []

    async def _fake_send(token, chat_id, text):
        sent.append((chat_id, text))

    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}), \
         patch.object(scheduler.config, "telegram_owner_chat_id", 4242), \
         patch.object(scheduler, "_send_text", new=_fake_send):
        await scheduler._report_gate_failure("ada/btc-buy-alerter", "gate exited 3")

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == 4242
    assert "ada/btc-buy-alerter" in text and "gate exited 3" in text


# ---------------------------------------------------------------------------
# R5 — an overrunning gate is killed, reported, and does not fire
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_running_past_its_timeout_is_killed_and_reported(tmp_path):
    argv = _script(tmp_path, "hang.py", "import time\ntime.sleep(30)\n")

    outcome, reason = await scheduler.run_gate(argv, timeout=0.3)

    assert outcome == scheduler.GATE_ERROR
    assert "timed out" in reason


# ---------------------------------------------------------------------------
# R7 — a task with no gate is untouched
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_task_without_a_gate_fires_and_spawns_no_subprocess():
    skill = _skill(discord_channel="1551239934051090462", gate=None)

    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as fired, \
         patch.object(scheduler.asyncio, "create_subprocess_exec",
                      new=AsyncMock()) as spawned:
        await scheduler.fire_skill(skill)

    fired.assert_awaited_once()
    spawned.assert_not_awaited()
