# Story State Tracker

Story: [#12] Fix secrets.py credential retrieval — use keyring CLI directly in skills
Card: 1753883640334386357
Branch: story/secrets-keyring-cli

## Progress
- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][X] Implement with TDD
- [state 4][ ] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [ ] All skills in `/home/hivemind/.claude-config/skills/` audited for `secrets.py get` calls
- [ ] All `secrets.py get` calls replaced with `python3 -m keyring get` pattern
- [ ] Pattern used: `TOKEN=$(python3 -m keyring get hive-mind REMOTE_ADMIN_TOKEN 2>/dev/null || echo "$REMOTE_ADMIN_TOKEN")`
- [ ] `tools/stateless/secrets/secrets.py` gutted or reduced to `set`-only
- [ ] remote-admin skill updated with new keyring CLI invocations
- [ ] Credential retrieval tested end-to-end in both Ada container and Telegram bot context

## Implementation Notes

**Key points:**
1. This is primarily a plugin repository change (skills, not core hive_mind code)
2. Keyring library already available as dependency
3. New pattern: `python3 -m keyring get hive-mind <KEY_NAME>` with env var fallback
4. Reduces complexity by replacing wrapper around working CLI tool

**Audit targets:**
- `/home/hivemind/dev/hivemind-claude-plugin/skills/` — all skill SKILL.md files
- Any hive_mind Python code calling `tools/stateless/secrets/secrets.py`

**Testing contexts:**
- Ada container (keyring file accessible at `data/keyring/`)
- Telegram bot (no keyring access, uses env vars)
