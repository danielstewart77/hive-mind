"""Local skill discovery for bot UIs.

Reads the active harness's skills directory (``~/.claude/skills/`` for
Claude Code, ``~/.codex/skills/`` for Codex) and returns user-invocable
skills extracted from each ``SKILL.md`` frontmatter. Bots use this to
render their ``/help`` menus and (Discord) to register slash-commands.

Per-host concern — each mind reads its own user's skills dir. Not
shared via NS.
"""

import glob
import os
import re


def _resolve_skills_dir() -> str:
    """Return the active skills directory for the current harness."""
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return os.path.join(os.path.expanduser(codex_home), "skills")

    claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_config_dir:
        return os.path.join(os.path.expanduser(claude_config_dir), "skills")

    default_codex_dir = os.path.expanduser("~/.codex/skills")
    if os.path.isdir(default_codex_dir):
        return default_codex_dir

    return os.path.expanduser("~/.claude/skills")


def get_skills() -> list[dict]:
    """Read all user-invocable skills from SKILL.md files."""
    skills = []
    skills_dir = _resolve_skills_dir()
    for path in sorted(glob.glob(os.path.join(skills_dir, "*/SKILL.md"))):
        try:
            with open(path) as f:
                content = f.read()
            m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if not m:
                continue
            fm: dict[str, str] = {}
            for line in m.group(1).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip().strip('"').strip("'")
            invocable = fm.get("user-invocable", fm.get("user_invocable", "")).lower()
            if invocable == "true" and (name := fm.get("name", "")):
                skills.append({
                    "name": name,
                    "description": fm.get("description", "")[:100],
                    "argument_hint": fm.get("argument-hint", ""),
                })
        except Exception:
            pass
    return skills
