"""Scheduled skill discovery.

Walks every mind's `.claude/skills/*/SKILL.md` and `.codex/skills/*/SKILL.md`,
extracts skills that declare a `schedule:` cron expression in frontmatter,
and returns them as a flat list the scheduler can register with APScheduler.

Schedule lives on the skill, not in `config.yaml`. Adding or removing a
scheduled task is the same operation as adding or removing a skill.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Chicago"


@dataclass(frozen=True)
class ScheduledSkill:
    """A single scheduled invocation: which mind, which skill, what cron.

    Sources: either a mind's SKILL.md frontmatter (dispatched by path
    reference) or a scheduler-owned YAML entry (dispatched with the
    instruction body embedded inline). YAML-sourced tasks set
    `instructions_path`; the scheduler reads the file at fire time so an
    edit to the instructions takes effect on the next fire without a
    container restart.
    """

    mind_id: str          # canonical UUID, used in gateway payloads.
                          # For command-typed tasks this is "scheduler" — no
                          # gateway dispatch happens, the field is kept for
                          # log labelling.
    mind_name: str        # short folder name, used in log labels
    skill_name: str
    skill_path: str       # absolute path to SKILL.md (SKILL.md-sourced) or
                          # the instructions file (YAML-sourced); used for logs
    cron: str
    timezone: str
    voice: bool
    notify: bool
    instructions_path: str | None = None  # when set, scheduler reads at fire time
    command: tuple[str, ...] | None = None  # when set, scheduler runs subprocess
                          # instead of dispatching a turn to a mind. Tasks
                          # with `command` set ignore mind / instructions_file.


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

    skill_files = sorted(
        list(minds_root.glob("*/.claude/skills/*/SKILL.md"))
        + list(minds_root.glob("*/.codex/skills/*/SKILL.md"))
    )
    for skill_md in skill_files:
        # minds_root/<mind_name>/(.claude|.codex)/skills/<skill_name>/SKILL.md
        try:
            mind_name = skill_md.relative_to(minds_root).parts[0]
            skill_name = skill_md.parent.name
        except (ValueError, IndexError):
            continue

        mind_id = _load_mind_uuid(minds_root / mind_name)
        if not mind_id:
            log.warning(
                "Skipping %s/%s — no mind_id in %s/runtime.yaml",
                mind_name, skill_name, mind_name,
            )
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
        if cron.lower() in {"", "null", "none"}:
            # Explicit opt-out — author wrote `schedule: null` or similar.
            # Silent skip; only real cron strings deserve a warning.
            continue
        if not _validate_cron(cron):
            log.warning(
                "Skipping %s/%s — invalid cron %r (need 5 fields)",
                mind_name, skill_name, cron,
            )
            continue

        found.append(ScheduledSkill(
            mind_id=mind_id,
            mind_name=mind_name,
            skill_name=skill_name,
            skill_path=str(skill_md),
            cron=cron,
            timezone=fm.get("schedule_timezone", DEFAULT_TIMEZONE),
            voice=_coerce_bool(fm.get("voice"), default=True),
            notify=_coerce_bool(fm.get("notify"), default=True),
        ))

    return found


def discover_scheduler_tasks(tasks_yaml: Path, minds_root: Path) -> list[ScheduledSkill]:
    """Discover scheduler-owned recurring tasks from `tasks_yaml`.

    Each entry names a mind, a cron, and an `instructions_file` (relative
    to the tasks_yaml's parent directory). The instruction body is read
    once at discovery time and stored on the ScheduledSkill so the
    scheduler can embed it inline in the dispatch message — no skill
    file involved, no discovery on the receiver.
    """
    found: list[ScheduledSkill] = []
    if not tasks_yaml.is_file():
        return found

    try:
        data = yaml.safe_load(tasks_yaml.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("Could not read %s: %s", tasks_yaml, exc)
        return found

    entries = data.get("tasks") or []
    instructions_root = tasks_yaml.parent / "instructions"

    for entry in entries:
        name = entry.get("name")
        cron = (entry.get("cron") or "").strip()
        if not (name and cron):
            log.warning("Skipping malformed scheduler task entry: %r", entry)
            continue
        if not _validate_cron(cron):
            log.warning(
                "Skipping scheduler task %s — invalid cron %r (need 5 fields)",
                name, cron,
            )
            continue

        command = entry.get("command")
        if command:
            if isinstance(command, str):
                command_tuple: tuple[str, ...] = tuple(command.split())
            elif isinstance(command, list) and all(isinstance(p, str) for p in command):
                command_tuple = tuple(command)
            else:
                log.warning(
                    "Skipping scheduler task %s — `command` must be a string or list[str]",
                    name,
                )
                continue
            found.append(ScheduledSkill(
                mind_id="scheduler",
                mind_name="scheduler",
                skill_name=name,
                skill_path=command_tuple[0],
                cron=cron,
                timezone=entry.get("timezone") or DEFAULT_TIMEZONE,
                voice=bool(entry.get("voice", False)),
                notify=bool(entry.get("notify", False)),
                command=command_tuple,
            ))
            continue

        mind_name = entry.get("mind")
        instructions_file = entry.get("instructions_file")
        if not (mind_name and instructions_file):
            log.warning("Skipping malformed scheduler task entry: %r", entry)
            continue

        mind_id = _load_mind_uuid(minds_root / mind_name)
        if not mind_id:
            log.warning(
                "Skipping scheduler task %s — no mind_id in %s/runtime.yaml",
                name, mind_name,
            )
            continue

        instructions_path = (instructions_root / instructions_file).resolve()
        if not instructions_path.is_file():
            log.warning(
                "Skipping scheduler task %s — instructions file not found: %s",
                name, instructions_path,
            )
            continue

        found.append(ScheduledSkill(
            mind_id=mind_id,
            mind_name=mind_name,
            skill_name=name,
            skill_path=str(instructions_path),
            cron=cron,
            timezone=entry.get("timezone") or DEFAULT_TIMEZONE,
            voice=bool(entry.get("voice", True)),
            notify=bool(entry.get("notify", True)),
            instructions_path=str(instructions_path),
        ))

    return found


_MIND_ID_RE = re.compile(r"^\s*mind_id\s*:\s*(.+?)\s*$", re.MULTILINE)


def _load_mind_uuid(mind_dir: Path) -> str | None:
    """Read `mind_id:` out of a mind's runtime.yaml. Returns None if absent."""
    runtime = mind_dir / "runtime.yaml"
    try:
        text = runtime.read_text()
    except OSError:
        return None
    m = _MIND_ID_RE.search(text)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'") or None
