# Admin Mind — Non-Persistent Privileged Mind

> **Status:** Concept. Not yet designed.

## Problem

Mind CRUD skills (`/create-mind`, `/add-mind`, `/update-mind`, `/remove-mind`, `/generate-compose`) are dangerous — a prompt injection could use them to create a mind scoped to the entire system. No persistent, always-available mind should have these skills.

## Concept

A non-persistent "admin mind" that:
- Only runs when explicitly invoked at the terminal (not via Discord, Telegram, or any bot)
- Has access to the dangerous skills (mind CRUD, secret scope management)
- Session dies when the terminal session ends
- Is never reachable from the broker or inter-mind messaging
- Cannot be woken by other minds

This creates a privilege separation: persistent minds (Ada, Bilby, etc.) handle day-to-day work but cannot modify the mind roster. Administrative changes require a human at the terminal.

## Open Questions

- How is this implemented? A bare-metal Claude session with specific skill mounts?
- Should it be a mind at all, or just a local Claude Code session with the right `.claude/skills/` path?
- How does this interact with container isolation? The admin session needs Docker access to run `generate-compose` and `docker compose up`.
