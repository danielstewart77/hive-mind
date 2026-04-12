---
name: setup
description: Master setup wizard for the Hive Mind system. Bootstraps a new deployment from zero or verifies an existing one. Runs sub-setup skills in dependency order.
argument-hint: "[component]"
---

# setup

Master setup wizard. Run `/setup all` for full onboarding or `/setup <component>` for a specific step.

## Step 1 — Parse arguments

`$ARGUMENTS[0]` = optional component name.
Valid: `prerequisites`, `config`, `auth`, `nervous-system`, `provider`, `body`, `mind`, `all`
Default (no argument): show the menu.

## Step 2 — Show menu with quick health scan

```bash
gw=$(curl -sf http://localhost:8420/sessions > /dev/null 2>&1 && echo "UP" || echo "DOWN")
neo=$(curl -sf http://localhost:7474 > /dev/null 2>&1 && echo "UP" || echo "DOWN")
dk=$(docker info > /dev/null 2>&1 && echo "UP" || echo "DOWN")
minds=$(curl -sf http://localhost:8420/broker/minds 2>/dev/null | jq length 2>/dev/null || echo 0)
```

```
Hive Mind Setup
===============

Quick scan: Gateway [$gw]  Neo4j [$neo]  Docker [$dk]  Minds [$minds]

1. Prerequisites     — hardware, OS, Docker, Git         [not checked]
2. Configuration     — config.yaml, .env, compose profile [not generated]
3. Authentication    — Claude Code auth tokens            [not configured]
4. Nervous System    — gateway, broker, Neo4j, MCP        [$gw]
5. Providers         — Anthropic, OpenAI, Ollama          [check config]
6. Body              — surfaces, integrations, services   [check containers]
7. Minds             — AI minds                           [$minds registered]

Run all:  /setup all
Run one:  /setup <component>
```

## Step 3 — Run selected components in dependency order

If `all`, run in this order (each completes before the next):
1. `/setup-prerequisites`
2. `/setup-config`
3. `/setup-auth`
4. `/setup-nervous-system`
5. `/setup-provider`
6. `/setup-body`
7. `/setup-mind`

If a specific component, run just that sub-skill.

## Step 4 — Final report

After all requested components complete:

```
Setup Complete
==============

| Component       | Result       |
|-----------------|--------------|
| Prerequisites   | OK / ISSUES  |
| Configuration   | OK / ISSUES  |
| Authentication  | OK / ISSUES  |
| Nervous System  | OK / ISSUES  |
| Providers       | OK / ISSUES  |
| Body            | OK / ISSUES  |
| Minds           | OK / ISSUES  |

Next steps: <any outstanding issues or recommendations>
```
