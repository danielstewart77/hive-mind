# Security vs. Usability — Open Decisions

Outstanding security findings that require an explicit decision before remediation. Each one has a real usability cost; the right answer depends on whether the safety gain justifies it.

---

## Error Channel Architecture

**Findings:** MEDIUM-2 (skill wrappers leak stderr)

**The tension:** Raw error details — stderr output, exception messages, internal paths — are essential for Ada's self-correction loop. If a skill fails, she needs to know why. Suppressing that information makes the agent less capable. But piping it back to the end user (e.g., Discord) exposes internal file paths, API key validation failures, and database schema details.

**What needs to be decided:** Whether to build a separate error channel (agent-visible, not user-visible) before closing these findings. This requires architecture work — not just a one-line fix.

**Current posture:** Error details are returned to the tool caller. In practice this means the mind sees them; end users may also see them depending on how the client renders tool output.

---

## Docker Hardening

**Resolved:** HIGH-1 (container runs as root) — `Dockerfile` pins a non-root `USER hivemind`. MEDIUM-1 (non-pinned base image) — every Dockerfile pins a specific tag (`ubuntu:24.04`, `python:3.12-slim`, `python:3.11-slim`); no `latest` anywhere.

**Open:** HIGH-2 (broad volume mounts) — `${HOST_PROJECT_DIR:-.}:/usr/src/app` bind-mounts the whole project root into most containers, and mind containers, `lucent`, and `comms` carry none of `no-new-privileges` / `cap_drop: ALL` / `read_only: true` (the surface bots and voice servers do — see [security.md](security.md#whats-actually-built)).

**The tension:** The bind-mount approach is what makes live development practical — edit a file, it's immediately reflected in the container. A restrictive volume policy or read-only root would break that workflow for the mind/lucent/comms containers specifically (they're edited live far more often than the bots).

**What needs to be decided:** Whether to harden mind/lucent/comms uniformly with the bots now, or treat the current split as intentional (dev-convenience for the frequently-edited services, hardened for the rarely-touched ones) until a hardening pass is scoped.

---

## Long-term (no urgency decision needed)

- Expanded audit logging to all tool invocations
- `pip-audit` in development workflow — `scripts/pre-commit-pip-audit.sh` exists and `scripts/install-hooks.sh` will install it as a git pre-commit hook, but it's opt-in per clone, not installed by default or run in CI
