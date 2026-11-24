"""Per-harness skill-fire detectors for the usage telemetry sidecar.

Pure transcript→``set[str]`` detection that turns a parsed session into the
set of bare skill names that fired in it. Both detectors reuse the
already-tested transcript parsers — ``core.training_capture_claude._parse_grouped``
and ``core.training_capture_codex._parse_grouped`` — so there is no new hook
event and no second parse path to keep in sync. They take a transcript path and
return a plain ``set[str]`` of bare skill names; the shared
``skill_telemetry.bump_skills`` consumes that set, bumping ``use_count`` once
per distinct name.

Detection rules
---------------
**Claude (claude_code).** A skill fires either as a ``Skill`` tool-use block
(``{"type":"tool_use","name":"Skill","input":{"skill":"hivemind:planka"}}``)
or as a leading ``/slash`` token in a user turn. The ``input.skill`` value may
carry a ``namespace:`` prefix (plugin skills surface as ``hivemind:planka``);
the bare name is whatever follows the last ``:``.

**dsh**, read through ``dsh_transcript``'s translation into the Claude shape.
Its skill tool is ``skill`` and its arguments arrive as the JSON text the
model wrote, ``{"name": "backlog"}``, so the translated block is
``{"type":"tool_use","name":"skill","input":"{\"name\": \"backlog\"}"}``.
Arguments that are not JSON, and a name dsh's own pattern refuses (a
``namespace:`` prefix, capitals), name no skill: dsh loaded nothing.

**Codex (codex).** Codex loads a skill by reading its ``SKILL.md`` through an
``exec_command`` tool call, so the skill name appears inside the call's
``input`` (and may echo back in the ``tool_result``). Scanning the serialized
block text for ``skills/<name>/SKILL.md`` recovers the bare name regardless of
which arg value carries the path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.training_capture_claude import _parse_grouped as _parse_claude
from core.training_capture_codex import _parse_grouped as _parse_codex

# A leading ``/skill-name`` token at the very start of a user turn.
_SLASH_RE = re.compile(r"^/([a-z0-9][a-z0-9._-]*)")
# A ``skills/<name>/SKILL.md`` reference anywhere in a Codex block's text.
_CODEX_SKILL_RE = re.compile(r"skills/([a-z0-9][a-z0-9._-]*)/SKILL\.md")
# The only names dsh's skill tool accepts; it refuses any other before loading.
_DSH_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _bare_name(raw: str) -> str:
    """Strip any ``namespace:`` prefix, returning the bare skill name."""
    return raw.rsplit(":", 1)[-1]


def _invoked_skill(block: dict) -> object:
    """The skill a tool-use block invokes, in either harness's shape, or None."""
    name, args = block.get("name"), block.get("input")
    if name == "Skill":
        return args.get("skill") if isinstance(args, dict) else None
    if name != "skill":
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, RecursionError):
            return None
    skill = args.get("name") if isinstance(args, dict) else None
    # A name dsh refuses loaded nothing, so it is not a use.
    if not isinstance(skill, str) or not _DSH_SKILL_NAME_RE.match(skill):
        return None
    return skill


def detect_claude_skills(transcript_path: str | Path) -> set[str]:
    """Return the set of bare skill names that fired in a Claude transcript.

    Scans every ``Skill`` tool-use block's ``input.skill`` and every dsh
    ``skill`` block's ``name`` argument (namespace-stripped), and every user
    turn for a leading ``/slash`` command. Empty/None names are
    ignored.
    """
    names: set[str] = set()
    for user_content, blocks in _parse_claude(transcript_path):
        if isinstance(user_content, str):
            m = _SLASH_RE.match(user_content.strip())
            if m:
                names.add(m.group(1))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            skill = _invoked_skill(block)
            if isinstance(skill, str) and skill:
                bare = _bare_name(skill)
                if bare:
                    names.add(bare)
    return names


def _block_text(block: dict) -> str:
    """Flatten a Codex block's searchable text (input dict + tool_result)."""
    parts: list[str] = []
    inp = block.get("input")
    if inp is not None:
        try:
            parts.append(json.dumps(inp, ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(inp))
    content = block.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        parts.append(str(content))
    return "\n".join(parts)


def detect_codex_skills(transcript_path: str | Path) -> set[str]:
    """Return the set of bare skill names that fired in a Codex rollout.

    A Codex skill load reads ``skills/<name>/SKILL.md`` via an ``exec_command``
    tool call, so the name is recovered from the serialized block text of each
    ``tool_use`` ``input`` and any ``tool_result`` ``content``. Non-skill paths
    (e.g. ``/etc/hosts``) yield nothing.
    """
    names: set[str] = set()
    for _user_content, blocks in _parse_codex(transcript_path):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = _block_text(block)
            for m in _CODEX_SKILL_RE.finditer(text):
                names.add(m.group(1))
    return names
