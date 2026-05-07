"""Tests for core.scheduled_skills discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.scheduled_skills import (
    DEFAULT_TIMEZONE,
    ScheduledSkill,
    discover_scheduled_skills,
)


def _write_skill(
    minds_root: Path,
    mind_id: str,
    skill_name: str,
    frontmatter: str,
    body: str = "skill body",
) -> Path:
    skill_dir = minds_root / mind_id / ".claude" / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(f"---\n{frontmatter}\n---\n\n{body}\n")
    return skill_md


def test_returns_empty_when_minds_root_missing(tmp_path: Path):
    assert discover_scheduled_skills(tmp_path / "nope") == []


def test_skips_skills_without_schedule_field(tmp_path: Path):
    _write_skill(tmp_path, "ada", "no-cron", "name: no-cron\ndescription: x")
    assert discover_scheduled_skills(tmp_path) == []


def test_discovers_basic_cron(tmp_path: Path):
    _write_skill(
        tmp_path, "ada", "7am",
        "name: 7am\nschedule: \"0 7 * * *\"\nschedule_timezone: \"America/Chicago\"",
    )
    found = discover_scheduled_skills(tmp_path)
    assert found == [
        ScheduledSkill(
            mind_id="ada",
            skill_name="7am",
            cron="0 7 * * *",
            timezone="America/Chicago",
            voice=True,
            notify=True,
        ),
    ]


def test_defaults_timezone_when_omitted(tmp_path: Path):
    _write_skill(tmp_path, "ada", "fire", "name: fire\nschedule: \"*/5 * * * *\"")
    found = discover_scheduled_skills(tmp_path)
    assert len(found) == 1
    assert found[0].timezone == DEFAULT_TIMEZONE


def test_voice_and_notify_flags_parsed(tmp_path: Path):
    _write_skill(
        tmp_path, "ada", "silent",
        "name: silent\nschedule: \"0 3 * * *\"\nvoice: false\nnotify: false",
    )
    found = discover_scheduled_skills(tmp_path)
    assert found[0].voice is False
    assert found[0].notify is False


def test_invalid_cron_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    _write_skill(tmp_path, "ada", "bad", "name: bad\nschedule: \"every minute\"")
    found = discover_scheduled_skills(tmp_path)
    assert found == []
    assert any("invalid cron" in r.message.lower() for r in caplog.records)


def test_skill_without_frontmatter_is_skipped(tmp_path: Path):
    skill_dir = tmp_path / "ada" / ".claude" / "skills" / "naked"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# naked skill — no frontmatter\n")
    assert discover_scheduled_skills(tmp_path) == []


def test_walks_multiple_minds(tmp_path: Path):
    _write_skill(tmp_path, "ada", "morning", "name: morning\nschedule: \"0 7 * * *\"")
    _write_skill(tmp_path, "bob", "evening", "name: evening\nschedule: \"0 19 * * *\"")
    found = discover_scheduled_skills(tmp_path)
    minds = {(s.mind_id, s.skill_name) for s in found}
    assert minds == {("ada", "morning"), ("bob", "evening")}


def test_quoted_and_unquoted_cron_values_both_accepted(tmp_path: Path):
    _write_skill(tmp_path, "ada", "quoted", "name: quoted\nschedule: \"0 7 * * *\"")
    _write_skill(tmp_path, "ada", "bare", "name: bare\nschedule: 0 13 * * *")
    found = {s.skill_name: s.cron for s in discover_scheduled_skills(tmp_path)}
    assert found == {"quoted": "0 7 * * *", "bare": "0 13 * * *"}
