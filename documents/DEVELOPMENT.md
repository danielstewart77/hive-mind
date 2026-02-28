# Hive Mind — Development Guide

## Setup

```bash
# Clone and configure
git clone <repo>
cd hive_mind
cp config.yaml.example config.yaml   # fill in your IDs
cp .env.example .env                  # fill in your secrets

# Run locally (outside Docker)
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python server.py                       # gateway on :8420
python scheduler.py                    # scheduled tasks
python voice_server.py                 # voice on :8422

# Run via Docker (from project root — important, see note below)
docker compose up -d --build
```

**Docker rebuild note:** Always run `docker compose` from the project root
(`/home/USER/Storage/Dev/hive_mind/`). The `volumes: - .:/usr/src/app` mounts
are relative to CWD. Running from elsewhere will map an empty directory and the
container will fail to find its source files.

---

## Adding a New MCP Tool

1. Create `agents/your_tool.py`:
   ```python
   from agent_tooling import tool

   @tool(tags=["category"])
   def your_tool(param: str) -> str:
       """Clear one-sentence description of what this does."""
       # Return raw data as JSON string — Claude formats for the user
       return json.dumps({"result": ...})
   ```
2. The MCP server auto-discovers all `@tool`-decorated functions in `agents/`.
3. No registration step needed.
4. Restart the MCP server process (or Claude Code session) to pick up the new tool.

**Rules for agent tools:**
- Return raw data (JSON strings preferred) — never format for display
- Read credentials via `os.getenv("KEY")` or `keyring.get_password("hive-mind", "KEY")`
- No module-level side effects (no DB connections at import time)
- Catch specific exceptions; return `{"error": "brief description"}` on failure

---

## Testing

### Unit tests
```bash
# Run the test suite
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=agents --cov-report=term-missing
```

### Testing MCP tools manually
```python
# From a Python shell with venv active:
from agents.your_tool import your_tool
result = your_tool(param="test")
print(result)
```

### Testing the gateway
```bash
# Health check
curl http://localhost:8420/health

# Create a session and send a message
SESSION=$(curl -s -X POST http://localhost:8420/sessions \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet"}' | jq -r '.session_id')

curl -X POST http://localhost:8420/sessions/$SESSION/message \
  -H "Content-Type: application/json" \
  -d '{"content": "What tools do you have?"}' \
  --no-buffer
```

### Testing the voice server
```bash
# Health check
curl http://localhost:8422/health

# Test TTS (saves to test.ogg)
curl -X POST http://localhost:8422/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Testing voice output", "voice": "bf_alice"}' \
  -o test.ogg
```

### Testing scheduled tasks manually
```python
# Trigger a task immediately (bypasses cron schedule)
import asyncio
from scheduler import run_task
asyncio.run(run_task(0))  # task index from config.yaml
```

---

## Rollback Strategy

### Code rollback
```bash
# See recent commits
git log --oneline -20

# Roll back to a specific commit (creates new commit, preserves history)
git revert <commit-hash>

# Emergency: hard reset to last known good state (destructive — local only)
git reset --hard <commit-hash>
```

### Container rollback
Docker images are tagged at build time. To roll back a service:
```bash
# List available images
docker images hive_mind-server

# Roll back to previous image tag
docker compose stop server
docker tag hive_mind-server:previous hive_mind-server:latest
docker compose up -d server
```

**Recommended:** Before any significant change, tag the current images:
```bash
docker tag hive_mind-voice-server:latest hive_mind-voice-server:stable
```

### Scheduled task rollback
Scheduled tasks are defined in `config.yaml` (gitignored). To roll back a task:
1. Edit `config.yaml` directly
2. Restart the scheduler: `docker compose restart scheduler` (from project root)

### Database rollback
The session database (`data/sessions.db`) is ephemeral — sessions are short-lived.
No migration system is needed currently. If the DB is corrupted, delete it and
restart the server (sessions will be lost, which is acceptable).

---

## Branch Strategy

```
master          — stable, deployed
refactor/*      — active refactor work
feature/*       — new features
fix/*           — bug fixes
```

PRs require:
- [ ] No secrets or credentials in tracked files
- [ ] `.gitignore` updated if new generated/personal file types added
- [ ] `config.yaml.example` updated if `config.yaml` schema changed
- [ ] `documents/SEC_REVIEW.md` updated if security posture changed
- [ ] `goals.md` updated if new autonomous capabilities were added

---

## Security Practices

See `specs/security.md` for the full security policy and `documents/SEC_REVIEW.md`
for the current open findings.

**Key rules:**
- Secrets go in `.env` (gitignored) or system keyring — never in source code
- `config.yaml` is gitignored — it contains user IDs (Telegram, Discord)
- Agent tools use `get_credential()` helper: keyring first, env var fallback
- All `subprocess.run` calls must use list arguments (`shell=False`)
- New tools that create/execute code must be audit-logged

---

## Notification Channels

The system uses layered notification with automatic fallback:

| Priority | Channel | When available |
|----------|---------|----------------|
| 1 | Telegram bot | Normal operation |
| 2 | Direct Telegram API | Gateway down, bot up |
| 3 | Gmail (via MCP) | When Telegram is unreachable |
| 4 | Alert file | Last resort, always works |

The `notify_owner` MCP tool (`agents/notify.py`) implements channels 2–4.
Use it for system alerts, scheduled task failures, and self-improvement summaries.

Alert file location: `/usr/src/app/data/alerts.log`

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_BOT_TOKEN` | For Discord | Discord bot token |
| `TELEGRAM_BOT_TOKEN` | For Telegram | Telegram bot token |
| `ANTHROPIC_API_KEY` | Yes | Claude API key (auto-set by Claude Code) |
| `NEO4J_URI` | For graph tools | Neo4j connection URI |
| `NEO4J_USERNAME` | For graph tools | Neo4j username |
| `NEO4J_PASSWORD` | For graph tools | Neo4j password |
| `VOICE_SERVER_URL` | For voice | Default: `http://voice-server:8422` |
| `HIVE_MIND_SERVER_URL` | For bots | Default: `http://server:8420` |
| `KOKORO_VOICE` | Optional | TTS voice preset. Default: `bf_alice` |
| `WHISPER_MODEL` | Optional | STT model size. Default: `medium` |
