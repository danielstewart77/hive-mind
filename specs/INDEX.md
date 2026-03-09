# Specs Index

Read this file first. Load only the specs relevant to the current task.

## Core Standards (always relevant)
| Spec | File | Summary |
|------|------|---------|
| Conventions | `specs/conventions.md` | Build order (CLI → skill → spec → code), when to use skill-creator-claude / mcp-tool-builder |
| Security Policy | `specs/security.md` | Hard limits, elevated-risk rules, prompt injection defense, default stance |
| Branch Strategy | `specs/branching.md` | Branch naming, PR checklist |
| Notification Channels | `specs/notification-channels.md` | Fallback order: Telegram → Telegram API → Gmail → alert file |

## Security Implementation
| Spec | File | Summary |
|------|------|---------|
| Secret Management | `specs/secret-management.md` | Keyring hierarchy, get_credential(), managed keys, keyring-to-env bridge |
| Tool Safety | `specs/tool-safety.md` | Ring 1 AST validation, Ring 2 subprocess isolation, blocked patterns, staging flow |
| Container Hardening | `specs/container-hardening.md` | Ring 3 runtime restrictions, compatibility exceptions, Ring 4 production volumes |
| HITL Approval | `specs/hitl-approval.md` | Approval flow, token lifecycle, blocking vs non-blocking, session heartbeat |
