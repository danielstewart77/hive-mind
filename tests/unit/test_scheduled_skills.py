"""Tests for core.scheduled_skills discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.scheduled_skills import (
    DEFAULT_TIMEZONE,
    ScheduledSkill,
    discover_scheduled_skills,
)


_TEST_UUIDS = {
    "ada": "565e5a66-d20c-4266-872a-3268c4c894fc",
    "bob": "11111111-2222-3333-4444-555555555555",
}


def _write_skill(
    minds_root: Path,
    mind_name: str,
    skill_name: str,
    frontmatter: str,
    body: str = "skill body",
    harness: str = ".claude",
) -> Path:
    mind_dir = minds_root / mind_name
    mind_dir.mkdir(parents=True, exist_ok=True)
    uuid_ = _TEST_UUIDS.get(mind_name, f"00000000-0000-0000-0000-{mind_name:>012s}")
    (mind_dir / "runtime.yaml").write_text(f"name: {mind_name}\nmind_id: {uuid_}\n")
    skill_dir = mind_dir / harness / "skills" / skill_name
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
    skill_md = _write_skill(
        tmp_path, "ada", "7am",
        "name: 7am\nschedule: \"0 7 * * *\"\nschedule_timezone: \"America/Chicago\"",
    )
    found = discover_scheduled_skills(tmp_path)
    assert found == [
        ScheduledSkill(
            mind_id=_TEST_UUIDS["ada"],
            mind_name="ada",
            skill_name="7am",
            skill_path=str(skill_md),
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
    minds = {(s.mind_name, s.skill_name) for s in found}
    assert minds == {("ada", "morning"), ("bob", "evening")}
    by_name = {s.mind_name: s.mind_id for s in found}
    assert by_name == {"ada": _TEST_UUIDS["ada"], "bob": _TEST_UUIDS["bob"]}


def test_skips_when_runtime_yaml_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """A skill is only schedulable if its mind has a runtime.yaml with mind_id."""
    skill_dir = tmp_path / "orphan" / ".claude" / "skills" / "fire"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fire\nschedule: \"0 7 * * *\"\n---\nbody\n"
    )
    assert discover_scheduled_skills(tmp_path) == []
    assert any("no mind_id" in r.message.lower() for r in caplog.records)


def test_discovers_codex_harness_skills(tmp_path: Path):
    """Codex-harness minds keep their skills under `.codex/skills/` instead
    of `.claude/skills/`, but the SKILL.md schema is identical and the
    scheduler must walk both."""
    _write_skill(
        tmp_path, "ada", "morning",
        "name: morning\nschedule: \"0 7 * * *\"",
        harness=".claude",
    )
    _write_skill(
        tmp_path, "bob", "evening",
        "name: evening\nschedule: \"0 19 * * *\"",
        harness=".codex",
    )
    found = discover_scheduled_skills(tmp_path)
    by_skill = {(s.mind_name, s.skill_name): s for s in found}
    assert set(by_skill) == {("ada", "morning"), ("bob", "evening")}
    assert by_skill[("bob", "evening")].mind_id == _TEST_UUIDS["bob"]
    assert by_skill[("bob", "evening")].cron == "0 19 * * *"


def test_quoted_and_unquoted_cron_values_both_accepted(tmp_path: Path):
    _write_skill(tmp_path, "ada", "quoted", "name: quoted\nschedule: \"0 7 * * *\"")
    _write_skill(tmp_path, "ada", "bare", "name: bare\nschedule: 0 13 * * *")
    found = {s.skill_name: s.cron for s in discover_scheduled_skills(tmp_path)}
    assert found == {"quoted": "0 7 * * *", "bare": "0 13 * * *"}
