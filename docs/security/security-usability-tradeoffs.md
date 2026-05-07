# Security vs. Usability — Open Decisions

Outstanding security findings that require an explicit decision before remediation. Each one has a real usability cost; the right answer depends on whether the safety gain justifies it.

---

## Error Channel Architecture

**Findings:** MEDIUM-2 (skill wrappers leak stderr), MEDIUM-3 (`fetch_articles` leaks Neo4j exception details)

**The tension:** Raw error details — stderr output, exception messages, internal paths — are essential for Ada's self-correction loop. If a skill fails, she needs to know why. Suppressing that information makes the agent less capable. But piping it back to the end user (e.g., Discord) exposes internal file paths, API key validation failures, and database schema details.

**What needs to be decided:** Whether to build a separate error channel (agent-visible, not user-visible) before closing these findings. This requires architecture work — not just a one-line fix.

**Current posture:** Error details are returned to the tool caller. In practice this means the mind sees them; end users may also see them depending on how the client renders tool output.

---

## Docker Hardening

**Findings:** HIGH-1 (container runs as root), HIGH-2 (broad volume mounts), MEDIUM-1 (non-pinned base image)

**The tension:** The current bind-mount approach (`.:/usr/src/app`) is what makes live development practical — edit a file, it's immediately reflected in the container. A restrictive volume policy would break that workflow. Running as root is similarly a developer-convenience default.

**What needs to be decided:** Whether to harden for production now (non-root user, named volumes, pinned image) or treat this as a dev-only deployment until a hardening sprint is scoped.

**Remediation (when decided):**
- Pin base image: `FROM ubuntu:24.04` (trivial, no tradeoff)
- Add non-root user: `RUN useradd -m hivemind && USER hivemind` (breaks host-path permissions; needs volume ownership alignment)
- Restrict mounts: mount only `./agents`, `./data`, `./docs` rather than the whole project root

---

## Long-term (no urgency decision needed)

- Sandboxed execution for dynamically created tools (`create_tool` writes and loads code in-process)
- Expanded audit logging to all tool invocations (currently only `create_tool` and `install_dependency`)
- `pip-audit` or `safety` in development workflow
