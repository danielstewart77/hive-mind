---
name: add-mind
description: Connects a mind to the Hive Mind system. For new local minds, scaffolds MIND.md and implementation.py, then registers. For remote minds, writes a MIND.md pointing at the external gateway. For re-registration, re-runs discovery against an existing folder.
argument-hint: "[name]"
---

# add-mind

`$ARGUMENTS[0]` = mind name. Ask if missing.

## Step 1 — Determine scenario

Check if `minds/$ARGUMENTS[0]/` directory exists:

- **No directory → Scenario A (new local) or B (remote).** Ask the user:
  - "Is this a local mind (runs in this Docker stack) or a remote mind (runs on another host)?"
  - If local → Scenario A
  - If remote → Scenario B

- **Directory exists → Scenario C (re-registration).** The mind folder is already there but may not be in the broker.

Collect from the user:
- `name` (from argument)
- `gateway_url` (default `http://hive_mind:8420` for local, ask for remote)
- `model` (e.g. `sonnet`, `gpt-oss:20b-32k`)
- `harness` (e.g. `claude_cli_claude`, `codex_cli_codex`)

For Scenario B: verify the external gateway is reachable before proceeding:
```bash
curl -sf <gateway_url>/broker/minds > /dev/null && echo "Reachable" || echo "UNREACHABLE"
```

## Step 2 — Create MIND.md (Scenarios A and B)

Write `minds/<name>/MIND.md`:

**Scenario A (new local):**
```markdown
---
name: <name>
model: <model>
harness: <harness>
gateway_url: <gateway_url>
---

# <Name>

<Ask the user for a brief identity description to use as the soul seed>
```

**Scenario B (remote):**
```markdown
---
name: <name>
model: <model>
harness: <harness>
gateway_url: <gateway_url>
remote: true
---
```

For Scenario C: MIND.md already exists — skip to Step 3.

## Step 3 — Scaffold implementation.py (Scenario A only)

List available templates:
```bash
ls mind_templates/*.py
```

Ask the user which template matches their harness + model family. Copy it:
```bash
mkdir -p minds/<name>
cp mind_templates/<selected>.py minds/<name>/implementation.py
```

Replace the `MIND_NAME` placeholder:
```bash
sed -i 's/MIND_NAME/<name>/g' minds/<name>/implementation.py
```

Create `minds/<name>/__init__.py` (empty file).

## Step 4 — Generate compose (if containerised)

Check if the MIND.md has a `container:` block. If it does, run `/generate-compose` to update `docker-compose.yml` with the new mind's service definition, then start the container:

```bash
docker compose up -d <name>
```

If the mind does NOT have a `container:` block, it runs inside the main `hive_mind` container (subprocess mode) — skip this step.

## Step 5 — Register with broker

```bash
curl -s -X POST http://localhost:8420/broker/minds \
  -H "Content-Type: application/json" \
  -d '{"name":"<name>","gateway_url":"<gateway_url>","model":"<model>","harness":"<harness>"}'
```

If the response contains an error, stop and surface it to the user.

## Step 6 — Verify routability

Create a test session:
```bash
curl -s -X POST http://localhost:8420/sessions \
  -H "Content-Type: application/json" \
  -d '{"owner_type":"test","owner_ref":"add-mind-verify","client_ref":"add-mind","mind_id":"<name>"}'
```

Extract the session ID from the response. Send a test message:
```bash
curl -s -X POST http://localhost:8420/sessions/<session_id>/message \
  -H "Content-Type: application/json" \
  -d '{"content":"Respond with exactly: registration verified."}'
```

Check if the response stream contains "registration verified". Then clean up:
```bash
curl -s -X DELETE http://localhost:8420/sessions/<session_id>
```

If routability check fails, surface the error clearly but do NOT roll back the registration — the mind is registered, it just couldn't respond yet.

## Step 7 — Report

Summarize:
- Scenario handled (A/B/C)
- Files created (MIND.md, implementation.py, __init__.py)
- Containerised (yes/no, compose updated)
- Broker registration status
- Routability verification result
