# Plan: Plugin Setup Loose Ends

> **Status:** Not yet implemented. These are open items identified during the hivemind-claude-plugin build (2026-04-12).
> **Context:** Once the plugin is published, users need a setup experience that handles these gaps cleanly.

---

## 1. TTS Provider in Setup ✓ DONE

The voice server (Chatterbox) is one option, but users may have no TTS, a different local engine, or an external TTS API.

**What's needed:**
- Add a TTS step to `setup-body` (or a new `setup-voice` skill)
- Present options:
  1. **None** — no voice, text-only operation
  2. **Chatterbox (local)** — current default; requires GPU, Docker Compose voice container
  3. **External TTS provider** — user supplies API endpoint and key; stored in keyring
- If Chatterbox: walk through Dockerfile.voice build, voice_ref setup, reference audio upload
- If external: store endpoint and key via `/secrets`, update config.yaml with `tts_provider: external` and `tts_endpoint`
- `self-reflect` and other identity skills should check TTS availability before generating voice output

**Files to update:**
- `skills/setup-body/SKILL.md` — add TTS step
- `config.yaml` — add `tts_provider` key (default: `chatterbox`)
- `specs/containers.md` — document voice container as optional

---

## 2. Federated vs. Non-Federated Mind Setup ✓ DONE

Currently `setup-mind` creates minds in the federated model (minds communicate via the main Hive Mind gateway/broker). There should be a non-federated option.

**Three topologies:**

| Topology | Description | When to use |
|---|---|---|
| **Federated (default)** | Minds run as containers, route through the main gateway and broker | Standard multi-mind install; requires full server stack |
| **Remote federated** | A second Hive Mind instance elsewhere, linked to the main via the broker API | Offloading to a separate machine or cloud instance |
| **Non-federated (standalone mind)** | Just the mind folder + container + its own minimal server; no gateway dependency | First-time users who want to run a single mind; simpler but isolated |

**What's needed:**
- `setup-mind` should ask: "Federated (part of a Hive Mind cluster) or standalone?"
- If **standalone**: generate a minimal docker-compose with only the mind container + its own simple message handler; no broker or gateway dependency
- If **federated**: current behavior
- If **remote federated**: guide user through linking their remote instance to the main broker endpoint (API key exchange, broker URL config)
- Add warning: "If this is your first mind and you choose standalone, you will not be able to use multi-mind features. Federated is recommended."

**Files to update:**
- `skills/setup-mind/SKILL.md` — add topology selection step
- `skills/create-mind/SKILL.md` — add `standalone` mode scaffold
- `skills/generate-compose/SKILL.md` — add standalone compose template
- `plans/phase2e-plugin-distribution.md` — update distribution notes

---

## 3. Deferred Setup Steps + Scheduler Nudge ✓ RESOLVED BY DESIGN

**Decision:** No state file. `/setup` is idempotent and detection-driven.

**Reasoning:** The ground truth for whether a step is done already exists in `config.yaml`, the keyring, running containers, and the broker. A `setup-state.json` would be a second, potentially stale copy of that reality. If setup detects current state on every run, "resume from where you left off" is free — no persistence needed. The `remind-me` scheduler approach also has a chicken-and-egg problem: the scheduler may not be running mid-install.

**What replaces it:**
- Each optional setup step begins with a detection check (e.g. `tts_provider` already set? `voice_ref/<mind>.wav` exists? Planka placeholder still literal?). If already configured → skip.
- Optional steps offer "do now / skip / skip all remaining optionals" — no "remind me later."
- Setup completion message lists any skipped optional steps: `"Optional steps skipped: TTS, Discord. Re-run /setup anytime to complete them."`
- `/setup` is always safe to re-run; re-running is the resume mechanism.

**Dropped:** `setup-state.json`, `setup-resume` skill, `/remind-me` integration in setup.

**Files to update:**
- `skills/setup/SKILL.md` — add detection-first logic to each optional step; add completion summary of skipped steps

---

## 4. Personality Builder ✓ DONE

When a new mind is created, it has no soul — just a blank container. Users need a guided flow to define its personality, voice, and identity.

**What's needed:**
- New skill: `setup-personality` (or extend `seed-mind`)
- Steps:
  1. **Name and role** — what is this mind called? What is its primary function?
  2. **Personality traits** — conversational style (formal/casual), tone (warm/precise/witty), domain focus
  3. **Soul seed** — generate a `souls/<mind-id>.md` from the answers above using Claude; review with user before saving
  4. **Voice profile** (if TTS available):
     - Select voice engine (Chatterbox, external, none)
     - If Chatterbox: upload reference audio clip, set voice_id
     - If external: configure endpoint
  5. **Seed identity into graph** — run `seed-mind` to write the soul to Neo4j as the initial identity node
