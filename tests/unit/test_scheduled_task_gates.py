"""Tests for the gate that decides whether a scheduled task fires at all.

A gate is a command the scheduler runs on the cron before any session
exists. Exit zero means the task has nothing to say and no mind is woken;
the signal code means fire; anything else is a broken gate and is reported
to Daniel rather than swallowed.

Each test names the requirement it protects.
"""

import asyncio
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bots import scheduler
from core.scheduled_skills import (
    ScheduledSkill,
    discover_scheduled_skills,
    discover_scheduler_tasks,
)


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


def test_gate_keeps_a_quoted_argument_written_in_frontmatter(tmp_path):
    """Frontmatter is flat text, so the parser has to leave the author's own
    quoting intact — eating the inner closing quote makes a working gate
    unreadable, and an unreadable gate is a task that does not run."""
    d = tmp_path / "ada" / ".claude" / "skills" / "watcher"
    d.mkdir(parents=True)
    (tmp_path / "ada" / "runtime.yaml").write_text("mind_id: ada-uuid\n")
    (d / "SKILL.md").write_text(
        "---\nname: watcher\n"
        'schedule: "0 * * * *"\n'
        """gate: "python g.py --label 'deep value'"\n"""
        "---\n\nbody\n"
    )

    found = discover_scheduled_skills(tmp_path)

    assert found[0].gate == ("python", "g.py", "--label", "deep value")
    assert found[0].gate_error is None


def test_a_gate_that_collapses_to_nothing_is_an_error_not_an_opt_out(tmp_path):
    """A list written in flat frontmatter leaves an empty value behind. Read
    as "no gate", that task fires every hour instead of never."""
    d = tmp_path / "ada" / ".claude" / "skills" / "watcher"
    d.mkdir(parents=True)
    (tmp_path / "ada" / "runtime.yaml").write_text("mind_id: ada-uuid\n")
    (d / "SKILL.md").write_text(
        "---\nname: watcher\n"
        'schedule: "0 * * * *"\n'
        "gate:\n"
        "---\n\nbody\n"
    )

    found = discover_scheduled_skills(tmp_path)

    assert found[0].gate is None
    assert found[0].gate_error


def test_gate_as_a_yaml_list_keeps_an_argument_holding_a_space(tmp_path):
    """A list is how an argv entry containing a space is expressed at all."""
    (tmp_path / "ada").mkdir()
    (tmp_path / "ada" / "runtime.yaml").write_text("mind_id: ada-uuid\n")
    inst = tmp_path / "instructions"
    inst.mkdir()
    (inst / "body.md").write_text("do the thing\n")
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        "tasks:\n"
        "  - name: watcher\n"
        '    cron: "0 * * * *"\n'
        "    mind: ada\n"
        "    instructions_file: body.md\n"
        "    gate: ['python', 'g.py', '--label', 'deep value']\n"
    )

    found = discover_scheduler_tasks(tasks, tmp_path)

    assert len(found) == 1
    assert found[0].gate == ("python", "g.py", "--label", "deep value")


def test_gate_list_holding_a_non_string_is_refused(tmp_path):
    (tmp_path / "ada").mkdir()
    (tmp_path / "ada" / "runtime.yaml").write_text("mind_id: ada-uuid\n")
    inst = tmp_path / "instructions"
    inst.mkdir()
    (inst / "body.md").write_text("do the thing\n")
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        "tasks:\n"
        "  - name: watcher\n"
        '    cron: "0 * * * *"\n'
        "    mind: ada\n"
        "    instructions_file: body.md\n"
        "    gate: ['python', 7]\n"
    )

    found = discover_scheduler_tasks(tasks, tmp_path)

    assert found[0].gate is None
    assert found[0].gate_error


def test_gate_written_as_none_is_an_opt_out_not_a_command(tmp_path):
    """`gate: none` means no gate. Splitting it would run a program called
    `none`, and report that failure to Daniel every hour."""
    d = tmp_path / "ada" / ".claude" / "skills" / "btc-buy-alerter"
    d.mkdir(parents=True)
    (tmp_path / "ada" / "runtime.yaml").write_text("mind_id: ada-uuid\n")
    (d / "SKILL.md").write_text(
        "---\nname: btc-buy-alerter\n"
        'schedule: "0 * * * *"\n'
        "gate: none\n---\n\nbody\n"
    )

    found = discover_scheduled_skills(tmp_path)

    assert found[0].gate is None
    assert found[0].gate_error is None


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
    assert found[0].gate_error


