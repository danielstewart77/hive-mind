"""Scheduled skill discovery.

Walks every mind's `.claude/skills/*/SKILL.md`, extracts skills that declare
a `schedule:` cron expression in frontmatter, and returns them as a flat
list the scheduler can register with APScheduler.

Schedule lives on the skill, not in `config.yaml`. Adding or removing a
scheduled task is the same operation as adding or removing a skill.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Chicago"


@dataclass(frozen=True)
class ScheduledSkill:
    """A single scheduled invocation: which mind, which skill, what cron."""

    mind_id: str
    skill_name: str
    cron: str
    timezone: str
    voice: bool
    notify: bool


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str] | None:
    """Return frontmatter as a flat str→str dict, or None if absent.

    Only handles the simple key: value forms our skills use today. Quoted
    values have surrounding quotes stripped. Unparseable lines are ignored.
    """
    m = _FRONTMATTER_RE.search(text)
    if not m:
        return None
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _coerce_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in {"true", "yes", "1"}


def _validate_cron(cron: str) -> bool:
    """Cheap structural check: 5 whitespace-separated fields."""
    return len(cron.split()) == 5


def discover_scheduled_skills(minds_root: Path) -> list[ScheduledSkill]:
    """Discover all scheduled skills across every mind under `minds_root`.

    A skill is considered scheduled if its frontmatter contains a `schedule`
    field with a 5-field cron expression. `schedule_timezone` defaults to
    America/Chicago. `voice` and `notify` default to True / True.

    Skills with malformed cron expressions are logged and skipped — one bad
    skill must not take down the scheduler.
    """
    found: list[ScheduledSkill] = []
    if not minds_root.is_dir():
        return found

    for skill_md in sorted(minds_root.glob("*/.claude/skills/*/SKILL.md")):
        # minds_root/<mind_id>/.claude/skills/<skill_name>/SKILL.md
        try:
            mind_id = skill_md.relative_to(minds_root).parts[0]
            skill_name = skill_md.parent.name
        except (ValueError, IndexError):
            continue

        try:
            text = skill_md.read_text()
        except OSError as exc:
            log.warning("Could not read %s: %s", skill_md, exc)
            continue

        fm = _parse_frontmatter(text)
        if not fm or "schedule" not in fm:
            continue

        cron = fm["schedule"].strip()
        if not _validate_cron(cron):
            log.warning(
                "Skipping %s/%s — invalid cron %r (need 5 fields)",
                mind_id, skill_name, cron,
            )
            continue

        found.append(ScheduledSkill(
            mind_id=mind_id,
            skill_name=skill_name,
            cron=cron,
            timezone=fm.get("schedule_timezone", DEFAULT_TIMEZONE),
            voice=_coerce_bool(fm.get("voice"), default=True),
            notify=_coerce_bool(fm.get("notify"), default=True),
        ))

    return found