- `update-mind` skill already exists and can handle post-install changes

**Files to update/create:**
- New: `skills/setup-personality/SKILL.md`
- `skills/seed-mind/SKILL.md` — ensure it accepts generated soul content, not just a file path
- `skills/setup-mind/SKILL.md` — call setup-personality after container is running

---

## 5. Setup Skill Awareness of Plugin Placeholders ✓ DONE

Skills in the plugin contain `{{USER}}`, `{{PLANKA_BOARD_ID}}`, and other placeholders. The setup flow should populate these automatically during install.

**What's needed:**
- During setup, collect: user's name, Planka board/list/label IDs (or skip Planka if not used)
- After collection, run a find-and-replace pass across all installed skills to substitute placeholders with real values
- Store the resolved values in `~/.claude-config/setup-state.json` for future reference
- This makes the plugin "live" after a single setup run without manual skill editing

**Placeholder registry (values to collect during setup):**

| Placeholder | Prompt |
|---|---|
| `{{USER}}` | "What is your name?" |
| `{{PLANKA_BOARD_ID}}` | "Paste your Planka development board ID (or skip)" |
| `{{PLANKA_PROJECT_ID}}` | "Paste your Planka project ID (or skip)" |
| `{{PLANKA_BACKLOG_LIST_ID}}` | Auto-discovered from board ID via Planka API |
| `{{PLANKA_IN_PROGRESS_LIST_ID}}` | Auto-discovered from board ID via Planka API |
| `{{PLANKA_DONE_LIST_ID}}` | Auto-discovered from board ID via Planka API |
| `{{PLANKA_ADA_LABEL_ID}}` | Auto-discovered or manually entered |
| `{{PLANKA_OWNER_LABEL_ID}}` | Auto-discovered or manually entered |
| `{{PLANKA_LOW_PRIORITY_LABEL_ID}}` | Auto-discovered or manually entered |

**Files to update:**
- `skills/setup/SKILL.md` — add placeholder resolution pass
- `skills/planka/SKILL.md` — add helper to list board IDs for setup
- `CONFIGURATION.md` — document all placeholders (already done)

---

## 6. Generic Single-Mind Telegram Bot (Parameterized via Env)

Currently `telegram_bot.py` is effectively Ada-specific. For plugin distribution, any mind should be able to run its own Telegram bot using the same code, parameterized entirely via environment variables.

**What's needed:**
- Audit `telegram_bot.py` for any Ada-specific hardcoding (mind name, channel IDs, etc.)
- Ensure all per-mind values come from env: `MIND_ID`, `TELEGRAM_BOT_TOKEN`, `GATEWAY_URL`, `ALLOWED_USERS`, `OWNER_CHAT_ID`
- `voice_id` fix is part of this: `os.getenv("MIND_ID", "default")` passed to `_tts()`
- `setup-body` skill: add Telegram bot step that collects token and wires the env block in `docker-compose.yml`
- The group-chat bot (`hivemind_bot.py`) stays separate — it's inherently multi-mind and does no per-mind TTS

**Files to update:**
- `clients/telegram_bot.py` — audit + `voice_id` fix
- `skills/setup-body/SKILL.md` — add Telegram bot setup step
- `skills/generate-compose/SKILL.md` — parameterize per-mind bot service block

---

## 7. Async Reflection Cycle (Non-Blocking Stop Hook)

The stop hook currently runs `self-reflect --load` and `self-reflect --reflect` synchronously, blocking session teardown on the bot thread.

**Two phases:**

**Phase 1 (diagnostic):** Reflection runs async (backgrounded in the hook script) so it doesn't block the bot thread, but output is still surfaced to the user via Telegram notification — visibility into what was captured.

**Phase 2 (silent):** Once validated and stable, reflection fires async and silently — no user notification, pure background.

**What's needed:**
- Update `~/.claude-config/hooks/soul_nudge.sh`: background the claude invocation (`claude ... &`)
- Phase 1: add a `notify_owner` call at the end of `self-reflect --reflect` with a brief summary
- Phase 2: remove the notify call once the cycle is trusted

**Files to update:**
- `~/.claude-config/hooks/soul_nudge.sh` — background the invocation
- `skills/self-reflect/SKILL.md` — add optional notify step for Phase 1

---

## 8. Remote Installation via SSH ✓ DONE

**Goal:** Install Hive Mind on a remote host from a running Hive Mind system, without physical access to the target.

