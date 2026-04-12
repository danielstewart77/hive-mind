---
name: sync-discord-slash-commands
description: Syncs user-invocable Claude skills to Discord as slash commands via the Discord REST API. Use when you want Discord slash commands to reflect the current set of Claude skills.
---

# Sync Discord Slash Commands

Scans all user-invocable Claude skills and syncs them to Discord as slash commands.
Never modifies built-in bot commands.

## Built-in commands (never touch)
`sessions`, `new`, `clear`, `status`, `model`, `autopilot`, `switch`, `kill`

## STEP 1 — Collect Skills
Read all `~/.claude/skills/*/SKILL.md` files. Parse YAML frontmatter between `---` markers.
Keep only `user-invocable: true`. Extract per skill:
- `name` — Discord command name (must be lowercase, ≤32 chars, only `a-z 0-9 - _`)
- `description` — truncate to 100 chars
- `argument-hint` — optional; becomes a single STRING option named `args` if present

## STEP 2 — Get Token & App ID
Token: `os.environ.get("DISCORD_BOT_TOKEN")` → fallback `keyring.get_password("hive-mind", "DISCORD_BOT_TOKEN")`
App ID: `GET https://discord.com/api/v10/users/@me` with `Authorization: Bot <token>` → `.id`

## STEP 3 — Fetch Current Discord Commands
`GET https://discord.com/api/v10/applications/{app_id}/commands`
Split result into `builtin_cmds` (name in built-in list) and `skill_cmds` (everything else).

## STEP 4 — Sync
For each desired skill:
- Skip + warn if name conflicts with built-in
- Not in `skill_cmds` → `POST /applications/{app_id}/commands`
- Exists but description or options differ → `PATCH /applications/{app_id}/commands/{id}`

For each entry in `skill_cmds` with no matching skill → `DELETE /applications/{app_id}/commands/{id}`

## Command Payload Format
```json
{
  "name": "skill-name",
  "description": "description ≤100 chars",
  "type": 1,
  "options": [
    {
      "type": 3,
      "name": "args",
      "description": "<argument-hint text ≤100 chars>",
      "required": false
    }
  ]
}
```
Omit `options` entirely if skill has no `argument-hint`.

## STEP 5 — Report
Print per-command actions: `+ Added /name`, `~ Updated /name`, `- Deleted /name`, `! Failed /name: reason`
Print summary: `Done: N added, N updated, N deleted`
Note: Discord clients may take a few minutes to refresh.

## Implementation Notes
Write and run a Python script using only stdlib: `os`, `glob`, `re`, `json`, `urllib.request`.
Use `Authorization: Bot <token>` header and `Content-Type: application/json` on all requests.
DELETE returns 204 (no body) — don't parse it as JSON.
On 429 rate limit: parse `retry_after` from response, sleep that many seconds + 0.5s buffer, retry up to 3 times.
On other HTTP errors: print status + body and continue to next command.