@pytest.mark.asyncio
async def test_a_gate_that_cannot_be_read_declines_to_fire_and_says_so():
    """The one failure that could fail open. A task whose gate is a typo must
    not become the task that fires every hour instead of never."""
    skill = _skill(
        discord_channel="1551239934051090462",
        gate=None,
        gate_error="unreadable gate declaration: 'python g.py --label \'x'",
    )

    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as fired, \
         patch.object(scheduler, "_report_gate_failure", new=AsyncMock()) as reported:
        await scheduler.fire_skill(skill)

    fired.assert_not_awaited()
    reported.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_a_quiet_gate_creates_no_session_on_the_telegram_path(tmp_path):
    """The gate runs before dispatch, whichever surface the task delivers to."""
    skill = _skill(
        discord_channel=None, notify=True,
        gate=_script(tmp_path, "quiet.py", "raise SystemExit(0)\n"),
    )

    with patch.object(scheduler, "_create_session", new=AsyncMock()) as created, \
         patch.object(scheduler, "_send_message", new=AsyncMock()) as messaged:
        await scheduler.fire_skill(skill)

    created.assert_not_awaited()
    messaged.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_quiet_gate_stops_a_command_task_before_it_runs(tmp_path):
    """A `command` task is dispatched earlier than a mind turn; the gate is
    earlier still."""
    skill = _skill(
        discord_channel=None,
        command=("/bin/echo", "ran"),
        gate=_script(tmp_path, "quiet.py", "raise SystemExit(0)\n"),
    )

    with patch.object(scheduler, "_fire_command", new=AsyncMock()) as ran:
        await scheduler.fire_skill(skill)

    ran.assert_not_awaited()


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
    assert reported.await_args.args[0] == "ada/btc-buy-alerter"
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

    marker = tmp_path / "still-running"
    argv = _script(
        tmp_path, "hang.py",
        f"import time\ntime.sleep(1.5)\nopen({str(marker)!r}, 'w').write('alive')\n",
    )

    outcome, reason = await scheduler.run_gate(argv, timeout=0.3)
    await asyncio.sleep(2.0)

    assert outcome == scheduler.GATE_ERROR
    assert "timed out" in reason
    # The process is gone, not merely abandoned — an orphan per fire is an
    # orphan an hour.
    assert not marker.exists()


@pytest.mark.asyncio
async def test_a_timed_out_gate_takes_its_children_with_it(tmp_path):
    """A gate written as a wrapper spawns something. Killing only the wrapper
    leaves the grandchild running and holds the pipe open, so the scheduler
    waits for it anyway — and APScheduler skips every fire in the meantime."""
    marker = tmp_path / "grandchild-finished"
    child = tmp_path / "child.py"
    child.write_text(f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').write('alive')\n")
    argv = _script(
        tmp_path, "wrapper.py",
        f"import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
        f"time.sleep(30)\n",
    )

    started = asyncio.get_running_loop().time()
    outcome, reason = await scheduler.run_gate(argv, timeout=0.5)
    elapsed = asyncio.get_running_loop().time() - started
    await asyncio.sleep(3.5)

    assert outcome == scheduler.GATE_ERROR
    # The call returns on the timeout, not when the grandchild happens to end.
    assert elapsed < 2.0
    assert not marker.exists()


# ---------------------------------------------------------------------------
# R7 — a task with no gate is untouched
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_task_without_a_gate_fires_without_consulting_one():
    skill = _skill(discord_channel="1551239934051090462", gate=None)

    with patch.object(scheduler, "_fire_into_channel", new=AsyncMock()) as fired, \
         patch.object(scheduler, "run_gate", new=AsyncMock()) as gated:
        await scheduler.fire_skill(skill)

    fired.assert_awaited_once()
    gated.assert_not_awaited()


# ---------------------------------------------------------------------------
# R4 — what the report says
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_report_carries_what_the_gate_said_about_its_own_failure(tmp_path):
    """"gate exited 1" and "gate exited 1: ledger unreachable" send Daniel to
    two different places."""
    argv = _script(
        tmp_path, "noisy.py",
        "import sys\nprint('observation post failed: connection refused', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
    )

    outcome, reason = await scheduler.run_gate(argv, timeout=10)

    assert outcome == scheduler.GATE_ERROR
    assert "connection refused" in reason