**Feasibility assessment:**

Claude Code can run `ssh user@host "command"` non-interactively via the Bash tool — sufficient for all installation steps (clone repo, run docker compose, copy config, set secrets). Real-time interactive streaming is not needed for setup; batch SSH works.

**What works:**
- Non-interactive SSH command execution (each step runs, returns output, proceeds)
- `scp`/`rsync` for config and key file transfer
- Remote `docker compose up` — output captured and returned
- Post-install: link the remote instance to the local broker as a Remote Federated mind (see loose end #2)

**What doesn't work without extra effort:**
- Watching a full Claude Code session stream in real time on the remote host (would require the remote gateway running and accessible, or a broker tunnel)
- Secrets management on the remote — the remote keyring is separate; setup must populate it over SSH

**Proposed `setup-remote` skill flow:**
1. Collect: remote host, SSH user, SSH key path (or password)
2. Test SSH connectivity: `ssh -o BatchMode=yes user@host "echo ok"`
3. Check prerequisites remotely: Docker, Docker Compose, git, Python
4. `git clone` the Hive Mind repo on the remote
5. Transfer `.env` skeleton and `config.yaml` via `scp`
6. Run `docker compose up -d --build` remotely
7. Configure secrets on the remote via SSH + the secrets tool
8. Optionally: register the remote instance as a Remote Federated mind in the local broker

**Files to create:**
- New: `skills/setup-remote/SKILL.md`
- `skills/setup-mind/SKILL.md` — add "Remote Federated" path that calls setup-remote
- `plans/plugin-setup-loose-ends.md` — this entry

**Implemented:**
- `services/remote_admin.py` — FastAPI + WebSocket SSH bridge on port 8430, paramiko sessions, `Authorization: Bearer <token>` auth
- `docker-compose.yml` — `remote-admin` service block (port 8430, `REMOTE_ADMIN_TOKEN` env)
- `requirements.txt` — added `paramiko>=3.0`
- `minds/ada/.claude/skills/remote-admin/SKILL.md` — curl-based API reference skill
- `minds/ada/.claude/skills/setup-remote/SKILL.md` — interactive remote install workflow skill
- Plugin: same skills + `MIND-INSTALL-MANIFEST.md` updated (remote-admin in System/Ops)

---

## 9. Tools Externalization — Credentials Out of the Mind Layer

**Goal:** Move all credential-holding functionality outside the hive_mind project entirely. Minds never hold secrets — only tool API keys. Tools enforce HITL at the service layer, not the mind layer.

**Core principle:** A mind that holds a credential can leak it via prompt injection. The only fix is to ensure minds never hold credentials. Tools are standalone web API services (no mind present). Even if a tool API key leaks, the attacker hits a HITL wall with no mind to exploit.

**Design:**
- Tools extracted to their own projects adjacent to but separate from `hive_mind/`
- `~/hivemind-tools/` — generic tools service (DB, notify, Planka, crypto, weather, etc.)
- `~/remote-admin/` — SSH bridge (a la carte, own project)
- Each tools project has its own `.env` — never inside `hive_mind/` project dir
- HITL enforced per-call inside the tools service (hardcoded, not in mind)
- Skills pass `user_id` not credentials — tools service resolves credentials internally
- Install-time choice: security route (tools external) vs bundled (simpler, less secure)

**Skippy's role (revised):**
- Skippy is a *mind* (not a tools service) — Daniel's privileged delegate for operations requiring judgment
- Awakened on demand, not always running
- Can create tools, modify config, restructure projects — things tool services cannot do
- Ada can relay to Skippy; Daniel can talk to Skippy directly
- Telegram-direct = full trust; broker messages = HITL required
- Skippy is the exception to the no-credentials-in-minds rule: local, intentionally awakened, high-trust

**Files to create:**
- `specs/tools-architecture.md` — policy doc: tools layer design, HITL requirements, credential placement
- `skills/setup-tools/SKILL.md` — security-route install flow for `hivemind-tools` project
- Update `setup` skill — add security-route branch: externalize tools? yes/no
- Update `MIND-INSTALL-MANIFEST.md` — tools a la carte options

**Migration (non-breaking, future):**
- `services/remote_admin.py` stays in hive_mind for now (bundled route)
- Extraction to `~/remote-admin/` is a directory move + separate docker-compose — no code changes
- `hive-mind-mcp` project renamed conceptually to `hivemind-tools` — same codebase, new identity

**Canvas:** Full spec at `sparktobloom.com` — "Hive Mind Tools Architecture — Security Redesign"
