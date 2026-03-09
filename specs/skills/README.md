# Skills

This directory is the canonical, version-controlled home for all Hive Mind Claude skills. It is the source of truth for skills that should exist in every deployment.

## Bootstrap (new machine or fresh container)

Copy these skills into your active Claude Code skills directory:

```bash
# Merge into your user-level skills (won't overwrite existing personal skills)
cp -rn specs/skills/. ~/.claude/skills/

# Or copy into the project-level skills directory
cp -rn specs/skills/. .claude/skills/
```

Use `-n` (no-clobber) to avoid overwriting skills you've customised locally. Review any conflicts manually.

## Creating New Skills

Use `/skill-creator-claude` — it automatically creates the skill in both:
- `.claude/skills/<name>/` — active, loaded by Claude Code
- `specs/skills/<name>/` — tracked here in the repo

Do not manually create skill folders in one location only. The creator handles both.

## Structure

Each skill is a subfolder containing exactly one `SKILL.md` file:

```
specs/skills/
  skill-name/
    SKILL.md
```

The folder name is the skill identifier. The file must be named `SKILL.md` (uppercase).
