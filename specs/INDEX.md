# Specs Index

Read this file first. Load only the specs relevant to the current task.

## Core Standards (always relevant)
| Spec | File | Summary |
|------|------|---------|
| Security Policy | `specs/security.md` | Hard limits, elevated-risk rules, prompt injection defense, default stance |
| Development Conventions | `specs/DEVELOPMENT.md` | Setup, testing, branching, rollback, environment variables, notification channels |

## Architecture
| Spec | File | Summary |
|------|------|---------|
| Gateway Architecture | `specs/gateway-architecture.md` | Session manager, SSE streaming, model registry, client pattern, MCP auth |

## Security Implementation
| Spec | File | Summary |
|------|------|---------|
| Secret Management | `specs/secret-management.md` | Keyring hierarchy, get_credential(), managed keys, keyring-to-env bridge |
| Tool Safety | `specs/tool-safety.md` | Ring 1 AST validation, Ring 2 subprocess isolation, blocked patterns, staging flow |
| Container Hardening | `specs/container-hardening.md` | Ring 3 runtime restrictions, compatibility exceptions, Ring 4 production volumes |
| HITL Approval | `specs/hitl-approval.md` | Approval flow, token lifecycle, blocking vs non-blocking, session heartbeat |
