# Mind `.claude` Folder Convention

## Rule

Every mind owns its Claude configuration entirely. The `.claude` folder lives inside the mind's directory — nowhere else.

```
minds/
├── ada/
│   └── .claude/          ← Ada's complete Claude config
│       ├── settings.json
│       ├── settings.local.json
│       ├── hooks/
│       ├── skills/
│       ├── agents/
│       └── plugins/
├── bob/
│   └── .claude/
└── bilby/
    └── .claude/          ← Historical only; Bilby now uses `.codex`
```

## What Goes Here

Everything needed to run a mind's Claude harness belongs in its `.claude` folder:

| File/Folder | Purpose |
|---|---|
| `settings.json` | Hooks, plugins, model defaults, permission mode |
| `settings.local.json` | Host-specific permissions (not committed) |
| `skills/` | All Claude Code skills for this mind |
| `hooks/` | Session/stop hooks (soul nudge, identity load, etc.) |
| `agents/` | Custom subagent definitions |
| `plugins/` | Installed Claude plugins |

## What Does NOT Go Here

- **No `.claude/` at the repo root** — Claude should never read `hive_mind/.claude/`
- **No `.claude/` at `/home/hivemind/.claude`** — mind containers do not use the host user's Claude dir
- **No shared skills directory** — skills are per-mind; copy explicitly if another mind needs one

## Docker Wiring

Each mind container sets `CLAUDE_CONFIG_DIR` to point at its own `.claude` folder via bind mount:

```yaml
environment:
  - CLAUDE_CONFIG_DIR=/home/hivemind/.claude-config
volumes:
  - ${HOST_PROJECT_DIR:-.}/minds/ada/.claude:/home/hivemind/.claude-config:rw
  - ${HOST_CLAUDE_DIR:-~/.claude}:/home/hivemind/.host-claude:ro
```

- `CLAUDE_CONFIG_DIR` tells the Claude CLI where its config lives
- `minds/ada/.claude` is bind-mounted read-write — changes inside the container persist to the repo
- The host's `~/.claude` is mounted read-only at `.host-claude` for OAuth token access only

## Skill Creation

When creating skills, always write to `$CLAUDE_CONFIG_DIR/skills/`, not any hardcoded path:

```bash
mkdir -p $CLAUDE_CONFIG_DIR/skills/my-skill
cp /tmp/my-skill.md $CLAUDE_CONFIG_DIR/skills/my-skill/SKILL.md
```

The canonical (version-controlled) copy goes in `specs/skills/`:

```bash
mkdir -p /usr/src/app/specs/skills/my-skill
cp /tmp/my-skill.md /usr/src/app/specs/skills/my-skill/SKILL.md
```

## Sharing Skills Between Minds

Skills are not automatically shared. To give another mind a skill:

```bash
cp -r $CLAUDE_CONFIG_DIR/skills/my-skill /usr/src/app/minds/ada/.claude/skills/
```

Or bootstrap from the canonical copy in `specs/skills/`.

## Codex Equivalent

The same convention applies to Codex minds (Nagatha and Bilby). Replace `.claude` with `.codex` and `CLAUDE_CONFIG_DIR` with `CODEX_HOME`. See `docs/mind-codex-folder.md` (TODO) for details.
