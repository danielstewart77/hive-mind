# Specs Index

Read this file first. Load only the specs relevant to the current task.

Plans (forward-looking, not yet implemented) live in `plans/` — see `plans/` directory.

## Core Standards (always relevant)
| Spec | File | Summary |
|------|------|---------|
| Conventions | `specs/conventions.md` | Build order (CLI → skill → spec → code), when to use skill-creator-claude / mcp-tool-builder |
| Security Policy | `specs/security.md` | Hard limits, elevated-risk rules, prompt injection defense, default stance |
| Branch Strategy | `specs/branching.md` | Branch naming, PR checklist |
| Notification Channels | `specs/notification-channels.md` | Fallback order: Telegram → Telegram API → Gmail → alert file |
| Architecture Principles | `specs/hive-mind-architecture.md` | Event → Specification → Tools pattern; what belongs where |
| Testing Guidelines | `specs/testing.md` | What makes a test worth keeping; test strategy |

## Security Implementation
| Spec | File | Summary |
|------|------|---------|
| Secret Management | `specs/secret-management.md` | Keyring hierarchy, get_credential(), managed keys, keyring-to-env bridge |
| Tool Safety | `specs/tool-safety.md` | Ring 1 AST validation, Ring 2 subprocess isolation, blocked patterns, staging flow |
| Container Hardening | `specs/container-hardening.md` | Ring 3 runtime restrictions, compatibility exceptions, Ring 4 production volumes |
| HITL Approval | `specs/hitl-approval.md` | Approval flow, token lifecycle, blocking vs non-blocking, session heartbeat |
| HITL Telegram Buttons | `specs/hitl-telegram-inline-buttons.md` | Inline keyboard button implementation for HITL approvals |
| OpenClaw CVE Analysis | `specs/openclaw-cve-analysis.md` | CVE pattern mapping to Hive Mind; hardening checklist |

## Multi-Mind Architecture
| Spec | File | Summary |
|------|------|---------|
| Multi-Mind | `docs/multi-mind.md` | Named minds (Ada/Bob/Bilby/Nagatha), backends, soul isolation, inter-mind comms — reference doc, not operational spec |
| Bob (Ollama) | `specs/bob-ollama-mind.md` | Bob mind: Ollama-backed, local/private, CLI harness pattern |
| Group Sessions | `specs/group-sessions-gateway.md` | Gateway endpoints for group chat, moderator routing |

## Infrastructure
| Spec | File | Summary |
|------|------|---------|
| Containers | `specs/containers.md` | All Docker services: names, ports, volumes, build context |
| Remote Control | `specs/remote-control-integration.md` | Session observation endpoint; real-time stream access |
| Logging | `specs/logging.md` | Structured logging levels, silence rules, rotation config |
| Epilogue Exceptions | `specs/epilogue-exceptions.md` | Exception trigger conditions for session epilogue HITL (Phase 3) |

## Voice
| Spec | File | Summary |
|------|------|---------|
| Chatterbox TTS | `specs/chatterbox.md` | Working synthesis code reference for the Chatterbox engine |
